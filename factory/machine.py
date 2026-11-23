"""What this machine does for the factory: load, memory, disk, and each self-hosted runner's share of it.

Several runners on one machine share its cores, memory and disk, and a ticket gets slow without saying why
when they crowd each other out. The board's machine page (factory/machine.html) shows it live and for the
last hour: per runner the job it runs, its CPU, its real memory and how many heavy processes (Unity) it has
open. It finds orphans too: heavy processes or agents that a finished or broken job left behind.

Everything is read from the process list of this machine: no GitHub call. Memory is the footprint the system
counts against a process (what Activity Monitor shows), not ps's resident size: an idle editor's memory is
mostly compressed, and ps then shows a tenth of what it costs.

The same reading measures a job from inside (Meter): what it used goes into the ticket's events and the
closing report.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

JOB = re.compile(r"-m factory ([a-z][\w-]*)(?:.*?--issue[ =](\d+))?")
CLAUDE = re.compile(r"(?:^|/)claude(?:\s|$)")
KINDS = {"work": "Arbeit", "judge": "Richter", "deliver": "Auslieferung", "orchestrate": "Orchestrator",
         "ci-result": "CI-Ergebnis", "human-outcome": "Entscheidung"}


# ---- settings --------------------------------------------------------------------------------------------------

def settings(cfg: dict) -> dict:
    m = cfg.get("machine") or {}
    return {"procs": [str(p) for p in m.get("heavy_processes") or []],
            "sample_s": max(2.0, float(m.get("sample_seconds") or 10)),
            "warn_load": float(m.get("warn_load") or 1.5), "warn_swap_gb": float(m.get("warn_swap_gb") or 4),
            "orphan_minutes": float(m.get("orphan_minutes") or 3)}


def heavy_pattern(patterns) -> re.Pattern | None:
    """One expression for every heavy process (a Unity editor); bad expressions are skipped."""
    good = []
    for p in patterns or []:
        try:
            re.compile(p)
            good.append(f"(?:{p})")
        except re.error:
            continue
    return re.compile("|".join(good)) if good else None


# ---- reading the machine -----------------------------------------------------------------------------------------

def _seconds(text: str) -> float:
    """ps's times: [dd-][hh:]mm:ss[.cc]."""
    try:
        days, _, rest = text.rpartition("-")
        total = 0.0
        for part in rest.split(":"):
            total = total * 60 + float(part)
        return total + (int(days) * 86400 if days else 0)
    except ValueError:
        return 0.0


def process_table() -> list[dict]:
    try:
        out = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,time=,etime=,rss=,command="], text=True,
                             capture_output=True, timeout=10, errors="replace").stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        p = line.split(None, 5)
        if len(p) == 6 and p[0].isdigit() and p[1].isdigit():
            rows.append({"pid": int(p[0]), "ppid": int(p[1]), "cpu_s": _seconds(p[2]), "age_s": _seconds(p[3]),
                         "rss": int(p[4]) * 1024 if p[4].isdigit() else 0, "cmd": p[5]})
    return rows


_libproc = None


def footprint(pid: int, rss: int = 0) -> int:
    """Bytes of memory the system counts against a process (phys_footprint on macOS), else its resident size."""
    global _libproc
    if sys.platform != "darwin":
        return rss
    try:
        if _libproc is None:
            _libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        buf = ctypes.create_string_buffer(512)
        if _libproc.proc_pid_rusage(ctypes.c_int(pid), ctypes.c_int(0), buf) != 0:   # RUSAGE_INFO_V0
            return rss
        return int.from_bytes(buf.raw[72:80], "little")   # 16 bytes uuid, then ri_phys_footprint is the 8th u64
    except (OSError, AttributeError):
        return rss


