"""The floor plan: the cartographer's map on the state branch, a ticket laid over it, and what PR, report and board show."""
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from factory import board, floormap

NODE = shutil.which("node")

# A stand-in for floorplan's scripts: same arguments, same --json answers, files written where floorplan writes them.
SCAN = r"""
const fs = require('fs'), path = require('path'), cp = require('child_process');
const root = process.argv[2];
const files = cp.execFileSync('git', ['ls-files'], { cwd: root, encoding: 'utf8' }).split('\n').filter(Boolean)
  .map(p => ({ path: p, repo: '.', category: 'code', lang: 'csharp', loc: 1, bytes: 1 }));
const edges = files.length > 1 ? [{ from: files[0].path, to: files[1].path }] : [];
fs.mkdirSync(path.join(root, 'floorplan', '.cache'), { recursive: true });
fs.writeFileSync(path.join(root, 'floorplan', '.cache', 'scan.json'), JSON.stringify({ version: 1, files, edges, extEdges: [] }));
console.log(JSON.stringify({ schema: 'floorplan/scan-result@1', ok: true }));
"""
BUILD = r"""
const fs = require('fs'), path = require('path');
const dir = path.join(process.argv[2], 'floorplan');
const scan = JSON.parse(fs.readFileSync(path.join(dir, '.cache', 'scan.json'), 'utf8'));
const arch = JSON.parse(fs.readFileSync(path.join(dir, 'architecture.json'), 'utf8'));
const recent = fs.existsSync(path.join(dir, 'recent-work.json')) ? JSON.parse(fs.readFileSync(path.join(dir, 'recent-work.json'), 'utf8')) : null;
const nodeOf = p => p.startsWith('ui/') ? 'game.ui' : 'game.sim';
const graph = { nodes: [{ id: 'game', parent: null, label: 'Das Spiel' }, { id: 'game.ui', parent: 'game', label: 'Menüs' },
                        { id: 'game.sim', parent: 'game', label: 'Simulation' }],
  files: scan.files.map(f => ({ p: f.path, n: nodeOf(f.path), name: f.path, d: 'Beschreibung von ' + f.path })),
  links: scan.edges.map(e => ({ a: 'f:' + e.from, b: 'f:' + e.to, k: 'import', w: 1 })), recent };
fs.writeFileSync(path.join(dir, 'graph.json'), JSON.stringify(graph));
fs.writeFileSync(path.join(dir, 'index.html'), '<html>' + arch.project.name + ' ' + JSON.stringify(recent) + '</html>');
console.log(JSON.stringify({ schema: 'floorplan/build-result@1', ok: true, files: { total: scan.files.length },
  nodes: graph.nodes, descriptions: { complete: true } }));
"""
REFRESH = r"""
console.log(JSON.stringify({ schema: 'floorplan/refresh-result@1', ok: true, next: [{ code: 'recent_work' }], report: 'nichts Neues' }));
"""


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


@pytest.fixture
def skill(tmp_path):
    root = tmp_path / "work" / "plugins" / "floorplan" / "skills" / "floorplan" / "scripts"
    root.mkdir(parents=True)
    for name, code in (("scan", SCAN), ("build", BUILD), ("refresh", REFRESH)):
        # .mjs files are ES modules: the stand-ins get a require of their own
        (root / f"{name}.mjs").write_text("import { createRequire } from 'module'; const require = createRequire(import.meta.url);\n" + code)
    return root.parent


def cfg():
    return {"map": {"enabled": True, "every_minutes": 60, "max_parts": 12},
            "plugins": {"floorplan": {"repo": "x/floorplan", "ref": "0" * 40, "enabled": True}}}


def product(tmp_path) -> Path:
    repo = tmp_path / "product"
    (repo / "ui").mkdir(parents=True)
    (repo / "ui" / "Menu.cs").write_text("class Menu {}\n")
    (repo / "Sim.cs").write_text("class Sim {}\n")
    sh(tmp_path, "git", "init", "-q", "-b", "main", str(repo))
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    return repo


def bundle(repo: Path) -> dict:
    return {"architecture": {"project": {"name": "Spiel"}}, "descriptions": {"game": {"files": {}}},
            "scan": {"version": 1, "files": [{"path": "Sim.cs"}, {"path": "ui/Menu.cs"}], "edges": [], "extEdges": []}}


def test_pack_and_unpack_keep_what_a_map_is_drawn_from(tmp_path):
    folder = tmp_path / "floorplan"
    floormap.unpack(bundle(tmp_path), folder)
    assert floormap.pack(folder) == bundle(tmp_path)


