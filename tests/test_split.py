"""Big tickets become several small ones (factory/split.py)."""
import json
from types import SimpleNamespace

from factory import cli, fake, split

real_fake = fake.run


def _plan(lines):
    tasks = ""
    for i, n in enumerate(lines, 1):
        tasks += (f"  - id: T{i}\n    title: Part {i}\n    goal: Goal {i}.\n    acceptance: [\"criterion {i}\"]\n"
                  f"    files: [part{i}.md]\n    depends_on: [{'T1' if i > 1 else ''}]\n    estimated_lines: {n}\n")
    return tasks.replace("depends_on: []", "depends_on: []")


def _run_with_plan(product, monkeypatch, lines):
    def run(role, values, cwd, out):
        real_fake(role, values, cwd, out)
        if role == "planner":
            out.write_text(f"---\nschema: plan/1\nticket: {values['ticket']}\nsummary: Big.\ntasks:\n{_plan(lines)}---\n")
    monkeypatch.setattr(fake, "run", run)
    cli.main(product.args)
    return product.status()


def _created(product):
    path = product.run_dir / "created-issues.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _dispatched(product):
    path = product.run_dir / "dispatched.jsonl"
    return [json.loads(line)["issue"] for line in path.read_text().splitlines()] if path.exists() else []


def test_a_big_plan_becomes_one_ticket_per_task(product, monkeypatch):
    st = _run_with_plan(product, monkeypatch, [500, 500])
    assert st["phase"] == "split" and st["stopped"] is None
    parts = _created(product)
    assert len(parts) == 2
    first, second = parts
    assert first["labels"] == ["factory:part"] and second["labels"] == ["factory:part", "factory:blocked"]
    assert first["body"].startswith(f"Teil von #{st['ticket']}") and "criterion 1" in first["body"]
    assert f"Hängt ab von: #{first['number']}" in second["body"]
    assert _dispatched(product) == [first["number"]]          # the blocked part waits
    assert "Aufgeteilt" in (product.run_dir / "comments.md").read_text()
    assert not any(e["station"] == "implementer" for e in product.events())


def test_a_split_ticket_started_again_rests_instead_of_stopping(product, monkeypatch):
    st = _run_with_plan(product, monkeypatch, [500, 500])
    assert st["phase"] == "split"
    # Tickets stopped by the old check: a run from the queue or the board's restart now puts them back to rest.
    status = json.loads((product.run_dir / "status.json").read_text())
    status["stopped"] = "Ticket steht in Phase `split` und kann von hier nicht weiterlaufen."
    (product.run_dir / "status.json").write_text(json.dumps(status))
    before = len(_created(product))
    assert cli.main(product.args) == 0
    st = product.status()
    assert st["phase"] == "split" and st["stopped"] is None and len(_created(product)) == before
    assert not any(e.get("outcome") == "stopped" for e in product.events())


def test_the_board_shows_an_old_split_stop_as_split_not_stopped():
    from factory import board
    status = {"phase": "split", "stopped": "Ticket steht in Phase `split` und kann von hier nicht weiterlaufen."}
    c = board.card("o/r", 5, {"status.json": json.dumps(status), "events.jsonl": json.dumps(
        {"ts": "2026-11-21T18:46:27Z", "kind": "pipeline", "station": "pipeline", "outcome": "split", "detail": "#6, #7"})},
        None, None)
    assert c["column"] != "stopped" and c["parts"] == [6, 7]


def test_a_plan_under_the_limit_is_built_as_one_ticket(product, monkeypatch):
    st = _run_with_plan(product, monkeypatch, [300, 300])
    assert st["phase"] == "delivered" and not _created(product)


def test_a_single_task_is_never_split(product, monkeypatch):
    st = _run_with_plan(product, monkeypatch, [5000])
    assert st["phase"] == "delivered" and not _created(product)


