"""The floor plan of a project: a map of its parts in plain words, and what a ticket changed on it.

Who does not read code cannot tell from a diff what the factory built. floorplan (github.com/haydenschmitty/floorplan,
a pinned plugin like the skill packs) draws a codebase as parts, files and the links between them, each described in
plain words. The factory keeps such a map of each project's base branch and lays every ticket over it:

- the cartographer (role cartographer) draws the map in the orchestrator's sweep once the base branch has moved.
  After the first map floorplan's own refresh does most of it without a model; the agent only describes new files.
  The map lives on the state branch as one bundle (map/floorplan.json) with a small map/meta.json beside it.
- after green checks a ticket scans its worktree with the same map and records what it touched: which parts, which
  files, new or changed, and what each file does (tickets/N/floorplan.json). No model is involved.
- the pull request and the closing report show the touched parts as a Mermaid diagram; the board draws the whole map
  with the ticket's files marked (floorplan's "recent work"), from the bundle and the ticket's record.

floorplan's scripts need Node 18+. Where it or the map is missing, everything here does nothing: the map is a help,
never a step a ticket waits for.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import gitops, plugins

PLUGIN = "floorplan"
BUNDLE = "map/floorplan.json"
META = "map/meta.json"
RECORD = "floorplan.json"          # in a ticket's folder: the touched parts, small, read with every board refresh
DELTA = "floorplan-delta.json"     # beside it: what the board needs to draw the ticket's map, read only then
KEEP = ("architecture.json", ".cache/scan.json")   # plus descriptions/*.json: what a map needs to be drawn again
UNATTENDED = {"FLOORPLAN_UNATTENDED": "1", "FLOORPLAN_AGENT": "none"}


# ---- floorplan itself ---------------------------------------------------------------------------------------------

def settings(cfg: dict) -> dict:
    m = cfg.get("map") or {}
    return {"enabled": bool(m.get("enabled")), "every_minutes": float(m.get("every_minutes") or 60),
            "max_parts": int(m.get("max_parts") or 12)}


def node() -> str:
    return shutil.which("node") or ""


def skill_dir(cfg: dict, workdir: Path) -> Path | None:
    """The pinned floorplan skill, fetched when needed; None when it is switched off or cannot be had."""
    spec = (cfg.get("plugins") or {}).get(PLUGIN) or {}
    if not spec.get("enabled"):
        return None
    root = plugins.plugin_root(Path(workdir)) / PLUGIN
    if not (root / "skills" / "floorplan" / "scripts" / "build.mjs").exists():
        try:
            plugins.fetch({"plugins": {PLUGIN: spec}}, Path(workdir))
        except (subprocess.CalledProcessError, OSError):
            return None
    d = root / "skills" / "floorplan"
    return d if (d / "scripts" / "build.mjs").exists() else None


def script(skill: Path, name: str, root: Path, *args: str, timeout: float = 600) -> dict:
    """Run one of floorplan's scripts with --json; its one JSON object, or {"ok": False, "errors": [...]}."""
    exe = node()
    if not exe:
        return {"ok": False, "errors": ["node is not installed"]}
    try:
        proc = subprocess.run([exe, str(skill / "scripts" / f"{name}.mjs"), str(root), *args, "--json"], text=True,
                              capture_output=True, timeout=timeout, env={**os.environ, **UNATTENDED})
        return json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return {"ok": False, "errors": [f"{name}.mjs: {type(exc).__name__}"]}


def commands(skill: Path) -> list[str]:
    """What the cartographer may start: floorplan's scripts, each by its full path."""
    return [f"node {skill / 'scripts' / n}.mjs" for n in ("scan", "build", "describe", "refresh", "recent", "status")]


# ---- the bundle on the state branch ------------------------------------------------------------------------------

def pack(folder: Path) -> dict:
    """The files a map is drawn from, as one object: architecture, descriptions and floorplan's scan."""
    out = {"architecture": json.loads((folder / "architecture.json").read_text()),
           "scan": json.loads((folder / ".cache" / "scan.json").read_text()), "descriptions": {}}
    for f in sorted((folder / "descriptions").glob("*.json")):
        out["descriptions"][f.stem] = json.loads(f.read_text())
    return out


def unpack(bundle: dict, folder: Path, scan: dict | None = None) -> None:
    (folder / ".cache").mkdir(parents=True, exist_ok=True)
    (folder / "descriptions").mkdir(exist_ok=True)
    (folder / "architecture.json").write_text(json.dumps(bundle["architecture"], ensure_ascii=False, indent=1))
    (folder / ".cache" / "scan.json").write_text(json.dumps(scan or bundle["scan"], ensure_ascii=False))
    for key, value in (bundle.get("descriptions") or {}).items():
        (folder / "descriptions" / f"{key}.json").write_text(json.dumps(value, ensure_ascii=False, indent=1))


