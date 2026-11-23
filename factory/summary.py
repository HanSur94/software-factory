"""The closing report: once a ticket's pull request is merged, the issue says what was done and how it was proved.

Posted once (status summary_posted), on either way a ticket ends merged: the factory merged it itself
(deliver._merge) or the owner merged the pull request (deliver.human_outcome). Switched by
delivery.closing_summary. It never stops anything: a report that cannot be written is a line in the log.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import contracts, events, evidence, floormap, machine

MODES = {"advisory": "beratend", "gate": "Tor", "off": "aus"}
LIMIT = 60000   # GitHub takes 65 536 characters in a comment


def _read(ctx):
    run = ctx.run_dir
    ticket = contracts.read_json(run / "ticket.json", "ticket") if (run / "ticket.json").exists() else {}
    plan = contracts.read_frontmatter(run / "plan.md", "plan")[0] if (run / "plan.md").exists() else {}
    review = contracts.read_frontmatter(run / "review.md", "review")[0] if (run / "review.md").exists() else None
    verdict = contracts.read_json(run / "verdict.json", "verdict") if (run / "verdict.json").exists() else None
    return ticket, plan, review, verdict, events.read(run)


def proof_rows(ctx, verdict: dict | None, review: dict | None, evs: list[dict], each_check: bool = False) -> list[str]:
    """The stations' results as a table. each_check: one row per check of the last round, with its time."""
    def last(station: str, kind: str = "verdict") -> dict | None:
        found = [e for e in evs if e["station"] == station and e["kind"] == kind]
        return found[-1] if found else None

    rows = ["| Prüfung | Modus | Ergebnis |", "|---|---|---|"]
    e = last("checks")
    checks = {}
    for c in (x for x in evs if x["station"] == "checks" and x["kind"] == "check"):
        checks[c.get("detail") or "?"] = c          # the last run of each check
    if each_check and checks:
        for name, c in checks.items():
            took = f" ({_dur(c.get('duration_s'))})" if c.get("duration_s") else ""
            rows.append(f"| Check `{name}` | Tor | {'bestanden' if c['outcome'] == 'pass' else 'nicht bestanden'}{took} |")
    else:
        rows.append(f"| Checks | Tor | {'bestanden' if e and e['outcome'] == 'pass' else 'nicht bestanden'} |")
    e = last("plan_check")
    if e:
        rows.append(f"| Plan-Prüfung | {MODES[ctx.mode('plan_check')]} | {e['outcome']} ({e.get('detail', '')}) |")
    if review:
        result = "freigegeben" if review["verdict"] == "approve" else "Änderungen verlangt"
        rows.append(f"| Review | {MODES[ctx.mode('review')]} | {result}: {len(review['blocking'])} blockierend, "
                    f"{len(review['minor'])} unkritisch |")
    if verdict is None or verdict.get("passed") is None:
        note = (verdict or {}).get("notes") or "Der Richter ist nicht gelaufen."
        rows.append(f"| Richter | {MODES[ctx.mode('judge')]} | kein Urteil: {note} |")
    else:
        result = "bestanden" if verdict["passed"] else "nicht bestanden"
        rows.append(f"| Richter | {MODES[ctx.mode('judge')]} | {verdict['scenarios_passed']} von "
                    f"{verdict['scenarios_total']} Szenarien, Schwelle {verdict['threshold']:.2f}: {result} |")
    e = last("ci")
    if e:
        rows.append(f"| CI des Projekts | Tor | {'grün' if e['outcome'] == 'pass' else 'rot'}: {e.get('detail', '')} |")
    return rows


def _dur(seconds) -> str:
    s = round(float(seconds or 0))
    if s < 90:
        return f"{s} s"
    m = round(s / 60)
    return f"{m} min" if m < 90 else f"{m // 60} h {m % 60} min"


def _tokens(n: int) -> str:
    return f"{n / 1e6:.1f} Mio.".replace(".", ",") if n >= 1e6 else f"{round(n / 1e3)} Tsd." if n >= 1e3 else str(n)


def _effort(ctx, evs: list[dict]) -> str:
    runs = [e for e in evs if e["kind"] == "agent"]
    tokens = sum(sum(int(v or 0) for v in (e.get("tokens") or {}).values()) for e in runs)
    for e in evs:   # the judge's runs stay in its job; its verdict event carries their tokens
        if e["station"] == "judge" and e["kind"] == "verdict":
            tokens += sum(int(v or 0) for v in (e.get("tokens") or {}).values())
    st = ctx.status
    parts = [f"{_tokens(tokens)} Tokens" if tokens else "", f"{len(runs)} Agent-Läufe",
             f"{st.get('rework_rounds', 0)} Nacharbeitsrunden" if st.get("rework_rounds") else "",
             f"Richter-Runde {st['round']}" if st.get("round", 1) > 1 else ""]
    if evs:
        try:
            start = datetime.fromisoformat(evs[0]["ts"].replace("Z", "+00:00"))
            parts.append("Durchlauf " + _dur((datetime.now(timezone.utc) - start).total_seconds()))
        except (KeyError, ValueError):
            pass
    used = [e for e in evs if e["kind"] == "pipeline" and e.get("outcome") == "resources"]
    if used:   # each work job measured itself (factory/machine.py)
        parts.append("Rechner: " + machine.describe({
            "cpu_min": round(sum(float(e.get("cpu_min") or 0) for e in used), 1),
            "peak_gb": max(float(e.get("peak_gb") or 0) for e in used),
            "heavy_peak": max(int(e.get("heavy_peak") or 0) for e in used)}))
    if (ctx.cfg.get("board") or {}).get("show_cost"):
        parts.append(f"etwa {st.get('spent_usd', 0.0):.2f} $ nach Listenpreis")
    return " · ".join(p for p in parts if p)


