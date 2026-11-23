"""Big tickets become several small ones.

A ticket the planner estimates above pipeline.split_above_lines changed lines is not built
as one pull request: every task of its plan becomes an issue of its own (a part), and the
ticket itself waits until all parts are closed. ck3-battle-engine #59 went out as one pull
request of 11,235 lines: too big to review, held from auto-merge, and in conflict with every
ticket merged while it was open.

Parts that depend on no other part start at once. A part with dependencies carries the
label `blocked` and starts when the last part it depends on is merged (release). When no
part of a ticket is open any more, the ticket is closed.
"""
from __future__ import annotations

import re

from .hosts import HostError

PARENT = re.compile(r"^Teil von #(\d+)", re.M)
NEEDS = re.compile(r"^Hängt ab von: (.+)$", re.M)


def estimated_lines(plan: dict) -> int:
    return sum(int(t.get("estimated_lines") or 0) for t in plan["tasks"])


def wanted(ctx, plan: dict, issue: dict) -> bool:
    """Split this ticket? Never a part (no splitting twice), never a single task, never with the setting off."""
    limit = int(ctx.cfg["pipeline"].get("split_above_lines") or 0)
    if not limit or len(plan["tasks"]) < 2 or ctx.label("part") in (issue.get("labels") or []):
        return False
    return estimated_lines(plan) > limit


def split(ctx, plan: dict, issue: dict) -> list[int]:
    """Open one issue per task, start the ones that can start, and leave the ticket waiting for them."""
    host, L, parent = ctx.host, ctx.label, ctx.ticket
    existing = _parts_of(host, L, parent)
    if existing:   # a restart after the parts were opened: start what is ready, open nothing twice
        for part in existing:
            if not {L("blocked"), L("queued"), L("running")} & set(part["labels"]):
                _start(ctx, part["number"])
        host.labels(add=[L("split")], remove=[L("running")])
        ctx.emit("pipeline", "pipeline", outcome="split", detail=", ".join(f"#{p['number']}" for p in existing))
        return [p["number"] for p in existing]
    numbers: dict[str, int] = {}
    ready: list[int] = []
    for task in plan["tasks"]:   # the planner lists a task after the tasks it depends on
        needs = [numbers[d] for d in task.get("depends_on") or [] if d in numbers]
        body = "\n".join([
            f"Teil von #{parent}", "",
            f"**Ziel:** {task['goal']}", "",
            "**Abnahmekriterien**", *[f"- {a}" for a in task["acceptance"]],
            *(["", "**Dateien:** " + ", ".join(f"`{f}`" for f in task["files"])] if task.get("files") else []),
            *(["", "Hängt ab von: " + ", ".join(f"#{n}" for n in needs)] if needs else []),
            "", f"Aus dem Plan von #{parent} ({task['id']}, geschätzt {task.get('estimated_lines', '?')} Zeilen). "
                "Angelegt von der Software Factory.",
        ])
        labels = [L("part")] + ([L("blocked")] if needs else [])
        numbers[task["id"]] = host.create_issue(f"{issue['title']} · {task['id']}: {task['title']}", body, labels)
        if not needs:
            ready.append(numbers[task["id"]])
    for number in ready:
        _start(ctx, number)
    parts = "\n".join(f"- #{numbers[t['id']]} {t['id']}: {t['title']}"
                      + (" (wartet auf " + ", ".join(f"#{numbers[d]}" for d in t.get('depends_on') or [] if d in numbers) + ")"
                         if t.get("depends_on") else "")
                      for t in plan["tasks"])
    host.comment(f"**Aufgeteilt:** Der Plan schätzt {estimated_lines(plan)} geänderte Zeilen, mehr als "
                 f"{ctx.cfg['pipeline']['split_above_lines']}. Jeder Task wird ein eigenes Ticket mit eigenem "
                 f"Pull Request:\n\n{parts}\n\nDieses Issue schließt die Factory, sobald alle Teile erledigt sind.")
    host.labels(add=[L("split")], remove=[L("running")])
    ctx.emit("pipeline", "pipeline", outcome="split", detail=", ".join(f"#{n}" for n in numbers.values()))
    return list(numbers.values())


def _parts_of(host, L, parent: int) -> list[dict]:
    """The open parts of a ticket, with body and labels."""
    found = ({**host.issue_info(i["number"]), "number": i["number"]} for i in host.open_issues(L("part")))
    return [info for info in found if (m := PARENT.search(info.get("body") or "")) and int(m.group(1)) == parent]


def _start(ctx, number: int) -> None:
    """Start a part now; if this job may not start runs, it waits as queued and the next sweep starts it."""
    try:
        ctx.host.dispatch(issue=number)
    except HostError as exc:
        print(f"[factory] #{number} could not be started now, it waits for the next sweep: {exc}", flush=True)
        ctx.host.labels_on(number, add=[ctx.label("queued")])


def release(ctx, merged_body: str) -> None:
    """A part was merged: start the parts that waited only for done parts, and close the ticket when all are done."""
    match = PARENT.search(merged_body or "")
    if not match:
        return
    host, L, parent = ctx.host, ctx.label, int(match.group(1))
    for waiting in host.open_issues(L("blocked")):
        other = host.issue_info(waiting["number"])
        if f"Teil von #{parent}" not in other["body"]:
            continue
        needs = NEEDS.search(other["body"])
        numbers = [int(n) for n in re.findall(r"#(\d+)", needs.group(1))] if needs else []
        if all(host.issue_info(n)["state"] == "closed" for n in numbers):
            host.labels_on(waiting["number"], remove=[L("blocked")])
            _start(ctx, waiting["number"])
    open_parts = [p for p in _parts_of(host, L, parent) if p["number"] != ctx.ticket]
    if not open_parts:
        host.labels_on(parent, add=[L("done")], remove=[L("split")])
        host.close_issue_number(parent)