class Host:
    def __init__(self, issues):
        self.issues, self.dispatched, self.closed, self.label_calls = issues, [], [], []

    def open_issues(self, label):
        return [{"number": n} for n, i in self.issues.items() if i["state"] == "open" and label in i["labels"]]

    def issue_info(self, number):
        return self.issues[number]

    def labels_on(self, number, add=(), remove=()):
        self.label_calls.append((number, tuple(add), tuple(remove)))
        for name in remove:
            if name in self.issues[number]["labels"]:
                self.issues[number]["labels"].remove(name)

    def dispatch(self, issue=None):
        self.dispatched.append(issue)

    def close_issue_number(self, number):
        self.closed.append(number)


def _ctx(host, ticket):
    return SimpleNamespace(host=host, ticket=ticket, label=lambda name: "factory:" + name)


def _issue(state, body, labels):
    return {"state": state, "body": body, "labels": list(labels)}


def test_a_merged_part_starts_the_parts_that_waited_for_it():
    host = Host({
        10: _issue("open", "parent", ["factory:split"]),
        11: _issue("closed", "Teil von #10\n", ["factory:part"]),
        12: _issue("open", "Teil von #10\n\nHängt ab von: #11", ["factory:part", "factory:blocked"]),
        13: _issue("open", "Teil von #10\n\nHängt ab von: #11, #12", ["factory:part", "factory:blocked"]),
        20: _issue("open", "Teil von #99\n\nHängt ab von: #11", ["factory:part", "factory:blocked"]),
    })
    split.release(_ctx(host, 11), "Teil von #10\n")
    assert host.dispatched == [12]                  # #13 still waits for #12; #20 belongs to another ticket
    assert host.closed == []


def test_the_last_merged_part_closes_the_ticket():
    host = Host({
        10: _issue("open", "parent", ["factory:split"]),
        11: _issue("closed", "Teil von #10\n", ["factory:part"]),
        12: _issue("closed", "Teil von #10\n\nHängt ab von: #11", ["factory:part"]),
    })
    split.release(_ctx(host, 12), "Teil von #10\n\nHängt ab von: #11")
    assert host.closed == [10] and (10, ("factory:done",), ("factory:split",)) in host.label_calls


def test_a_ticket_that_is_no_part_releases_nothing():
    host = Host({})
    split.release(_ctx(host, 5), "An ordinary issue.")
    assert host.dispatched == [] and host.closed == []


def test_a_part_that_cannot_be_started_now_waits_for_the_sweep():
    """The work job's token could not start runs (HTTP 403), and the ticket broke off."""
    from factory.hosts import HostError

    class Refusing(Host):
        def dispatch(self, issue=None):
            raise HostError("gh workflow run factory.yml failed: HTTP 403: Resource not accessible by integration")

    host = Refusing({
        10: _issue("open", "parent", ["factory:split"]),
        11: _issue("closed", "Teil von #10\n", ["factory:part"]),
        12: _issue("open", "Teil von #10\n\nHängt ab von: #11", ["factory:part", "factory:blocked"]),
    })
    split.release(_ctx(host, 11), "Teil von #10\n")
    assert (12, ("factory:queued",), ()) in host.label_calls


def test_a_restarted_ticket_does_not_open_its_parts_twice():
    """A ticket whose run broke off after opening its parts plans again on restart and must reuse them."""
    created = []

    class Again(Host):
        def labels(self, add=(), remove=()):
            pass

        def create_issue(self, title, body, labels):
            created.append(title)
            return 99

    host = Again({
        10: _issue("open", "parent", []),
        11: _issue("open", "Teil von #10\n", ["factory:part"]),
        12: _issue("open", "Teil von #10\n\nHängt ab von: #11", ["factory:part", "factory:blocked"]),
        13: _issue("open", "Teil von #100\n", ["factory:part"]),
    })
    for number, info in host.issues.items():
        info["number"] = number
    events = []
    ctx = SimpleNamespace(host=host, ticket=10, label=lambda name: "factory:" + name,
                          emit=lambda *a, **k: events.append(k))
    plan = {"tasks": [{"id": "T1", "title": "a", "goal": "g", "acceptance": ["x"]}]}
    assert split.split(ctx, plan, {"title": "t"}) == [11, 12]
    assert not created and host.dispatched == [11] and events[0]["outcome"] == "split"