def read_bundle(state_root: Path) -> dict | None:
    p = Path(state_root) / BUNDLE
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except ValueError:
        return None


# ---- the cartographer ----------------------------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tick(ctx) -> str:
    """One look from the orchestrator: draw or refresh the base branch's map when it has moved. Returns what happened."""
    s = settings(ctx.cfg)
    if not s["enabled"]:
        return "off"
    state_root = Path(ctx.store.tickets_dir()).parent
    meta_file = state_root / META
    meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
    repo = Path(ctx.repo)
    head = gitops.git(repo, "rev-parse", "HEAD", check=False).strip()
    if head and meta.get("head") == head:
        return "current"
    if meta.get("error") and time.time() - float(meta.get("tried_at") or 0) < s["every_minutes"] * 60:
        return "recent"   # the last try failed: not again in every sweep
    if not node():
        return "no node"
    skill = skill_dir(ctx.cfg, ctx.workdir)
    if skill is None:
        return "no floorplan"
    folder = repo / "floorplan"
    shutil.rmtree(folder, ignore_errors=True)
    bundle = read_bundle(state_root)
    meta.update(tried=head, tried_at=time.time())
    cost = 0.0
    try:
        if bundle:
            unpack(bundle, folder)
            refreshed = script(skill, "refresh", repo)
            todo = [n.get("code") for n in refreshed.get("next") or []]
            needs_agent = not refreshed.get("ok", False) or any(c not in ("recent_work",) for c in todo)
            report = refreshed.get("report") or "\n".join(refreshed.get("errors") or [])
        else:
            needs_agent, report = True, ""
        if needs_agent:
            cost = _draw(ctx, skill, repo, first=not bundle, report=report)
        built = script(skill, "build", repo)
        if not built.get("ok"):
            raise RuntimeError("; ".join(built.get("errors") or ["build failed"])[:400])
        new = pack(folder)
        (state_root / "map").mkdir(exist_ok=True)
        (state_root / BUNDLE).write_text(json.dumps(new, ensure_ascii=False, indent=0) + "\n")
        nodes = built.get("nodes") or []
        meta.update(head=head, built=_now(), files=(built.get("files") or {}).get("total"), parts=len(nodes),
                    cost_usd=round(cost + float(meta.get("cost_usd") or 0), 2), last_cost_usd=round(cost, 2),
                    agent=needs_agent, error=None,
                    complete=bool((built.get("descriptions") or {}).get("complete")))
        meta_file.write_text(json.dumps(meta, indent=1) + "\n")
        ctx.store.save(f"map: {'drawn' if not bundle else 'refreshed'} at {head[:8]}")
        return f"{'drawn' if not bundle else 'refreshed'}{' with agent' if needs_agent else ''}"
    except Exception as exc:   # the old map stays; the next sweep tries again after every_minutes
        meta.update(error=str(exc)[:400])
        (state_root / "map").mkdir(exist_ok=True)
        meta_file.write_text(json.dumps(meta, indent=1) + "\n")
        try:
            ctx.store.save("map: failed")
        except Exception:
            pass
        return f"failed: {str(exc)[:120]}"
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _draw(ctx, skill: Path, repo: Path, first: bool, report: str) -> float:
    """The agent part: a first map, or describing what refresh found new. Returns what it cost."""
    from . import stations   # stations imports a lot; only the sweep needs it here
    state_root = Path(ctx.store.tickets_dir()).parent
    run = state_root / "orchestrator" / "map" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run.mkdir(parents=True, exist_ok=True)
    saved = (getattr(ctx, "run_dir", None), getattr(ctx, "log_dir", None), ctx.profile)
    ctx.run_dir, ctx.log_dir = run, None
    ctx.profile = {**ctx.profile, "agent_commands": commands(skill)}   # floorplan's scripts, nothing else
    values = {"mode": "first" if first else "update", "scripts": str(skill / "scripts"), "skill": str(skill),
              "report": report or "(keine Karte bisher)", "commands": "\n".join(f"- `{c} …`" for c in commands(skill))}
    before = float(ctx.status.get("spent_usd", 0.0)) if hasattr(ctx, "status") else 0.0
    try:
        stations.run_station(ctx, "cartographer", ctx.cfg["roles"]["cartographer"]["prompt"], values, repo, None,
                             lambda p: None, readonly=False)
    finally:
        ctx.run_dir, ctx.log_dir, ctx.profile = saved
        for old in sorted(d for d in run.parent.iterdir() if d.is_dir())[:-5]:
            shutil.rmtree(old, ignore_errors=True)
    return max(0.0, float(ctx.status.get("spent_usd", 0.0)) - before) if hasattr(ctx, "status") else 0.0


