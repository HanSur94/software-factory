"""Pictures as evidence: what a change looks like, next to the ticket and in the pull request.

Three sources, collected after green checks and before the review:

- the agents: the implementer saves screenshots into the ticket's evidence inbox (a folder
  outside the checkout, so nothing of it is committed), named before-<what> / after-<what>
  where it can show both;
- the repository: pictures the change itself adds or alters (a project that keeps evidence
  in docs/evidence, for instance);
- the project's capture command (evidence.capture in .factory/profile.yaml): run once on
  the base and after every green round on the ticket branch, it writes the same views of
  both into $FACTORY_EVIDENCE_OUT, so every ticket gets an evenly framed before/after pair.

The pictures are shrunk and stored in the ticket's folder on the state branch (evidence/ and
evidence.json), never in the product repository. The board shows them, the pull request
embeds them, and reviewer and judge are asked to look at them (evidence.reviewer_sees,
evidence.judge_sees). Evidence never stops a ticket: whatever fails here is a note.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

from . import events, gitops, warm
from .context import Ctx

IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
DEFAULTS = {"enabled": True, "max_images": 12, "max_px": 1600, "max_kb": 600, "reviewer_sees": True, "judge_sees": True}
SIDES = re.compile(r"^(?:(before|after|vorher|nachher)[-_ ](.+)|(.+?)[-_ ](before|after|vorher|nachher))$", re.I)
SIDE = {"before": "before", "vorher": "before", "after": "after", "nachher": "after"}
WRITERS = ("implementer",)   # roles that may save pictures


def conf(cfg: dict) -> dict:
    return {**DEFAULTS, **(cfg.get("evidence") or {})}


def enabled(ctx: Ctx) -> bool:
    return bool(conf(ctx.cfg)["enabled"])


def inbox(ctx: Ctx) -> Path:
    """Where the agents put their pictures: outside the checkout, so they are never committed."""
    path = Path(ctx.workdir) / "evidence" / str(ctx.ticket)
    path.mkdir(parents=True, exist_ok=True)
    return path


def dirs(ctx: Ctx, role: str) -> list[Path]:
    return [inbox(ctx)] if enabled(ctx) and role in WRITERS else []


def env(ctx: Ctx, role: str) -> dict:
    """For the project's own tools: where a run of this role saves pictures."""
    return {"FACTORY_EVIDENCE_DIR": str(inbox(ctx))} if enabled(ctx) and role in WRITERS else {}


def manifest(run_dir: Path) -> dict:
    try:
        return json.loads((run_dir / "evidence.json").read_text())
    except (OSError, ValueError):
        return {}


def note(ctx: Ctx, role: str) -> str:
    """The part of a station's prompt about pictures: where to save them, or which ones to look at."""
    c = conf(ctx.cfg)
    if not c["enabled"]:
        return ""
    if role in WRITERS:
        return ("\n## Pictures as evidence\n\n"
                f"If this change alters something one can see (a scene, the HUD, a menu, a chart, a page), save "
                f"screenshots that show it into `{inbox(ctx)}` (PNG or JPEG). Where you can show the state before the "
                "change too, name them as a pair: `before-<what>.png` and `after-<what>.png`. They go to the ticket "
                "and the pull request. The project's own tools find this folder in `$FACTORY_EVIDENCE_DIR`. Do not commit "
                "pictures to the repository unless the ticket asks for files there. "
                "A change nobody can see needs no pictures.\n")
    wanted = {"reviewer": c["reviewer_sees"], "judge": c["judge_sees"]}.get(role)
    items = manifest(ctx.run_dir).get("items") or []
    if not wanted or not items:
        return ""
    folder = ctx.run_dir / "evidence"
    lines = [f"- `{folder / i['file']}`: {describe(i)}" for i in items]
    return ("\n## Pictures of the change\n\n"
            "The factory collected these pictures. Open each one with the Read tool and check that it shows what "
            "the ticket asks for. Pictures saved by the implementer show what it chose to show; pictures from the "
            "project's capture command show the same views of the base and of this change. A picture that "
            "contradicts the ticket is a finding; a picture never proves behaviour that the code does not have.\n\n"
            + "\n".join(lines) + "\n")


