"""Parallel workers: independent tasks of a ticket at the same time, each in its own folder."""
import threading
from pathlib import Path

from conftest import sh
from factory import cli, fake, stations
from factory.workers import next_wave

real_fake = fake.run

PLAN = """---
schema: plan/1
ticket: {ticket}
summary: Three parts, two of them independent.
tasks:
  - id: T1
    title: Part one
    goal: The first part exists.
    acceptance: ["one.md exists"]
    files: [one.md]
  - id: T2
    title: Part two
    goal: The second part exists.
    acceptance: ["two.md exists"]
    files: [two.md]
  - id: T3
    title: Put them together
    goal: The summary names both parts.
    acceptance: ["sum.md exists"]
    files: [sum.md]
    depends_on: [T1, T2]
---

Plan.
"""


def use(monkeypatch, implement):
    """Canned stations, with a three-task plan and the given behaviour of the implementer."""
    def run(role, values, cwd, out):
        if role == "planner":
            out.write_text(PLAN.format(ticket=values["ticket"]))
        elif role == "implementer":
            implement(values["task_id"], Path(cwd))
        else:
            real_fake(role, values, cwd, out)
    monkeypatch.setattr(fake, "run", run)


def task(tid, files=None, deps=None):
    return {"id": tid, "title": tid, "goal": "g", "acceptance": ["a"], **({"files": files} if files is not None else {}),
            **({"depends_on": deps} if deps else {})}


def test_wave_takes_independent_tasks_and_respects_the_limit():
    tasks = [task("T1", ["a.py"]), task("T2", ["b.py"]), task("T3", ["c.py"]), task("T4", ["d.py"], ["T1"])]
    assert [t["id"] for t in next_wave(tasks, [], 3)] == ["T1", "T2", "T3"]
    assert [t["id"] for t in next_wave(tasks, [], 2)] == ["T1", "T2"]
    assert [t["id"] for t in next_wave(tasks, [], 1)] == ["T1"]
    assert [t["id"] for t in next_wave(tasks, ["T1", "T2"], 3)] == ["T3", "T4"]
    assert next_wave(tasks, ["T1", "T2", "T3", "T4"], 3) == []


def test_wave_keeps_tasks_apart_that_touch_the_same_files():
    same = [task("T1", ["src/a.py"]), task("T2", ["src/a.py"]), task("T3", ["docs"])]
    assert [t["id"] for t in next_wave(same, [], 3)] == ["T1", "T3"]
    folder = [task("T1", ["src/"]), task("T2", ["src/deep/b.py"])]
    assert [t["id"] for t in next_wave(folder, [], 3)] == ["T1"]
    unknown = [task("T1", ["a.py"]), task("T2"), task("T3", ["c.py"])]   # T2 does not say what it touches
    assert [t["id"] for t in next_wave(unknown, [], 3)] == ["T1", "T3"]
    assert [t["id"] for t in next_wave(unknown, ["T1", "T3"], 3)] == ["T2"]


def test_wave_does_not_hang_on_tasks_that_wait_for_each_other():
    circle = [task("T1", ["a"], ["T2"]), task("T2", ["b"], ["T1"])]
    assert [t["id"] for t in next_wave(circle, [], 3)] == ["T1"]
    missing = [task("T1", ["a"], ["T9"])]   # depends on a task the plan does not have
    assert [t["id"] for t in next_wave(missing, [], 3)] == ["T1"]


