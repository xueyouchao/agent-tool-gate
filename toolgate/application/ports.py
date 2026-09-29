"""The ports the application drives (spec §3).

Declared here rather than beside the adapters, because the consumer owns the interface: the
engine needs "something that evaluates policies", "somewhere a decision is recorded",
"something that answers the battery" and "somewhere a trace of the decision is kept", and needs
to know nothing about Cedar, JSONL or HTTP to say so.
"""
from __future__ import annotations

from typing import Protocol

from ..domain.battery import JevResponse
from ..domain.model import Approval, AuthzResult


class Authorizer(Protocol):
    """Evaluates one request against a policy set, reduced to the three bands (spec §2.1)."""

    def authorize(self, request: dict, entities: list[dict]) -> AuthzResult: ...


class DecisionLog(Protocol):
    """The decision record as the engine needs it: append, and pause for a human.

    Deliberately the write-side half only. The engine never reads its own log — joining a
    decision to the outcome a human later chose belongs to the reader, so the record is
    append-only and "what the gate decided" is never rewritten once stored.
    """

    def append(self, entry: dict) -> dict: ...

    def is_approved(self, pattern: str) -> bool: ...

    def queue(self, call_id: str, command: str, pattern: str, why: dict) -> Approval: ...


class Jev(Protocol):
    """Puts the battery to the judgment model and returns its answers (spec §5.2).

    Failures are part of the contract: a transport fault raises ``JevOutage`` and an
    off-contract body raises ``JevMalformed``, so the engine fails closed either way.
    """

    def invoke(self, state: dict) -> JevResponse: ...


class Trace(Protocol):
    """Records what crossed each boundary of one call — the viewer's raw material.

    Two rules bind every implementation. First: **scrub by default.** A trace is a second copy
    of the very arguments the gate exists to catch, so the safe setting has to be the one you
    get without asking. Raw is opt-in and explicit. Second: the engine treats a failing trace as
    lost diagnostics, never as a reason to refuse a call — the decision log is the security
    record, this is a convenience, and a full disk must not become a denial of service.
    """

    def append(self, event: dict) -> None: ...
