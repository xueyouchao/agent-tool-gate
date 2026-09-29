"""Concurrency invariants — one authorization decision at a time per session (spec §10).

Both guarantees pinned here hold *only* because a session's whole decision — the budget check,
the judgment call and the record — runs in one critical section. Both were measurably broken
when only the record's tail was serialized: ten concurrent gray calls against a ceiling of two
overspent it, and the decision record's sequence was not append-ordered.
"""
from __future__ import annotations

import json
import threading
import time

from fakes import FakeJev, make_engine

from toolgate.application.session_slot import SessionSlot
from toolgate.domain.model import Budget

GRAY = "echo hello"  # no phase-1 policy matches, so the call reaches judgment


def _concurrently(fn, n: int) -> list:
    """Run `fn` on n threads released together, maximizing contention."""
    barrier = threading.Barrier(n)
    out: list = [None] * n

    def run(i: int) -> None:
        barrier.wait()
        out[i] = fn()

    threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not any(t.is_alive() for t in threads), "a decision deadlocked on the slot"
    return out


def _engine(tmp_path, *, jev, budget=None):
    return make_engine(tmp_path / "d.jsonl", jev=jev, budget=budget, samples=1)


# --- the slot's own interface -------------------------------------------------

def test_slot_serializes_one_session():
    slot, order = SessionSlot(), []
    holding, release = threading.Event(), threading.Event()

    def first():
        with slot.hold("s1"):
            order.append("first-in")
            holding.set()
            release.wait(2)
            order.append("first-out")

    def second():
        holding.wait(2)
        with slot.hold("s1"):
            order.append("second-in")

    t1 = threading.Thread(target=first, daemon=True)
    t2 = threading.Thread(target=second, daemon=True)
    t1.start()
    t2.start()

    assert holding.wait(2)          # first is inside
    time.sleep(0.05)                # give second time to reach the acquire and block
    assert order == ["first-in"]    # second is queued behind it, not inside

    release.set()
    t1.join(2)
    t2.join(2)
    assert order == ["first-in", "first-out", "second-in"]


def test_slot_does_not_serialize_across_sessions():
    slot, acquired = SessionSlot(), threading.Event()

    def hold_s2():
        with slot.hold("s2"):
            acquired.set()

    with slot.hold("s1"):
        t = threading.Thread(target=hold_s2, daemon=True)
        t.start()
        assert acquired.wait(2), "s2 queued behind s1 — the slot is not per-session"
        t.join(2)


# --- the guarantees the slot exists to protect --------------------------------

def test_budget_ceiling_holds_under_concurrency(tmp_path):
    """§5.1: concurrent gray calls must not overspend the session ceiling."""
    jev = FakeJev(delay=0.005)  # widens the check→record window the slot closes
    engine = _engine(tmp_path, jev=jev, budget=Budget(session_calls=2))

    entries = _concurrently(
        lambda: engine.authorize_tool_call("bash", {"command": GRAY}), 10)

    assert jev.calls == 2                 # judgment was reached exactly twice
    assert engine.budget.calls_used == 2  # …so the ceiling held
    assert sum(e["decision"] == "allow" for e in entries) == 2
    assert sum(e.get("decision_reason") == "budget_exhausted" for e in entries) == 8


def test_decision_record_is_append_ordered_under_concurrency(tmp_path):
    """§10: `seq` is assigned in arrival order, so the log replays deterministically."""
    log = tmp_path / "d.jsonl"
    engine = _engine(tmp_path, jev=FakeJev(delay=0.002))

    _concurrently(lambda: engine.authorize_tool_call("bash", {"command": GRAY}), 12)

    lines = [json.loads(line) for line in log.read_text().strip().splitlines()]
    assert len(lines) == 12                              # none lost
    assert [line["seq"] for line in lines] == list(range(1, 13))  # written in order