# ---- a ticket on the map -------------------------------------------------------------------------------------------

def _changes(wt: Path, base: str) -> list[dict]:
    out = []
    numbers = {}
    for line in gitops.git(wt, "diff", "--numstat", "-M", f"{base}...HEAD", check=False).splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            path = parts[2].split(" => ")[-1].strip("{}") if "=>" in parts[2] else parts[2]
            numbers[path] = (int(parts[0]) if parts[0].isdigit() else 0, int(parts[1]) if parts[1].isdigit() else 0)
    for line in gitops.git(wt, "diff", "--name-status", "-M", f"{base}...HEAD", check=False).splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        code, path = parts[0][0], parts[-1]
        status = {"A": "new", "D": "deleted", "R": "moved"}.get(code, "changed")
        adds, dels = numbers.get(path, (0, 0))
        out.append({"path": path, "status": status, "add": adds, "del": dels, **({"from": parts[1]} if code == "R" else {})})
    return out


def overlay(ctx, wt: Path) -> dict | None:
    """Lay this ticket's changes over the project's map; the record goes to the ticket's folder. Never raises."""
    try:
        if not settings(ctx.cfg)["enabled"] or not node() or getattr(ctx, "store", None) is None:
            return None
        bundle = read_bundle(Path(ctx.store.tickets_dir()).parent)
        skill = skill_dir(ctx.cfg, ctx.workdir) if bundle else None
        if not bundle or skill is None:
            return None
        changes = _changes(wt, ctx.status["base_sha"])
        if not changes:
            return None
        with tempfile.TemporaryDirectory(prefix="factory-map-") as tmp:
            # floorplan writes only into <root>/floorplan, and a copy keeps the ticket's worktree untouched.
            folder = Path(tmp) / "floorplan"
            scanned = _scan_ticket(skill, wt, bundle, Path(tmp))
            if scanned is None:
                return None
            unpack(bundle, folder, scanned)
            recent = recent_work(ctx, changes)
            (folder / "recent-work.json").write_text(json.dumps(recent, ensure_ascii=False))
            built = script(skill, "build", Path(tmp))
            graph_file = folder / "graph.json"
            if not built.get("ok") or not graph_file.exists():
                return None
            graph = json.loads(graph_file.read_text())
        record = touched(graph, changes, settings(ctx.cfg)["max_parts"])
        record["base"] = ctx.status["base_sha"]
        (ctx.run_dir / RECORD).write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n")
        (ctx.run_dir / DELTA).write_text(json.dumps({"base": record["base"], "recent": recent,
                                                     "delta": delta(bundle["scan"], scanned, changes)}, ensure_ascii=False) + "\n")
        ctx.emit("pipeline", "pipeline", outcome="floorplan",
                 detail=f"{len(record['parts'])} Teile berührt, {sum(len(p['files']) for p in record['parts'])} Dateien")
        return record
    except Exception as exc:   # the map is a help: a ticket never stops for it
        print(f"[factory] floor plan of the ticket not drawn: {exc}", flush=True)
        return None


def _scan_ticket(skill: Path, wt: Path, bundle: dict, tmp: Path) -> dict | None:
    """floorplan's scan of the ticket's worktree, with the map's own settings (include, ignore) applied."""
    probe = wt / "floorplan"
    if probe.exists():   # never touch a folder the project itself has
        return None
    try:
        probe.mkdir()
        (probe / "architecture.json").write_text(json.dumps(bundle["architecture"], ensure_ascii=False))
        (probe / ".gitignore").write_text("*\n")
        result = script(skill, "scan", wt)
        scan_file = probe / ".cache" / "scan.json"
        return json.loads(scan_file.read_text()) if result.get("ok", True) and scan_file.exists() else None
    finally:
        shutil.rmtree(probe, ignore_errors=True)


def recent_work(ctx, changes: list[dict]) -> dict:
    """floorplan's "recent work" for the ticket: its files get a New or Changed tag on the map."""
    try:
        title = json.loads((ctx.run_dir / "ticket.json").read_text()).get("title") or ""
    except (OSError, ValueError):
        title = ""
    title = str(title or f"Ticket #{ctx.ticket}")
    items = [{"ref": c["path"], "change": "new" if c["status"] == "new" else "changed",
              "note": f"{'Neu' if c['status'] == 'new' else 'Geändert'}: +{c['add']} / −{c['del']} Zeilen"}
             for c in changes if c["status"] != "deleted"]
    gone = [c["path"] for c in changes if c["status"] == "deleted"]
    summary = f"{len(items)} Dateien neu oder geändert" + (f", {len(gone)} gelöscht" if gone else "") + "."
    return {"updatedAt": _now(), "tasks": [{"title": f"#{ctx.ticket}"[:40], "request": title[:200], "summary": summary,
                                            "items": items}]}