def describe(item: dict) -> str:
    source = {"agent": "saved by the implementer", "repo": f"changed in the repository ({item.get('path', '')})",
              "capture": "from the capture command"}.get(item.get("source"), "")
    side = {"before": "before the change", "after": "after the change"}.get(item.get("side") or "", "")
    return ", ".join(x for x in (item.get("title"), side, source) if x)


def _pair(stem: str) -> tuple[str | None, str]:
    m = SIDES.match(stem)
    if not m:
        return None, stem
    if m.group(1):
        return SIDE[m.group(1).lower()], m.group(2)
    return SIDE[m.group(4).lower()], m.group(3)


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")[:60] or "bild"


def shrink(src: Path, dst_stem: Path, max_px: int, max_kb: int) -> Path | None:
    """A copy of the picture no wider or taller than max_px; a large PNG becomes a JPEG. None when it cannot be read."""
    suffix = src.suffix.lower()
    if suffix == ".gif":   # an animation stays one: copied when small enough
        if src.stat().st_size > 4 * max_kb * 1024:
            return None
        dst = dst_stem.with_suffix(".gif")
        shutil.copyfile(src, dst)
        return dst
    sips = shutil.which("sips")
    if sips:
        dst = dst_stem.with_suffix(".png" if suffix == ".png" else ".jpg")
        args = [sips, "-Z", str(max_px)] + ([] if dst.suffix == ".png" else ["-s", "format", "jpeg", "-s", "formatOptions", "82"])
        if subprocess.run([*args, str(src), "--out", str(dst)], capture_output=True, timeout=60).returncode == 0 and dst.exists():
            if dst.suffix == ".png" and dst.stat().st_size > max_kb * 1024:
                jpg = dst.with_suffix(".jpg")
                if subprocess.run([sips, "-s", "format", "jpeg", "-s", "formatOptions", "82", str(dst), "--out", str(jpg)],
                                  capture_output=True, timeout=60).returncode == 0 and jpg.exists():
                    dst.unlink()
                    dst = jpg
            return dst
    try:
        from PIL import Image  # noqa: PLC0415 - optional
        with Image.open(src) as img:
            img.thumbnail((max_px, max_px))
            big = img.mode in ("RGBA", "LA", "P") and suffix == ".png"
            dst = dst_stem.with_suffix(".png" if big else ".jpg")
            (img if big else img.convert("RGB")).save(dst, **({} if big else {"quality": 82}))
            return dst
    except Exception:   # no Pillow, or not a picture it reads
        pass
    if src.stat().st_size <= 2 * max_kb * 1024:   # nothing to shrink with: small pictures go as they are
        dst = dst_stem.with_suffix(suffix)
        shutil.copyfile(src, dst)
        return dst
    return None


def _pictures(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in IMAGES) if folder.is_dir() else []


def candidates(ctx: Ctx, wt: Path, captured: dict | None = None) -> list[dict]:
    """Every picture of this round, the most telling first: capture pairs, then the agents', then the repository's."""
    found = []
    for side in ("before", "after"):
        folder = (captured or {}).get(side)
        if not folder:   # no capture this round; Path("") would be the working directory, the whole checkout
            continue
        for p in _pictures(Path(folder)):
            found.append({"path": p, "source": "capture", "side": side, "key": p.stem, "title": p.stem})
    for p in _pictures(inbox(ctx)):
        side, key = _pair(p.stem)
        found.append({"path": p, "source": "agent", "side": side, "key": key, "title": key})
    base = ctx.status.get("base_sha")
    if base:
        try:
            changed = gitops.changed_files(wt, base)
        except gitops.GitError:
            changed = []
        for rel in changed:
            p = wt / rel
            if p.suffix.lower() in IMAGES and p.is_file():
                side, key = _pair(p.stem)
                found.append({"path": p, "source": "repo", "side": side, "key": key, "title": key, "rel": rel})
    # Within each source a pair stays together, before first; the sources keep their order.
    rank = {"capture": 0, "agent": 1, "repo": 2}
    side = {"before": 0, "after": 1, None: 2}
    return sorted(found, key=lambda f: (rank[f["source"]], f["key"].lower(), side[f["side"]]))


