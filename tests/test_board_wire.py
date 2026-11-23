"""The page gets a lean board answer: the drawer's details come per ticket, an unchanged board keeps its tag."""

import json

from factory.board import DRAWER_ONLY, Snapshot, tag_of, wire


def card(n, tokens=10):
    run = {"tokens": {"input": tokens, "output": 1, "cache_read": 2, "cache_write": 3}, "model": "x"}
    return {"source": "gh:o/r", "number": n, "workflow": {
        "stations": [{"id": "implementer", "state": "done", "seconds": 5, "runs": [run, run]}],
        "totals": {"work_seconds": 5}, **{k: ["big"] * 50 for k in DRAWER_ONLY}}}


def test_the_wire_drops_drawer_details_and_keeps_what_the_board_draws():
    value = {"cards": [card(1)], "updated": "10:00:00"}
    sent = wire(value)
    w = sent["cards"][0]["workflow"]
    assert not any(k in w for k in DRAWER_ONLY)
    assert w["totals"] == {"work_seconds": 5}
    st = w["stations"][0]
    assert "runs" not in st and st["state"] == "done" and st["seconds"] == 5
    assert st["run_tokens"] == 2 * (10 + 1 + 2 + 3)            # the overview's token column without the runs
    assert "timeline" in value["cards"][0]["workflow"]          # the server keeps the whole answer
    assert len(json.dumps(sent)) < len(json.dumps(value)) / 3


def test_the_wire_leaves_other_answers_alone():
    assert wire({"n": 1}) == {"n": 1} and wire(None) is None


def test_the_tag_ignores_the_clock_but_not_the_content():
    a = {"cards": [card(1)], "updated": "10:00:00"}
    assert tag_of(a) == tag_of({**a, "updated": "10:00:08"})
    assert tag_of(a) != tag_of({**a, "cards": [card(1, tokens=11)]})


def test_the_snapshot_carries_the_tag_of_what_it_sends():
    snap = Snapshot(lambda: {"cards": [card(2)], "updated": "x"})
    got = snap.get()
    assert got.tag == tag_of(wire(got.value))
    assert "timeline" not in json.loads(got.body)["cards"][0]["workflow"]