def _sysctl(name: str) -> str:
    try:
        return subprocess.run(["sysctl", "-n", name], text=True, capture_output=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def memory() -> dict:
    """Total, used and swapped-out memory in bytes; {} where it cannot be read."""
    if sys.platform == "darwin":
        try:
            total = int(_sysctl("hw.memsize") or 0)
            out = subprocess.run(["vm_stat"], text=True, capture_output=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError, ValueError):
            return {}
        page = int((re.search(r"page size of (\d+)", out) or [0, 16384])[1])
        pages = {k.strip().lower(): int(v.strip(" .")) for k, v in re.findall(r"^([^:\n]+):\s+(\d+)\.?$", out, re.M)}
        used = (pages.get("anonymous pages", 0) - pages.get("pages purgeable", 0) + pages.get("pages wired down", 0)
                + pages.get("pages occupied by compressor", 0)) * page
        swap = re.search(r"used = ([\d.]+)M", _sysctl("vm.swapusage"))
        return {"total": total, "used": max(0, used), "swap": int(float(swap[1]) * 2**20) if swap else 0}
    try:
        info = {k: int(v.split()[0]) * 1024 for k, v in
                (line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line)}
    except (OSError, ValueError):
        return {}
    return {"total": info.get("MemTotal", 0), "used": info.get("MemTotal", 0) - info.get("MemAvailable", 0),
            "swap": info.get("SwapTotal", 0) - info.get("SwapFree", 0)}


def runner_dirs(rows: list[dict]) -> dict[int, Path]:
    """The listening runners on this machine: pid of Runner.Listener -> the runner's folder."""
    out = {}
    for r in rows:
        head = r["cmd"].split()[0] if r["cmd"] else ""
        if head.endswith("/bin/Runner.Listener"):
            out[r["pid"]] = Path(head[:-len("/bin/Runner.Listener")])
    return out


def _runner_info(folder: Path) -> dict:
    try:
        info = json.loads((folder / ".runner").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        info = {}
    url = str(info.get("gitHubUrl") or "")
    return {"name": str(info.get("agentName") or folder.name),
            "repo": "/".join(url.rstrip("/").split("/")[-2:]) if url.count("/") >= 4 else ""}


def _short(names: list[str]) -> dict[str, str]:
    """Runner names without the machine's name they all start with: mymac-factory-1 -> factory-1."""
    heads = {n.split("-", 1)[0] for n in names}
    if len(names) > 1 and len(heads) == 1 and all("-" in n for n in names):
        return {n: n.split("-", 1)[1] for n in names}
    return {n: n for n in names}


def snapshot(cfg: dict, prev: dict | None = None, rows: list[dict] | None = None, now: float | None = None) -> dict:
    """One reading of the machine and of each runner on it. prev (the reading before) turns CPU time into a rate."""
    s = settings(cfg)
    now = now or time.time()
    rows = process_table() if rows is None else rows
    by = {r["pid"]: r for r in rows}
    kids: dict[int, list[int]] = {}
    for r in rows:
        kids.setdefault(r["ppid"], []).append(r["pid"])

    def tree(pid: int) -> list[int]:
        out, todo = [], [pid]
        while todo:
            p = todo.pop()
            if p in out:
                continue
            out.append(p)
            todo.extend(kids.get(p, []))
        return out

    heavy = heavy_pattern(s["procs"]) or re.compile(r"(?!x)x")
    before = (prev or {}).get("cpu", {})
    dt = max(1.0, now - (prev or {}).get("t", now - s["sample_s"]))

    def cpu_rate(pids) -> float:
        used = 0.0
        for p in pids:
            r = by[p]
            last = before.get(p)
            used += r["cpu_s"] - last if last is not None and r["cpu_s"] >= last else (r["cpu_s"] if r["age_s"] <= dt else 0)
        return used / dt * 100

    mem_of: dict[int, int] = {}

    def mem(pids) -> int:
        total = 0
        for p in pids:
            if p not in mem_of:
                mem_of[p] = footprint(p, by[p]["rss"])
            total += mem_of[p]
        return total

    listeners = runner_dirs(rows)
    infos = {pid: _runner_info(folder) for pid, folder in listeners.items()}
    short = _short([i["name"] for i in infos.values()])
    owned: set[int] = set()
    runners = []
    for pid, folder in sorted(listeners.items(), key=lambda kv: infos[kv[0]]["name"]):
        pids = tree(pid)
        owned.update(pids)
        worker = next((p for p in pids if by[p]["cmd"].split()[0].endswith("/bin/Runner.Worker")), None)
        job = None
        if worker:
            for p in tree(worker):
                m = JOB.search(by[p]["cmd"])
                if m:
                    job = {"kind": m[1], "label": KINDS.get(m[1], m[1]), "ticket": int(m[2]) if m[2] else None}
                    break
            job = {**(job or {"kind": "", "label": "Workflow des Projekts", "ticket": None}), "minutes": round(by[worker]["age_s"] / 60, 1)}
        runners.append({"name": short[infos[pid]["name"]], "full": infos[pid]["name"], "repo": infos[pid]["repo"],
                        "folder": str(folder), "busy": bool(worker), "job": job, "pids": pids})
    # Processes that left their runner's tree (an editor whose script ended) still belong to it by their folder.
    loose: dict[str, list[int]] = {}
    orphans = []
    for r in rows:
        if r["pid"] in owned:
            continue
        for run in runners:
            if run["folder"] + "/_work" in r["cmd"]:
                loose.setdefault(run["name"], []).append(r["pid"])
                is_agent = bool(CLAUDE.search(r["cmd"].split(" -")[0]))
                if (heavy.search(r["cmd"]) or is_agent) and r["age_s"] >= s["orphan_minutes"] * 60:
                    orphans.append({"pid": r["pid"], "runner": run["name"], "name": _name(r["cmd"]),
                                    "minutes": round(r["age_s"] / 60), "mem": footprint(r["pid"], r["rss"]),
                                    "cmd": r["cmd"][:240], "runner_busy": run["busy"]})
                break
    out_runners, owner = [], {}
    for run in runners:
        pids = run.pop("pids") + loose.get(run["name"], [])
        owner.update({p: run["name"] for p in pids})
        top = sorted(pids, key=lambda p: -mem([p]))[:5]
        out_runners.append({**run, "cpu": round(cpu_rate(pids)), "mem": mem(pids),
                            "heavy": sum(1 for p in pids if heavy.search(by[p]["cmd"])), "procs": len(pids),
                            "loose": len(loose.get(run["name"], [])),
                            "top": [{"pid": p, "name": _name(by[p]["cmd"]), "mem": mem([p]), "cpu": round(cpu_rate([p]))}
                                    for p in top]})
    others = [r["pid"] for r in rows if r["pid"] not in owner]
    busiest = sorted(others, key=lambda p: -cpu_rate([p]))[:6]
    outside = {"cpu": round(cpu_rate(others)), "top": [{"pid": p, "name": _name(by[p]["cmd"]), "cpu": round(cpu_rate([p])),
                                                          "mem": mem([p])} for p in busiest]}
    try:
        load = os.getloadavg()
    except OSError:
        load = (0.0, 0.0, 0.0)
    try:
        du = shutil.disk_usage(str(Path.home()))
        disk = {"free": du.free, "total": du.total}
    except OSError:
        disk = {}
    used = round(cpu_rate(list(by)))
    return {"t": now, "cores": os.cpu_count() or 1, "load": [round(x, 1) for x in load], "cpu_used": used,
            "outside": outside, "mem": memory(), "disk": disk,
            "heavy": sum(1 for r in rows if heavy.search(r["cmd"])), "runners": out_runners, "orphans": orphans,
            "cpu": {r["pid"]: r["cpu_s"] for r in rows}, "owner": owner}


def _name(cmd: str) -> str:
    head = cmd.split(" -")[0].strip()
    if ".app/Contents/MacOS/" in head:
        return head.rsplit("/", 1)[-1]
    first = cmd.split()[0] if cmd.split() else ""
    base = first.rsplit("/", 1)[-1]
    if base.startswith("python") and len(cmd.split()) > 1:
        rest = cmd.split()[1:]
        return f"{base} {' '.join(rest[:3])}"[:60]
    return base[:60]


def de(x: float, digits: int = 1) -> str:
    """A number as the page writes it: 27,5."""
    return f"{x:.{digits}f}".replace(".", ",")


def alerts(sample: dict, cfg: dict) -> list[dict]:
    """What needs a look, worst first. Every alert says it in words: color alone carries nothing."""
    s, out = settings(cfg), []
    cores, load = sample["cores"], (sample.get("load") or [0])[0]
    if load / cores >= s["warn_load"]:
        busy = sample.get("cpu_used", 0) / 100
        if busy >= cores * 0.85:
            out.append({"tone": "bad", "text": f"Rechner überlastet: CPU-Last {round(busy / cores * 100)} %, "
                                               f"{round(load)} Prozesse wollen gleichzeitig rechnen"})
        else:   # many waiting, few computing: they wait for memory (swap) or the disk, not for a core
            out.append({"tone": "bad", "text": f"Rechner ausgebremst: {round(load)} Prozesse stauen sich vor {cores} Kernen. "
                                               "Die meisten warten auf Arbeitsspeicher oder Platte, nicht auf die CPU."})
    swap = (sample.get("mem") or {}).get("swap", 0) / 2**30
    if swap >= s["warn_swap_gb"]:
        out.append({"tone": "bad", "text": f"Arbeitsspeicher voll: {de(swap)} GB liegen auf der Platte statt im RAM. "
                                           "Das macht alle Runner langsam; weniger Runner gleichzeitig würden helfen."})
    board = cfg.get("board") or {}
    free = (sample.get("disk") or {}).get("free", 0) / 1e9
    need = float((cfg.get("limits") or {}).get("min_free_disk_gb") or 0)
    if sample.get("disk") and need and free < need:
        out.append({"tone": "bad", "text": f"Nur {free:.0f} GB frei: Tickets warten, bis {need:g} GB frei sind"})
    elif sample.get("disk") and free < float(board.get("min_free_disk_gb") or 15):
        out.append({"tone": "warn", "text": f"Nur {free:.0f} GB frei auf der Platte"})
    if sample["orphans"]:
        n = len(sample["orphans"])
        out.append({"tone": "warn", "text": f"{n} verwaiste{'r' if n == 1 else ''} Prozess{'e' if n > 1 else ''} ohne Job"})
    return out


def kill_tree(pid: int, rows: list[dict] | None = None) -> int:
    """End a process and everything it started: TERM now, KILL to what is left after a few seconds."""
    rows = process_table() if rows is None else rows
    kids: dict[int, list[int]] = {}
    for r in rows:
        kids.setdefault(r["ppid"], []).append(r["pid"])
    todo, doomed = [pid], []
    while todo:
        p = todo.pop()
        if p not in doomed:
            doomed.append(p)
            todo.extend(kids.get(p, []))
    for p in doomed:
        try:
            os.kill(p, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    def finish():
        time.sleep(5)
        for p in doomed:
            try:
                os.kill(p, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    threading.Thread(target=finish, daemon=True).start()
    return len(doomed)


# ---- the board's monitor -------------------------------------------------------------------------------------------

class Monitor:
    """Reads the machine every few seconds in the background and keeps the last hour."""

    def __init__(self, cfg_of, sample=snapshot):
        self.cfg_of, self.sample = cfg_of, sample
        self.last: dict | None = None
        self.history: deque = deque(maxlen=720)
        self.disk_use: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.started = False

    def start(self) -> "Monitor":
        if not self.started:
            self.started = True
            threading.Thread(target=self._loop, daemon=True).start()
            threading.Thread(target=self._disk_loop, daemon=True).start()
        return self

    def tick(self) -> dict:
        cfg = self.cfg_of()
        got = self.sample(cfg, self.last)
        with self.lock:
            self.last = got
            m = got.get("mem") or {}
            self.history.append({"t": round(got["t"]), "load": got["load"][0], "cpu": got.get("cpu_used", 0), "mem": m.get("used", 0), "swap": m.get("swap", 0),
                                 "heavy": got["heavy"], "r": {r["name"]: [r["cpu"], r["mem"], r["heavy"]] for r in got["runners"]}})
            keep = 3600 / settings(cfg)["sample_s"]
            while len(self.history) > keep:
                self.history.popleft()
        return got

    def _loop(self):
        first = True
        while True:
            try:
                self.tick()
            except Exception as exc:   # a broken reading must not end the monitor
                print(f"[factory] machine reading failed: {exc}", flush=True)
            # CPU is a rate between two readings: the second comes soon, so the page shows it from the start.
            time.sleep(2 if first else settings(self.cfg_of())["sample_s"])
            first = False

    def _disk_loop(self):
        """How much each runner's work folder holds. du is slow and APFS clones count twice, so rarely and as a hint."""
        while True:
            for run in list((self.last or {}).get("runners") or []):
                try:
                    out = subprocess.run(["du", "-sk", run["folder"] + "/_work"], text=True, capture_output=True,
                                         timeout=300).stdout.split()
                    self.disk_use[run["name"]] = {"bytes": int(out[0]) * 1024, "t": time.time()}
                except (OSError, subprocess.SubprocessError, ValueError, IndexError):
                    pass
            time.sleep(900 if self.last else 15)

    def state(self, cards: list[dict] | None = None) -> dict:
        cfg = self.cfg_of()
        got = self.last or self.tick()
        s = settings(cfg)
        steps = {(c.get("repo"), c.get("number")): c for c in cards or []}
        runners = []
        for r in got["runners"]:
            job = dict(r["job"] or {}) or None
            card = steps.get((r["repo"], (job or {}).get("ticket"))) if job else None
            if job and job.get("ticket") and r["repo"]:
                job["path"] = f"/p/{r['repo']}#t={job['ticket']}"
            if job and card:
                job.update(title=card.get("title") or "", step=card.get("step") or "")
            runners.append({**r, "job": job, "disk": self.disk_use.get(r["name"])})
        with self.lock:
            history = list(self.history)
        return {"now": {k: v for k, v in got.items() if k not in ("cpu", "runners", "owner")}, "runners": runners,
                "history": history, "alerts": alerts(got, cfg),
                "sample_s": s["sample_s"], "warn_load": s["warn_load"]}

    def tickets(self) -> dict:
        """What each running ticket costs this machine right now, keyed "owner/repo#number".

        CPU is the share of all cores and memory the share of all RAM, so both read as 0-100 % of the machine.
        A ticket with two jobs at once (work and judge) counts both. Jobs that name no ticket are left out."""
        got = self.last
        if not got:
            return {}
        cores = got.get("cores") or 1
        total = (got.get("mem") or {}).get("total") or 0
        out: dict[str, dict] = {}
        for r in got.get("runners") or []:
            job = r.get("job") or {}
            if not (job.get("ticket") and r.get("repo")):
                continue
            k = f"{r['repo']}#{job['ticket']}"
            t = out.setdefault(k, {"cpu_core": 0, "mem": 0, "runners": [], "jobs": []})
            t["cpu_core"] += r.get("cpu") or 0
            t["mem"] += r.get("mem") or 0
            t["runners"].append(r["name"])
            t["jobs"].append(job.get("label") or job.get("kind") or "")
        for t in out.values():
            t["cpu"] = min(100, round(t["cpu_core"] / cores))
            t["ram"] = min(100, round(t["mem"] / total * 100)) if total else None
            t["mem_gb"] = round(t["mem"] / 2**30, 1)
            t["cores"] = cores
        return {"t": round(got["t"]), "sample_s": settings(self.cfg_of())["sample_s"], "tickets": out}

    def kill(self, pid: int) -> dict:
        """End an orphan. Only a process that is an orphan right now, so the page cannot end anything else."""
        got = self.sample(self.cfg_of(), self.last)
        match = next((o for o in got["orphans"] if o["pid"] == pid), None)
        if not match:
            return {"error": "Der Prozess ist kein verwaister Prozess mehr (schon beendet oder wieder einem Job zugeordnet)."}
        n = kill_tree(pid)
        self.tick()
        return {"ok": True, "killed": n, "name": match["name"]}


# ---- a job measures itself ----------------------------------------------------------------------------------------

class Meter:
    """What one job and everything it started used: CPU time, the peak of memory and of heavy processes at once."""

    def __init__(self, cfg: dict, every_s: float = 15.0, root: int | None = None):
        self.cfg, self.every, self.root = cfg, every_s, root or os.getpid()
        self.cpu: dict[int, float] = {}
        self.peak_mem = 0
        self.peak_heavy = 0
        self.started = time.time()
        self.stop = threading.Event()
        self.heavy = heavy_pattern(settings(cfg)["procs"]) or re.compile(r"(?!x)x")

    def read(self) -> None:
        rows = process_table()
        kids: dict[int, list[int]] = {}
        by = {r["pid"]: r for r in rows}
        for r in rows:
            kids.setdefault(r["ppid"], []).append(r["pid"])
        todo, pids = [self.root], []
        while todo:
            p = todo.pop()
            if p in by and p not in pids:
                pids.append(p)
                todo.extend(kids.get(p, []))
        for p in pids:
            self.cpu[p] = max(self.cpu.get(p, 0.0), by[p]["cpu_s"])
        self.peak_mem = max(self.peak_mem, sum(footprint(p, by[p]["rss"]) for p in pids))
        self.peak_heavy = max(self.peak_heavy, sum(1 for p in pids if self.heavy.search(by[p]["cmd"])))

    def __enter__(self) -> "Meter":
        def loop():
            while not self.stop.wait(self.every):
                try:
                    self.read()
                except Exception:
                    pass
        threading.Thread(target=loop, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop.set()
        try:
            self.read()
        except Exception:
            pass

    def result(self) -> dict:
        return {"minutes": round((time.time() - self.started) / 60, 1), "cpu_min": round(sum(self.cpu.values()) / 60, 1),
                "peak_gb": round(self.peak_mem / 2**30, 2), "heavy_peak": self.peak_heavy}


def describe(res: dict) -> str:
    cpu = float(res.get("cpu_min") or 0)
    parts = [f"{de(cpu, 0 if cpu >= 10 else 1)} CPU-min" if cpu else "",
             f"Spitze {de(float(res['peak_gb']))} GB Speicher" if res.get("peak_gb") else "",
             f"bis {res['heavy_peak']} schwere Prozesse gleichzeitig" if res.get("heavy_peak", 0) > 1 else
             ("1 schwerer Prozess" if res.get("heavy_peak") == 1 else "")]
    return ", ".join(p for p in parts if p)
