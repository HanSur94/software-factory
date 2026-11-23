"""The backlog planner proposes tickets when the factory is idle (factory/backlog.py)."""
import json
from types import SimpleNamespace

from factory import backlog, config, stations


class Store:
    def __init__(self, root):
        self.root, self.saved = root, []
        (root / "tickets").mkdir(parents=True)

    def tickets_dir(self):
        return self.root / "tickets"

    def save(self, message):
        self.saved.append(message)


class Host:
    def __init__(self, busy=()):
        self.busy, self.created = set(busy), []

    def open_issues(self, label):
        return [{"number": 1}] if label in self.busy else []

    def _api(self, method, path, body=None, check=True):
        return [{"number": 5, "title": "Old ticket", "labels": [{"name": "factory:done"}]},
                {"number": 6, "title": "A PR", "pull_request": {}}]

    def create_issue(self, title, body, labels):
        self.created.append((title, body, labels))
        return 100 + len(self.created)


def _ctx(tmp_path, **conf):
    cfg = config.load_config()
    cfg["backlog"] = {"enabled": True, "every_hours": 24, "max_proposals": 2, **conf}
    return SimpleNamespace(cfg=cfg, workdir=tmp_path / "work", repo=tmp_path, store=Store(tmp_path / "state"),
                           label=lambda name: "factory:" + name)


def _station(proposals, seen=None):
    def run(ctx, role, template, values, cwd, out, loader, **kw):
        if seen is not None:
            seen.update(values)
        out.write_text(json.dumps({"schema": "backlog/1", "proposals": proposals}))
        return None, loader(out)
    return run


P = {"title": "Units · Retreat orders", "why": "game-design.md promises it.", "mandate": "Let a unit retreat.",
     "acceptance": ["A unit ordered to retreat leaves the field"], "where": ["Assets/Units/"]}


def test_an_idle_factory_gets_proposals_labelled_proposed(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(stations, "run_station", _station([P, P, P], seen))
    ctx, host = _ctx(tmp_path), Host()
    assert backlog.tick(ctx, host) == "proposed 101,102"           # at most max_proposals
    title, body, labels = host.created[0]
    assert title == P["title"] and labels == ["factory:proposed"] and "factory:go" in body
    assert "A unit ordered to retreat" in body and "game-design.md" in body
    opened = json.loads((tmp_path / "work" / "backlog" / "open-issues.json").read_text())
    assert opened == [{"number": 5, "title": "Old ticket", "labels": ["factory:done"]}]   # no pull requests


def test_a_busy_factory_or_an_unanswered_proposal_gets_none(tmp_path, monkeypatch):
    monkeypatch.setattr(stations, "run_station", _station([P]))
    for label in ("factory:running", "factory:queued", "factory:proposed", "factory:go"):
        assert backlog.tick(_ctx(tmp_path / label.replace(":", "-")), Host(busy=[label])) == "busy"


def test_one_round_a_day(tmp_path, monkeypatch):
    monkeypatch.setattr(stations, "run_station", _station([P]))
    ctx, host = _ctx(tmp_path), Host()
    backlog.tick(ctx, host)
    assert backlog.tick(ctx, Host()) == "recent"


def test_off_unless_the_project_turns_it_on(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.cfg["backlog"]["enabled"] = False
    assert backlog.tick(ctx, Host()) == "off"


def test_the_planners_session_is_kept_where_the_board_reads_it(tmp_path, monkeypatch):
    """The planner's session used to stay in the runner's temp folder: the board could show only its result."""
    from factory import board
    where = {}

    def run(ctx, role, template, values, cwd, out, loader, **kw):
        where["run_dir"] = ctx.run_dir
        (ctx.run_dir / "logs").mkdir()
        (ctx.run_dir / "logs" / "01-backlog.md").write_text("model: fable\n\nTwo proposals from the design doc.")
        out.write_text(json.dumps({"schema": "backlog/1", "proposals": [P]}))
        return None, loader(out)
    monkeypatch.setattr(stations, "run_station", run)
    ctx = _ctx(tmp_path)
    backlog.tick(ctx, Host())
    folder = where["run_dir"]
    assert folder.parent == tmp_path / "state" / "orchestrator" / "backlog" and getattr(ctx, "run_dir", None) is None
    files = {f.name: f.read_text() for f in folder.iterdir() if f.is_file()}
    dirs = {d.name: {f.name: f.read_text() for f in d.iterdir()} for d in folder.iterdir() if d.is_dir()}
    seen = board.backlog_session(files, dirs)
    assert seen["proposals"] == [{"issue": 101, "title": P["title"]}]
    assert seen["runs"][0]["final"].startswith("Two proposals")


def test_only_the_newest_planner_sessions_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(stations, "run_station", _station([P]))
    runs = tmp_path / "state" / "orchestrator" / "backlog"
    for i in range(backlog.KEEP_RUNS + 3):
        (runs / f"2026010{i}T000000Z").mkdir(parents=True)
    backlog.tick(_ctx(tmp_path), Host())
    assert len([d for d in runs.iterdir() if d.is_dir()]) == backlog.KEEP_RUNS


def test_in_auto_mode_proposals_start_at_once(tmp_path, monkeypatch):
    monkeypatch.setattr(stations, "run_station", _station([P]))
    ctx, host = _ctx(tmp_path, mode="auto"), Host()
    host.dispatched = []
    host.dispatch = lambda issue=None, dry_run=False: host.dispatched.append(issue)
    assert backlog.tick(ctx, host) == "started 101"
    title, body, labels = host.created[0]
    assert labels == [] and host.dispatched == [101] and "automatisch gestartet" in body


def test_the_board_shows_a_proposal_as_waiting_for_the_owner():
    from factory import board
    c = board.card("o/r", 101, {}, {"title": "Retreat orders", "url": "u", "labels": ["factory:proposed"]}, None)
    assert c["column"] == "proposed" and not c["restartable"] and "Starten" in c["detail"]