def collect(ctx: Ctx, wt: Path, captured: dict | None = None) -> dict:
    """This round's pictures, shrunk, into the ticket's folder on the state branch. Replaces the last round's."""
    c = conf(ctx.cfg)
    out = ctx.run_dir / "evidence"
    shutil.rmtree(out, ignore_errors=True)
    found = candidates(ctx, wt, captured)
    if not found:
        (ctx.run_dir / "evidence.json").unlink(missing_ok=True)
        return {}
    out.mkdir(parents=True)
    items, skipped = [], 0
    for n, pic in enumerate(found):
        if len(items) >= int(c["max_images"]):
            skipped += len(found) - n
            break
        side = f"-{pic['side']}" if pic["side"] else ""
        dst = shrink(pic["path"], out / f"{len(items) + 1:02d}-{_slug(pic['key'])}{side}", int(c["max_px"]), int(c["max_kb"]))
        if dst is None:
            skipped += 1
            continue
        data = dst.read_bytes()
        item = {"file": dst.name, "title": pic["title"], "source": pic["source"], "side": pic["side"],
                "pair": f"{pic['source']}:{pic['key']}" if pic["side"] else None,
                "bytes": len(data), "hash": hashlib.sha1(data).hexdigest()[:12]}
        if pic.get("rel"):
            item["path"] = pic["rel"]
        items.append(item)
    record = {"round": ctx.status.get("round", 1), "rework_round": ctx.status.get("rework_rounds", 0),
              "ts": events.now(), "items": items, "skipped": skipped}
    (ctx.run_dir / "evidence.json").write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n")
    ctx.emit("evidence", "collected", outcome="ok", detail=f"{len(items)} Bilder" + (f", {skipped} ausgelassen" if skipped else ""))
    return record


# ---- the project's capture command: the same views of the base and of the change ---------------------------

def capture_command(ctx: Ctx) -> str:
    return str((ctx.profile.get("evidence") or {}).get("capture") or "")


def _run_capture(ctx: Ctx, cwd: Path, out: Path, name: str) -> bool:
    from . import checks   # noqa: PLC0415 - checks imports the agent, which imports this module's users
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    minutes = float((ctx.profile.get("evidence") or {}).get("timeout_minutes", 20))
    log_dir = ctx.run_dir / "checks"
    log_dir.mkdir(parents=True, exist_ok=True)
    result = checks._run(ctx, name, capture_command(ctx), cwd, minutes, log_dir,
                         env={"FACTORY_EVIDENCE_OUT": str(out), "FACTORY_TICKET_FILE": str(ctx.run_dir / "ticket.json")})
    ctx.emit("evidence", "capture", outcome="pass" if result.ok else "fail", detail=name, duration_s=result.seconds)
    return result.ok and bool(_pictures(out))


def capture(ctx: Ctx, wt: Path) -> dict:
    """Run the capture command on the change, and once per ticket on its base. {"before": dir, "after": dir}."""
    if not capture_command(ctx):
        return {}
    root = Path(ctx.workdir) / "evidence-capture" / str(ctx.ticket)
    got = {}
    if _run_capture(ctx, wt, root / "after", "capture-after"):
        got["after"] = root / "after"
    base, before = ctx.status.get("base_sha"), root / "before"
    stamp = root / "before.sha"
    if base and (ctx.profile.get("evidence") or {}).get("before", True):
        if stamp.exists() and stamp.read_text().strip() == base and _pictures(before):
            got["before"] = before
        else:
            tree = root / "base-checkout"
            try:
                gitops.git(ctx.repo, "worktree", "remove", "--force", str(tree), check=False)
                shutil.rmtree(tree, ignore_errors=True)
                gitops.git(ctx.repo, "worktree", "add", "--quiet", "--detach", str(tree), base)
                warm.seed(ctx.profile, ctx.repo, tree)
                if _run_capture(ctx, tree, before, "capture-before"):
                    stamp.write_text(base + "\n")
                    got["before"] = before
            except gitops.GitError as exc:
                ctx.note(f"Vorher-Bilder nicht aufgenommen: {exc}"[:300])
            finally:
                gitops.git(ctx.repo, "worktree", "remove", "--force", str(tree), check=False)
                shutil.rmtree(tree, ignore_errors=True)
    return got


