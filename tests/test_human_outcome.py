"""What the factory does when a human merges or closes a pull request it opened."""
from types import SimpleNamespace

from factory import deliver


class Host:
    def __init__(self):
        self.calls = []

    def labels(self, add=(), remove=()):
        self.calls.append(("labels", tuple(add), tuple(remove)))

    def close_issue(self):
        self.calls.append(("close",))

    def issue(self):
        return {"body": "An ordinary issue."}


def _ctx():
    host, seen = Host(), []
    ctx = SimpleNamespace(host=host, label=lambda name: "factory:" + name,
                          emit=lambda *a, **k: seen.append(k.get("outcome")), save=lambda note: None)
    return ctx, host, seen


def test_a_human_merge_closes_the_issue_and_marks_it_done():
    ctx, host, seen = _ctx()
    deliver.human_outcome(ctx, merged=True)
    assert seen == ["merged"]
    assert ("labels", ("factory:done",), ("factory:running", "factory:review")) in host.calls
    assert ("close",) in host.calls


def test_a_closed_pull_request_leaves_the_issue_open():
    ctx, host, seen = _ctx()
    deliver.human_outcome(ctx, merged=False)
    assert seen == ["closed"]
    assert host.calls == []
