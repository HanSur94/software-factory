"""The board's machine page: each runner's share of this machine, alerts in words, orphans, and what a job used."""
import json
import subprocess
import sys
from types import SimpleNamespace

from factory import machine, summary


def cfg(tmp_path, **machine_values) -> dict:
    return {"agent": {}, "board": {}, "limits": {}, "machine": {**machine_values}}


def test_heavy_processes_are_one_expression_and_a_bad_one_is_skipped():
    pat = machine.heavy_pattern([r"Unity\.app/Contents/MacOS/Unity .*-batchmode", "(broken"])
    assert pat.search("/A/Unity.app/Contents/MacOS/Unity -batchmode -projectPath x")
    assert not pat.search("/A/Unity.app/Contents/MacOS/Unity -projectPath x")
    assert machine.heavy_pattern([]) is None


# ---- the monitor ---------------------------------------------------------------------------------------------------

def rows(tmp_path) -> list[dict]:
    """A machine with two runners: factory-1 runs ticket 12 with an editor, factory-2 is idle and left one behind."""
    r1, r2 = tmp_path / "actions-runner-factory-1", tmp_path / "actions-runner-factory-2"
    for d, name in ((r1, "Mac-factory-1"), (r2, "Mac-factory-2")):
        d.mkdir(exist_ok=True)
        (d / ".runner").write_text("﻿" + json.dumps({"agentName": name, "gitHubUrl": "https://github.com/o/game"}))
    def p(pid, ppid, cmd, cpu=0.0, age=3600.0, rss=1000):
        return {"pid": pid, "ppid": ppid, "cpu_s": cpu, "age_s": age, "rss": rss * 1024, "cmd": cmd}
    return [p(1, 0, "/sbin/launchd"),
            p(10, 1, f"{r1}/bin/Runner.Listener run"), p(11, 10, f"{r1}/bin/Runner.Worker spawnclient 1 2", age=600),
            p(12, 11, "/bin/bash -e x.sh"), p(13, 12, "python3 -m factory work --repo . --issue 12", cpu=30),
            p(14, 13, "/Applications/Unity/Hub/Editor/6/Unity.app/Contents/MacOS/Unity -batchmode -projectPath x", cpu=600, rss=2_000_000),
            p(20, 1, f"{r2}/bin/Runner.Listener run"),
            p(21, 1, f"/Applications/Unity/Hub/Editor/6/Unity.app/Contents/MacOS/Unity -batchmode -projectPath {r2}/_work/_temp/factory/wt-9",
              cpu=900, rss=1_500_000),
            p(30, 1, "/Applications/Safari.app/Contents/MacOS/Safari", cpu=50)]


def test_each_runner_gets_its_job_its_processes_and_what_it_left_behind(tmp_path):
    c = {**cfg(tmp_path), "machine": {"heavy_processes": [r"Unity\.app/Contents/MacOS/Unity .*-batchmode"], "orphan_minutes": 3}}
    before = machine.snapshot(c, rows=rows(tmp_path), now=1000.0)
    later = rows(tmp_path)
    for r in later:
        r["cpu_s"] += {14: 10.0, 21: 5.0, 30: 1.0}.get(r["pid"], 0.0)
    got = machine.snapshot(c, prev=before, rows=later, now=1010.0)
    one, two = got["runners"]
    assert (one["name"], one["busy"], one["job"]["kind"], one["job"]["ticket"], one["repo"]) == ("factory-1", True, "work", 12, "o/game")
    assert one["heavy"] == 1 and one["cpu"] == 100 and one["job"]["minutes"] == 10
    assert (two["name"], two["busy"], two["job"], two["heavy"], two["loose"]) == ("factory-2", False, None, 1, 1)
    assert [(o["pid"], o["runner"], o["name"]) for o in got["orphans"]] == [(21, "factory-2", "Unity")]
    assert got["heavy"] == 2 and got["outside"]["cpu"] == 10 and got["cpu_used"] == 160