def test_touched_parts_mermaid_and_markdown(tmp_path):
    graph = {"nodes": [{"id": "game", "parent": None, "label": "Das Spiel"},
                       {"id": "game.input", "parent": "game", "label": "Eingabe", "summary": "Tasten"},
                       {"id": "game.ui", "parent": "game", "label": "Menüs \"und\" Anzeige"}],
             "files": [{"p": "In.cs", "n": "game.input", "name": "In", "d": "Liest Tasten."},
                       {"p": "Ui.cs", "n": "game.ui", "name": "Ui", "d": "Zeigt Menüs."}],
             "links": [{"a": "f:In.cs", "b": "f:Ui.cs", "w": 2}, {"a": "f:Other.cs", "b": "f:Ui.cs", "w": 1}]}
    changes = [{"path": "In.cs", "status": "new", "add": 40, "del": 0}, {"path": "Ui.cs", "status": "changed", "add": 2, "del": 1},
               {"path": "Old.cs", "status": "deleted", "add": 0, "del": 9}, {"path": "Packages/x.json", "status": "changed", "add": 1, "del": 1}]
    record = floormap.touched(graph, changes)
    assert [p["id"] for p in record["parts"]] == ["game.input", "game.ui"]
    assert record["parts"][0]["path"] == ["Das Spiel", "Eingabe"] and record["links"] == [["game.input", "game.ui", 2]]
    assert [c["path"] for c in record["off_map"]] == ["Old.cs", "Packages/x.json"]
    chart = "\n".join(floormap.mermaid(record))
    assert 'subgraph n_game_g["Das Spiel"]' in chart and 'n_game_input["Eingabe<br/>1 neu"]:::new' in chart
    assert 'n_game_ui["Menüs und Anzeige<br/>1 geändert"]:::changed' in chart and "n_game_input --> n_game_ui" in chart
    (tmp_path / floormap.RECORD).write_text(json.dumps(record))
    text = "\n".join(floormap.section(tmp_path))
    assert "- **Das Spiel › Eingabe**" in text and "neu: `In.cs` (+40 / −0) – Liest Tasten." in text
    assert "gelöscht: `Old.cs`" in text and "nicht auf der Karte: `Packages/x.json`" in text
    assert floormap.section(tmp_path / "nothing") == []
    shown = board.floorplan_of(json.dumps(record))
    assert shown["parts"][0]["files"][0] == {"path": "In.cs", "status": "new", "add": 40, "del": 0, "name": "In", "d": "Liest Tasten."}
    assert board.floorplan_of(None) is None and board.floorplan_of("{}") is None


def test_the_board_draws_a_ticket_from_the_base_scan_and_the_ticket_delta():
    base = {"files": [{"path": "A.cs"}, {"path": "B.cs"}, {"path": "Gone.cs"}],
            "edges": [{"from": "A.cs", "to": "B.cs"}, {"from": "B.cs", "to": "A.cs"}], "extEdges": []}
    ticket = {"files": [{"path": "A.cs", "loc": 9}, {"path": "B.cs"}, {"path": "New.cs"}],
              "edges": [{"from": "New.cs", "to": "A.cs"}, {"from": "B.cs", "to": "A.cs"}], "extEdges": []}
    changes = [{"path": "A.cs", "status": "changed"}, {"path": "New.cs", "status": "new"}, {"path": "Gone.cs", "status": "deleted"}]
    d = floormap.delta(base, ticket, changes)
    assert [f["path"] for f in d["files"]] == ["A.cs", "New.cs"] and d["drop"] == ["A.cs", "Gone.cs", "New.cs"]
    merged = floormap.apply_delta(base, d)
    assert sorted(f["path"] for f in merged["files"]) == ["A.cs", "B.cs", "New.cs"]
    assert {"from": "New.cs", "to": "A.cs"} in merged["edges"] and {"from": "B.cs", "to": "A.cs"} in merged["edges"]


class Store:
    def __init__(self, root: Path):
        self.root, self.saved = root, []
        (root / "tickets").mkdir(parents=True, exist_ok=True)

    def tickets_dir(self):
        return self.root / "tickets"

    def save(self, message):
        self.saved.append(message)


@pytest.mark.skipif(not NODE, reason="needs node")
def test_the_cartographer_refreshes_without_an_agent_when_nothing_new_needs_words(tmp_path, skill, monkeypatch):
    repo = product(tmp_path)
    store = Store(tmp_path / "state")
    (store.root / "map").mkdir()
    (store.root / floormap.BUNDLE).write_text(json.dumps(bundle(repo)))
    ctx = SimpleNamespace(cfg=cfg(), repo=repo, workdir=tmp_path / "work", store=store, status={})
    monkeypatch.setattr(floormap, "_draw", lambda *a, **k: pytest.fail("no agent needed"))
    assert floormap.tick(ctx) == "refreshed"
    meta = json.loads((store.root / floormap.META).read_text())
    assert meta["head"] == sh(repo, "git", "rev-parse", "HEAD") and meta["agent"] is False and meta["files"] == 2
    assert store.saved == [f"map: refreshed at {meta['head'][:8]}"] and not (repo / "floorplan").exists()
    assert floormap.tick(ctx) == "current"
    assert floormap.tick(SimpleNamespace(**{**vars(ctx), "cfg": {"map": {"enabled": False}}})) == "off"