def test_independent_tasks_run_at_the_same_time_in_their_own_folders(product, monkeypatch):
    together = threading.Barrier(2, timeout=10)   # breaks unless T1 and T2 are inside the implementer at once
    folders = {}

    def implement(tid, cwd):
        folders[tid] = cwd
        if tid in ("T1", "T2"):
            together.wait()
            assert not (cwd / ("two.md" if tid == "T1" else "one.md")).exists()   # a worker does not see the other
            (cwd / ("one.md" if tid == "T1" else "two.md")).write_text(f"# {tid}\n")
        else:
            (cwd / "sum.md").write_text((cwd / "one.md").read_text() + (cwd / "two.md").read_text())
    use(monkeypatch, implement)

    assert cli.main(product.args) == 0
    st = product.status()
    assert st["phase"] == "delivered" and st["done_tasks"] == ["T1", "T2", "T3"] and st["workers"] == []
    assert folders["T1"].name == "wt-1-T1" and folders["T2"].name == "wt-1-T2" and folders["T3"].name == "wt-1"
    assert not folders["T1"].exists() and not folders["T2"].exists()   # worker folders are gone afterwards
    assert "--T" not in sh(product.repo, "git", "branch", "--list")
    assert sh(product.repo, "git", "show", f"{st['branch']}:sum.md") == "# T1\n# T2"
    subjects = sh(product.repo, "git", "log", "--format=%s", f"main..{st['branch']}").splitlines()
    assert subjects == ["T3: Put them together", "T2: Part two", "T1: Part one"]

    events = product.events()
    parallel = [e for e in events if e.get("outcome") == "parallel"]
    assert len(parallel) == 1 and parallel[0]["detail"].startswith("T1, T2 gleichzeitig")
    runs = [e for e in events if e["kind"] == "agent" and e["station"] == "implementer"]
    assert sorted(e["task"] for e in runs) == ["T1", "T2", "T3"]
    assert len({e["seq"] for e in events if e["kind"] == "agent"}) == len([e for e in events if e["kind"] == "agent"])
    logs = sorted(f.name for f in (product.run_dir / "logs").glob("*-implementer.md"))
    assert len(logs) == 3   # every run has its own log file


def test_a_change_that_does_not_fit_runs_again_on_top_of_the_others(product, monkeypatch):
    calls = []

    def implement(tid, cwd):
        calls.append((tid, cwd.name))
        if tid == "T3":
            (cwd / "sum.md").write_text("sum\n")
            return
        shared = cwd / "shared.md"   # both tasks write a file neither of them announced
        shared.write_text((shared.read_text() if shared.exists() else "") + f"{tid}\n")
    use(monkeypatch, implement)

    assert cli.main(product.args) == 0
    st = product.status()
    assert st["phase"] == "delivered" and st["done_tasks"] == ["T1", "T2", "T3"]
    assert ("T2", "wt-1-T2") in calls and ("T2", "wt-1") in calls   # once as a worker, once more in the ticket's folder
    assert sh(product.repo, "git", "show", f"{st['branch']}:shared.md") == "T1\nT2"
    conflict = [e for e in product.events() if e.get("outcome") == "parallel_conflict"]
    assert len(conflict) == 1 and conflict[0]["detail"].startswith("T2 ")


def test_one_worker_setting_runs_tasks_one_after_the_other(product, monkeypatch):
    profile = product.repo / ".factory" / "profile.yaml"
    profile.write_text(profile.read_text() + "overrides:\n  pipeline:\n    parallel_tasks: 1\n")
    sh(product.repo, "git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "one worker")
    folders = []
    use(monkeypatch, lambda tid, cwd: (folders.append(cwd.name), (cwd / f"{tid}.md").write_text(tid + "\n")))

    assert cli.main(product.args) == 0
    assert folders == ["wt-1", "wt-1", "wt-1"] and product.status()["done_tasks"] == ["T1", "T2", "T3"]
    assert not [e for e in product.events() if e.get("outcome") == "parallel"]


def test_a_failed_worker_stops_the_ticket_but_keeps_the_other_workers_result(product, monkeypatch):
    def implement(tid, cwd):
        if tid == "T2":
            raise stations.StationError("implementer", "the model gave up")
        (cwd / "one.md").write_text("# T1\n")
    use(monkeypatch, implement)

    assert cli.main(product.args) == 0
    st = product.status()
    assert st["phase"] == "implement" and "the model gave up" in st["stopped"]
    assert st["done_tasks"] == ["T1"] and st["workers"] == []
    assert not (product.work / "wt-1-T1").exists() and not (product.work / "wt-1-T2").exists()
    assert sh(product.repo, "git", "show", f"{st['branch']}:one.md") == "# T1"