def gather(ctx: Ctx, wt: Path) -> dict:
    """Capture and collect; whatever goes wrong becomes a note, never a stopped ticket."""
    if not enabled(ctx):
        return {}
    try:
        return collect(ctx, wt, capture(ctx, wt))
    except Exception as exc:   # evidence is a help for people, not a gate
        ctx.note(f"Bild-Belege nicht gesammelt: {exc}"[:300])
        return {}


# ---- the judge's copy: it runs in its own job, without the state checkout -------------------------------

def for_judge(ctx: Ctx, product: Path, dest: Path, source: Path | None = None) -> dict:
    """Copy the ticket's pictures into the judge's private folder: from a local folder, or from the state branch."""
    if not enabled(ctx) or not conf(ctx.cfg)["judge_sees"]:
        return {}
    try:
        if source is not None:
            record = manifest(source)
            folder = source / "evidence"
            if not record:
                return {}
            shutil.copytree(folder, dest / "evidence", dirs_exist_ok=True)
        else:
            branch = ctx.cfg["storage"]["branch"]
            gitops.git_net(product, "fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
            prefix = f"origin/{branch}:tickets/{ctx.ticket}"
            record = json.loads(gitops.git(product, "show", f"{prefix}/evidence.json"))
            (dest / "evidence").mkdir(parents=True, exist_ok=True)
            for item in record.get("items") or []:
                if not re.fullmatch(r"[\w.-]+", item["file"]):
                    continue
                data = subprocess.run(["git", "show", f"{prefix}/evidence/{item['file']}"], cwd=str(product),
                                      capture_output=True, timeout=60)
                if data.returncode == 0:
                    (dest / "evidence" / item["file"]).write_bytes(data.stdout)
        (dest / "evidence.json").write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n")
        return record
    except (gitops.GitError, OSError, ValueError, subprocess.SubprocessError):
        return {}


# ---- the pull request ---------------------------------------------------------------------------------------

def pr_section(ctx: Ctx) -> list[str]:
    """Markdown for the pull request: before/after pairs side by side, then the rest."""
    record = manifest(ctx.run_dir)
    items = record.get("items") or []
    repo = getattr(ctx.host, "repo", None)
    if not items:
        return []
    branch = ctx.cfg["storage"]["branch"]

    def img(i: dict) -> str:
        if not repo:
            return f"`{i['file']}`"
        url = f"https://github.com/{repo}/blob/{branch}/tickets/{ctx.ticket}/evidence/{i['file']}?raw=true"
        return f'<img src="{url}" alt="{i["title"]}" width="420">'

    lines, done = ["", f"**Belege** ({len(items)} Bilder, auch im Board)", ""], set()
    pairs = [i for i in items if i.get("pair")]
    for i in pairs:
        if i["pair"] in done:
            continue
        before = next((x for x in pairs if x["pair"] == i["pair"] and x["side"] == "before"), None)
        after = next((x for x in pairs if x["pair"] == i["pair"] and x["side"] == "after"), None)
        if before and after:
            done.add(i["pair"])
            lines += [f"| {i['title']}: vorher | nachher |", "|---|---|", f"| {img(before)} | {img(after)} |", ""]
    for i in items:
        if i.get("pair") not in done:
            lines += [f"{describe_de(i)}", "", img(i), ""]
    return lines


def describe_de(item: dict) -> str:
    source = {"agent": "vom Implementierer", "repo": f"im Repo geändert (`{item.get('path', '')}`)",
              "capture": "vom Aufnahme-Befehl"}.get(item.get("source"), "")
    side = {"before": "vorher", "after": "nachher"}.get(item.get("side") or "", "")
    return f"*{item['title']}*" + (f" · {side}" if side else "") + (f" · {source}" if source else "")