def touched(graph: dict, changes: list[dict], max_parts: int = 12) -> dict:
    """Which parts of the map the changes fall into, with each file's description and the links between those parts."""
    nodes = {n["id"]: n for n in graph.get("nodes") or []}
    files = {f["p"]: f for f in graph.get("files") or []}
    parts: dict[str, dict] = {}
    off = []
    for c in changes:
        f = files.get(c["path"])
        if not f or c["status"] == "deleted":
            off.append(c)
            continue
        part = parts.setdefault(f["n"], {"id": f["n"], "label": nodes.get(f["n"], {}).get("label", f["n"]),
                                        "path": _trail(nodes, f["n"]), "summary": nodes.get(f["n"], {}).get("summary", ""),
                                        "files": []})
        part["files"].append({**c, "name": f.get("name") or "", "d": f.get("d") or ""})
    owner = {f["p"]: f["n"] for f in graph.get("files") or []}
    mine = {c["path"] for c in changes}
    links: dict[tuple, int] = {}
    for link in graph.get("links") or []:
        a, b = str(link.get("a", "")), str(link.get("b", ""))
        if not (a.startswith("f:") and b.startswith("f:")):
            continue
        a, b = a[2:], b[2:]
        if a not in mine and b not in mine:
            continue
        na, nb = owner.get(a), owner.get(b)
        if na and nb and na != nb and na in parts and nb in parts:
            links[(na, nb)] = links.get((na, nb), 0) + int(link.get("w") or 1)
    ordered = sorted(parts.values(), key=lambda p: -sum(f["add"] + f["del"] for f in p["files"]))
    return {"parts": ordered[:max_parts], "more_parts": max(0, len(ordered) - max_parts),
            "off_map": off, "links": [[a, b, w] for (a, b), w in sorted(links.items(), key=lambda kv: -kv[1])][:20],
            "nodes": {i: {"label": n.get("label", i), "parent": n.get("parent")} for i, n in nodes.items()}}


def _trail(nodes: dict, nid: str) -> list[str]:
    out, seen = [], set()
    while nid and nid in nodes and nid not in seen:
        seen.add(nid)
        out.insert(0, nodes[nid].get("label", nid))
        nid = nodes[nid].get("parent")
    return out


def delta(base_scan: dict, scan: dict, changes: list[dict]) -> dict:
    """What the board needs to draw the ticket's map from the base map: the scan entries of the changed files."""
    paths = {c["path"] for c in changes if c["status"] != "deleted"}
    gone = {c["path"] for c in changes if c["status"] == "deleted"} | {c["from"] for c in changes if c.get("from")}
    return {"files": [f for f in scan.get("files") or [] if f.get("path") in paths],
            "edges": [e for e in scan.get("edges") or [] if e.get("from") in paths],
            "extEdges": [e for e in scan.get("extEdges") or [] if e.get("from") in paths],
            "drop": sorted(paths | gone)}


def apply_delta(base_scan: dict, d: dict) -> dict:
    drop = set(d.get("drop") or [])
    out = dict(base_scan)
    out["files"] = [f for f in base_scan.get("files") or [] if f.get("path") not in drop] + list(d.get("files") or [])
    out["edges"] = [e for e in base_scan.get("edges") or [] if e.get("from") not in drop] + list(d.get("edges") or [])
    out["extEdges"] = [e for e in base_scan.get("extEdges") or [] if e.get("from") not in drop] + list(d.get("extEdges") or [])
    return out


# ---- Markdown for the pull request and the closing report -------------------------------------------------------

def _mid(nid: str) -> str:
    return "n_" + re.sub(r"[^A-Za-z0-9_]", "_", nid)


def _label(text: str) -> str:
    return re.sub(r'["<>`]', "", str(text))[:60]