def _notes(ctx) -> list[str]:
    """What the implementer wrote down about its work: what it did, what it could not check."""
    out = []
    for path in sorted(ctx.run_dir.glob("notes-*.md")):
        text = path.read_text(errors="replace").strip()
        if text:
            out += [f"**{path.stem.removeprefix('notes-')}**", "", text[:1500] + (" …" if len(text) > 1500 else ""), ""]
    if not out:
        return []
    return ["<details><summary>Notizen des Implementierers</summary>", "", *out, "</details>", ""]


def closing(ctx, how: str, pr: dict | None = None) -> str:
    """how: "factory" (merged itself) or "human" (the owner merged the pull request)."""
    from .deliver import assumptions

    ticket, plan, review, verdict, evs = _read(ctx)
    st, pr = ctx.status, pr or {}
    url = st.get("pr") or ""
    who = "von der Factory automatisch gemergt" if how == "factory" else "von dir gemergt"
    if url.startswith("http"):
        done = f"**Erledigt:** [PR #{url.rstrip('/').rsplit('/', 1)[-1]}]({url}) {who}"
    else:   # a local run has a branch, not a pull request
        done = f"**Erledigt:** {who}" + (f" ({url})" if url else "")
    head = ["## Abschlussbericht", "", done]
    if pr.get("merge_commit_sha"):
        head[-1] += f" · Commit `{pr['merge_commit_sha'][:7]}`"
    if pr.get("changed_files") is not None:
        head += ["", f"**Umfang:** {pr['changed_files']} Dateien, +{pr.get('additions', 0)} / −{pr.get('deletions', 0)} Zeilen"]
    parts = head + [""]
    if ticket.get("mandate"):
        parts += [f"**Auftrag:** {ticket['mandate']}", ""]
    if ticket.get("acceptance"):
        judged = verdict and verdict.get("passed")
        parts += ["### Abnahmekriterien", "", *[f"- {a}" for a in ticket["acceptance"]], ""]
        if judged:
            parts += [f"Der Richter hat sie mit {verdict['scenarios_passed']} von {verdict['scenarios_total']} "
                      "verdeckten Szenarien geprüft.", ""]
    if plan:
        done = set(st.get("done_tasks") or [])
        parts += ["### Was umgesetzt wurde", "", plan.get("summary", ""), "",
                  *[f"- {'✓' if t['id'] in done else '○'} **{t['id']}** {t['title']}" for t in plan.get("tasks") or []], ""]
    parts += ["### Nachweise", "", *proof_rows(ctx, verdict, review, evs, each_check=True), ""]
    pictures = evidence.pr_section(ctx)
    if pictures:
        parts += ["### Belege", *pictures[2:]]   # without its own heading line
    parts += floormap.section(ctx.run_dir, "### Grundriss")
    if st.get("notes"):
        parts += ["**Hinweise**", *[f"- {i}" for i in st["notes"]], ""]
    folded = [("Annahmen der Factory", assumptions(ticket, plan)),
              ("Review: unkritische Punkte", [f["text"] for f in (review or {}).get("minor") or []])]
    for title, items in folded:   # worth reading once, but they would bury the proofs
        if items:
            parts += [f"<details><summary>{title} ({len(items)})</summary>", "", *[f"- {i}" for i in items], "", "</details>", ""]
    parts += _notes(ctx)
    parts += [f"**Aufwand:** {_effort(ctx, evs)}"]
    repo = getattr(ctx.host, "repo", None)
    if repo:
        branch = ctx.cfg["storage"]["branch"]
        parts += ["", f"Alle Protokolle, Sitzungen und Logs: [`{branch}/tickets/{ctx.ticket}`]"
                      f"(https://github.com/{repo}/tree/{branch}/tickets/{ctx.ticket})"]
    text = "\n".join(parts)
    return text if len(text) <= LIMIT else text[:LIMIT] + "\n\n… (gekürzt)"


def post(ctx, how: str) -> bool:
    """Write the closing report on the issue, once."""
    try:
        st = ctx.status
        if not (ctx.cfg.get("delivery") or {}).get("closing_summary", True) or st.get("summary_posted"):
            return False
        info = getattr(ctx.host, "pr_info", None)
        pr = info(st["pr"]) if info and st.get("pr") else {}
        ctx.host.comment(closing(ctx, how, pr))
        st["summary_posted"] = events.now()
        return True
    except Exception as exc:   # the merge stands; the report is a help, not a step that may fail it
        print(f"[factory] closing report not written: {exc}", flush=True)
        return False
