"""Shared test doubles and the single engine-construction path tests should use."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from toolgate.application.engine import Engine
from toolgate.domain.battery import NOUL_QUESTIONS, JevResponse, validate_jev_response
from toolgate.domain.model import Principal
from toolgate.infrastructure.approval import ApprovalStore
from toolgate.infrastructure.decision_record import DecisionRecord
from toolgate.infrastructure.jsonl_log import JsonlLog
from toolgate.infrastructure.policy_set import PolicySet

# Thresholding treats every noul question identically; the polarity lives in the policy. The
# clean-call permit requires `well_formed == true`, so being well-formed is the *good* outcome
# and must score high. Every other question is a risk, so a benign call scores it low.
_GOOD_WHEN_HIGH = {"well_formed"}


def clean_answers(**overrides) -> dict:
    """A full battery of confident, benign answers — satisfying the clean-call permit."""
    answers = {q: {"type": "noul", "noul": 0.95 if q in _GOOD_WHEN_HIGH else 0.05}
               for q in NOUL_QUESTIONS}
    answers["blast_radius"] = {"type": "choice", "choice": "local_workspace",
                               "probability": 0.9, "confidence": 0.95}
    answers.update(overrides)
    return answers


class FakeJev:
    """A judgment client that honours the §5.2 contract without touching the network."""

    def __init__(self, answers: dict | None = None, *, input_tokens: int = 0,
                 delay: float = 0.0):
        self.answers = clean_answers() if answers is None else answers
        self.input_tokens = input_tokens
        self.delay = delay
        self.calls = 0
        self._count_lock = threading.Lock()

    def invoke(self, state: dict) -> JevResponse:
        with self._count_lock:
            self.calls += 1
        validate_jev_response(self.answers)  # an off-contract double must fail like the real one
        if self.delay:  # widens the race window for the concurrency tests
            time.sleep(self.delay)
        return JevResponse(answers=self.answers, input_tokens=self.input_tokens)


def keyless(monkeypatch) -> None:
    """Force the outage path: a real ``JevClient`` with no credential, whatever this machine holds.

    The credential arrives two ways — exported, or read from `.env` when `toolgate.container` is
    first imported, which happens partway through a pytest run. So a test that means to exercise the
    outage has to say so. Inheriting "no key" made it depend on which files had already run: four
    tests in ``test_m4`` and one in ``test_m3`` passed only because of where they sat in the order.
    """
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("JEV_API_KEY", raising=False)


_loaded: PolicySet | None = None


def policy_set() -> PolicySet:
    """The shipped policy artifacts, loaded and validated once per test session."""
    global _loaded
    if _loaded is None:
        _loaded = PolicySet.load()
    return _loaded


def make_record(log_path, approvals: ApprovalStore | None = None) -> DecisionRecord:
    """A decision record beside the given log file."""
    log_path = Path(log_path)
    return DecisionRecord(JsonlLog(log_path),
                          approvals or ApprovalStore(log_path.parent / "pa.json"))


def make_engine(log_path, *, jev=None, budget=None, approvals=None, samples=1,
                principal=None, slot=None, trace=None) -> Engine:
    """An engine wired to the real policies — the one construction path tests should use.

    The judge defaults to a double, so a test that never thinks about the judgment model still gets
    a free, hermetic one. A real ``JevClient`` is asked for by name, in the tests that mean it: the
    outage paths, which force a keyless client, and the live file, which wants the wire.
    """
    ps = policy_set()
    return Engine(principal or Principal(id="agent-1", name="cli-agent"),
                  gate_authorizer=ps.gate_authorizer,
                  judgment_authorizer=ps.judgment_authorizer,
                  decisions=make_record(log_path, approvals),
                  policy_version=ps.version,
                  jev=jev if jev is not None else FakeJev(),
                  budget=budget,
                  consistency_samples=samples,
                  slot=slot,
                  trace=trace)