def mermaid(record: dict) -> list[str]:
    """The touched parts as a flowchart, nested in their parents; green has new files, amber only changed ones."""
    nodes = record.get("nodes") or {}
    parts = record.get("parts") or []
    ids = {p["id"] for p in parts}
    children: dict[str | None, list[str]] = {}
    shown = set(ids)
    for nid in list(ids):   # every ancestor of a touched part frames it
        parent = (nodes.get(nid) or {}).get("parent")
        while parent:
            shown.add(parent)
            parent = (nodes.get(parent) or {}).get("parent")
    for nid in shown:
        children.setdefault((nodes.get(nid) or {}).get("parent"), []).append(nid)
    by_id = {p["id"]: p for p in parts}
    lines = ["```mermaid", "flowchart LR"]

    def leaf(nid: str, indent: str) -> None:
        p = by_id[nid]
        new = sum(1 for f in p["files"] if f["status"] == "new")
        changed = len(p["files"]) - new
        counts = " · ".join(x for x in (f"{new} neu" if new else "", f"{changed} geändert" if changed else "") if x)
        lines.append(f'{indent}{_mid(nid)}["{_label(p["label"])}<br/>{counts}"]:::{"new" if new else "changed"}')

    def walk(parent: str | None, indent: str) -> None:
        for nid in sorted(children.get(parent, []), key=lambda i: (nodes.get(i) or {}).get("label", i)):
            if children.get(nid):
                lines.append(f'{indent}subgraph {_mid(nid)}_g["{_label((nodes.get(nid) or {}).get("label", nid))}"]')
                if nid in ids:
                    leaf(nid, indent + "  ")
                walk(nid, indent + "  ")
                lines.append(f"{indent}end")
            elif nid in ids:
                leaf(nid, indent)
    walk(None, "  ")
    for a, b, _ in record.get("links") or []:
        if a in ids and b in ids:
            lines.append(f"  {_mid(a)} --> {_mid(b)}")
    lines += ["  classDef new fill:#dcfce7,stroke:#16a34a,color:#14532d",
              "  classDef changed fill:#fef3c7,stroke:#d97706,color:#78350f", "```"]
    return lines


def section(run_dir: Path, heading: str = "**Grundriss**", map_link: str = "") -> list[str]:
    """Markdown: the diagram, then each touched part with its files and what each does. Empty without a record."""
    try:
        record = json.loads((Path(run_dir) / RECORD).read_text())
    except (OSError, ValueError):
        return []
    parts = record.get("parts") or []
    if not parts:
        return []
    out = [heading, "", "Wo im Projekt diese Änderung liegt: grün sind Teile mit neuen Dateien, gelb solche mit geänderten.",
           "", *mermaid(record), ""]
    for p in parts:
        out.append(f"- **{' › '.join(p['path']) or p['label']}**")
        for f in p["files"][:8]:
            what = f" – {f['d']}" if f.get("d") else ""
            word = "neu" if f["status"] == "new" else "verschoben" if f["status"] == "moved" else "geändert"
            out.append(f"  - {word}: `{f['path']}` (+{f['add']} / −{f['del']}){what}")
        if len(p["files"]) > 8:
            out.append(f"  - … und {len(p['files']) - 8} weitere")
    if record.get("more_parts"):
        out.append(f"- … und {record['more_parts']} weitere Teile")
    rest = [c for c in record.get("off_map") or []]
    if rest:
        gone = [c["path"] for c in rest if c["status"] == "deleted"]
        other = [c["path"] for c in rest if c["status"] != "deleted"]
        if gone:
            out.append(f"- gelöscht: {', '.join(f'`{g}`' for g in gone[:8])}{' …' if len(gone) > 8 else ''}")
        if other:
            out.append(f"- nicht auf der Karte: {', '.join(f'`{g}`' for g in other[:8])}{' …' if len(other) > 8 else ''}")
    if map_link:
        out += ["", map_link]
    return out + [""]


def touched_count(run_dir: Path) -> int:
    try:
        return len(json.loads((Path(run_dir) / RECORD).read_text()).get("parts") or [])
    except (OSError, ValueError):
        return 0


# ---- the board draws a map ---------------------------------------------------------------------------------------

def page(skill: Path, bundle: dict, record: dict | None = None, timeout: float = 120) -> bytes:
    """floorplan's page for the map, with a ticket's files marked when a record is given."""
    with tempfile.TemporaryDirectory(prefix="factory-map-") as tmp:
        root = Path(tmp)
        folder = root / "floorplan"
        scan = apply_delta(bundle["scan"], record["delta"]) if record and record.get("delta") else bundle["scan"]
        unpack(bundle, folder, scan)
        (folder / "recent-work.json").write_text(json.dumps((record or {}).get("recent") or {"tasks": []}, ensure_ascii=False))
        built = script(skill, "build", root, timeout=timeout)
        html = folder / "index.html"
        if not built.get("ok") or not html.exists():
            raise RuntimeError("; ".join(built.get("errors") or ["floorplan konnte die Karte nicht zeichnen"])[:300])
        return html.read_bytes()