def test_alerts_say_what_is_wrong_in_words(tmp_path):
    c = {**cfg(tmp_path), "limits": {"min_free_disk_gb": 8}, "board": {"min_free_disk_gb": 15}}
    sample = {"cores": 8, "load": [30.0], "cpu_used": 300, "mem": {"swap": 6 * 2**30}, "heavy": 3,
              "disk": {"free": 5e9}, "orphans": [{"pid": 1}]}
    texts = [a["text"] for a in machine.alerts(sample, c)]
    assert texts == ["Rechner ausgebremst: 30 Prozesse stauen sich vor 8 Kernen. "
                     "Die meisten warten auf Arbeitsspeicher oder Platte, nicht auf die CPU.",
                     "Arbeitsspeicher voll: 6,0 GB liegen auf der Platte statt im RAM. "
                     "Das macht alle Runner langsam; weniger Runner gleichzeitig würden helfen.",
                     "Nur 5 GB frei: Tickets warten, bis 8 GB frei sind", "1 verwaister Prozess ohne Job"]
    calm = {"cores": 8, "load": [2.0], "cpu_used": 150, "mem": {"swap": 0}, "heavy": 3, "disk": {"free": 100e9}, "orphans": []}
    assert machine.alerts(calm, c) == []


def test_only_an_orphan_can_be_ended_from_the_page(tmp_path):
    c = cfg(tmp_path, heavy_processes=[r"Unity\.app/Contents/MacOS/Unity .*-batchmode"])
    data = rows(tmp_path)
    killed = []
    mon = machine.Monitor(lambda: c, sample=lambda cfg, prev=None: machine.snapshot(cfg, prev, rows=data))
    import factory.machine as m
    real = m.kill_tree
    m.kill_tree = lambda pid, rows=None: killed.append(pid) or 1
    try:
        assert "error" in mon.kill(14)          # factory-1's editor belongs to a running job
        assert mon.kill(21)["ok"] and killed == [21]
    finally:
        m.kill_tree = real
    state = mon.state([{"repo": "o/game", "number": 12, "title": "Formation", "step": "Bau"}])
    job = state["runners"][0]["job"]
    assert (job["step"], job["path"]) == ("Bau", "/p/o/game#t=12") and "brake" not in state


def test_each_running_ticket_gets_its_share_of_cpu_and_ram(tmp_path):
    c = cfg(tmp_path)
    mon = machine.Monitor(lambda: c)
    assert mon.tickets() == {}                   # no reading yet: nothing to show, nothing invented
    runner = lambda name, ticket, cpu, mem, kind="work": {"name": name, "repo": "o/game", "cpu": cpu, "mem": mem,
                                                         "job": {"kind": kind, "label": kind, "ticket": ticket} if kind else None}
    mon.last = {"t": 1000.0, "cores": 8, "mem": {"total": 32 * 2**30},
                "runners": [runner("factory-1", 12, 300, 6 * 2**30), runner("factory-2", 12, 100, 2 * 2**30, "judge"),
                            runner("factory-3", None, 900, 9 * 2**30, "orchestrate"), runner("unity-2", None, 50, 2**30, "")]}
    got = mon.tickets()["tickets"]
    assert list(got) == ["o/game#12"]           # jobs without a ticket stay off the cards
    t = got["o/game#12"]
    assert (t["cpu"], t["ram"], t["mem_gb"], t["runners"]) == (50, 25, 8.0, ["factory-1", "factory-2"])
    mon.last["runners"] = [runner("factory-1", 7, 2000, 40 * 2**30)]
    assert (mon.tickets()["tickets"]["o/game#7"]["cpu"], mon.tickets()["tickets"]["o/game#7"]["ram"]) == (100, 100)


def test_a_job_measures_what_it_and_its_children_used(tmp_path):
    c = cfg(tmp_path)
    with machine.Meter(c, every_s=0.2) as meter:
        subprocess.run([sys.executable, "-c", "import time; t=time.time()\nwhile time.time()-t<0.6: pass"])
    res = meter.result()
    assert res["peak_gb"] > 0 and res["cpu_min"] >= 0 and set(res) == {"minutes", "cpu_min", "peak_gb", "heavy_peak"}
    assert machine.describe({"cpu_min": 42.3, "peak_gb": 5.12, "heavy_peak": 2}) == \
        "42 CPU-min, Spitze 5,1 GB Speicher, bis 2 schwere Prozesse gleichzeitig"


def test_the_closing_report_counts_what_the_jobs_used(tmp_path):
    evs = [{"kind": "pipeline", "station": "pipeline", "outcome": "resources", "cpu_min": 30, "peak_gb": 4.2, "heavy_peak": 1,
            "ts": "2026-11-20T08:00:00Z"},
           {"kind": "pipeline", "station": "pipeline", "outcome": "resources", "cpu_min": 12.5, "peak_gb": 5.1, "heavy_peak": 2,
            "ts": "2026-11-20T09:00:00Z"}]
    ctx = SimpleNamespace(status={}, cfg={})
    assert "Rechner: 42 CPU-min, Spitze 5,1 GB Speicher, bis 2 schwere Prozesse gleichzeitig" in summary._effort(ctx, evs)
