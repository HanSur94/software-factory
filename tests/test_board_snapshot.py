"""The board answers from its last snapshot and reads GitHub behind it, so no page waits."""

import gzip
import json
import threading
import time

from factory.board import Snapshot


def slow(results, gate=None):
    calls = []

    def make():
        if gate:
            gate.wait(5)
        calls.append(time.time())
        return results[min(len(calls), len(results)) - 1]
    return make, calls


def test_the_first_answer_is_waited_for_then_stale_ones_serve_at_once():
    gate = threading.Event()
    make, calls = slow([{"n": 1}, {"n": 2}], gate)
    snap = Snapshot(make, max_age=0.05)
    threading.Timer(0.1, gate.set).start()
    assert snap.get().value == {"n": 1}                 # nothing yet: wait for the first read
    assert json.loads(gzip.decompress(snap.gz)) == {"n": 1}
    gate.clear()
    time.sleep(0.1)
    start = time.time()
    assert snap.get().value == {"n": 1}                 # stale: the old answer comes at once ...
    assert time.time() - start < 0.05
    gate.set()
    for _ in range(100):
        if snap.value == {"n": 2}:
            break
        time.sleep(0.01)
    assert snap.value == {"n": 2} and len(calls) == 2   # ... and the new one is read behind it


def test_one_read_at_a_time_however_many_ask():
    gate = threading.Event()
    make, calls = slow([{"n": 1}], gate)
    snap = Snapshot(make, max_age=0)
    for _ in range(5):
        snap.kick()
    gate.set()
    snap.get(max_age=10)
    assert len(calls) == 1


def test_an_asked_max_age_waits_for_a_young_enough_answer():
    make, calls = slow([{"n": 1}, {"n": 2}])
    snap = Snapshot(make, max_age=60)
    snap.get()
    time.sleep(0.05)
    assert snap.get(max_age=0.01).value == {"n": 2}     # the watch needs news, not the old answer


def test_a_failed_read_keeps_the_last_answer():
    answers = [{"n": 1}]

    def make():
        if not answers:
            raise RuntimeError("GitHub down")
        return answers.pop()
    snap = Snapshot(make, max_age=0)
    snap.get()
    assert snap.get(max_age=0).value == {"n": 1} and not snap.busy