@pytest.mark.skipif(not NODE, reason="needs node")
def test_a_first_map_needs_the_agent_and_a_failure_keeps_the_old_one(tmp_path, skill, monkeypatch):
    repo = product(tmp_path)
    store = Store(tmp_path / "state")
    ctx = SimpleNamespace(cfg=cfg(), repo=repo, workdir=tmp_path / "work", store=store, status={})
    drawn = []

    def draw(ctx, skill, repo, first, report):
        drawn.append(first)
        floormap.unpack(bundle(repo), repo / "floorplan")
        return 1.25
    monkeypatch.setattr(floormap, "_draw", draw)
    assert floormap.tick(ctx) == "drawn with agent" and drawn == [True]
    assert json.loads((store.root / floormap.META).read_text())["last_cost_usd"] == 1.25
    (repo / "x.cs").write_text("x")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "more")

    def broken(*a, **k):
        raise RuntimeError("agent gave up")
    monkeypatch.setattr(floormap, "script", lambda skill, name, root, *a, **k: {"ok": True, "next": [{"code": "describe"}]})
    monkeypatch.setattr(floormap, "_draw", broken)
    assert floormap.tick(ctx).startswith("failed: agent gave up")
    assert floormap.read_bundle(store.root) is not None   # the old map stays
    assert floormap.tick(ctx) == "recent"                 # and nobody tries again at once


@pytest.mark.skipif(not NODE, reason="needs node")
def test_a_ticket_lays_its_change_over_the_map_and_the_board_draws_it(tmp_path, skill):
    repo = product(tmp_path)
    base = sh(repo, "git", "rev-parse", "HEAD")
    store = Store(tmp_path / "state")
    (store.root / "map").mkdir()
    (store.root / floormap.BUNDLE).write_text(json.dumps(bundle(repo)))
    run = store.tickets_dir() / "5"
    run.mkdir()
    (run / "ticket.json").write_text(json.dumps({"title": "Neues Menü"}))
    (repo / "ui" / "Pause.cs").write_text("class Pause {}\n")
    (repo / "Sim.cs").write_text("class Sim { int x; }\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "ticket")
    said = []
    ctx = SimpleNamespace(cfg=cfg(), workdir=tmp_path / "work", store=store, status={"base_sha": base}, run_dir=run,
                          ticket=5, emit=lambda *a, **k: said.append(k))
    record = floormap.overlay(ctx, repo)
    assert [(p["label"], [f["path"] for f in p["files"]]) for p in record["parts"]] == [("Menüs", ["ui/Pause.cs"]), ("Simulation", ["Sim.cs"])] \
        or [(p["label"], [f["path"] for f in p["files"]]) for p in record["parts"]] == [("Simulation", ["Sim.cs"]), ("Menüs", ["ui/Pause.cs"])]
    assert said[0]["outcome"] == "floorplan" and not (repo / "floorplan").exists()
    assert sh(repo, "git", "status", "--porcelain") == ""   # the worktree stays as it was
    drawing = json.loads((run / floormap.DELTA).read_text())
    assert drawing["recent"]["tasks"][0]["request"] == "Neues Menü"
    assert {i["ref"]: i["change"] for i in drawing["recent"]["tasks"][0]["items"]} == {"ui/Pause.cs": "new", "Sim.cs": "changed"}
    html = floormap.page(skill, bundle(repo), drawing).decode()
    assert "Spiel" in html and "ui/Pause.cs" in html
    assert "Grundriss" in "\n".join(floormap.section(run))


def test_without_a_map_or_node_a_ticket_goes_on(tmp_path, monkeypatch):
    store = Store(tmp_path / "state")
    ctx = SimpleNamespace(cfg=cfg(), workdir=tmp_path / "work", store=store, status={"base_sha": "x"}, run_dir=tmp_path, ticket=1)
    assert floormap.overlay(ctx, tmp_path) is None
    monkeypatch.setattr(floormap, "node", lambda: "")
    assert floormap.overlay(ctx, tmp_path) is None
    assert floormap.tick(SimpleNamespace(cfg=cfg(), repo=tmp_path, workdir=tmp_path, store=store, status={})) == "no node"


def test_the_board_says_when_there_is_no_map_yet(monkeypatch, tmp_path):
    def raw(repo, branch, path):
        raise FileNotFoundError(path)
    monkeypatch.setattr(board, "state_raw", raw)
    maps = board.Maps("factory-state", tmp_path, lambda: cfg())
    with pytest.raises(FileNotFoundError, match="noch keine Karte"):
        maps.page("o/game")
