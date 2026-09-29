"""DecisionRecord — one account of a call: what the gate decided, and what a human did about it.

Spec §10 draws `outcome` on the decision line, but a decision is recorded the moment it is made
while a human's answer arrives later, or never. Rather than rewriting the stored line, this
module owns both stores and joins them on read: `records()` is the view spec §10 describes and
the eval loop replays, and `append`/`queue` are the write side the engine drives.
"""
from __future__ import annotations

from ..domain.model import Approval
from .approval import ApprovalStore
from .jsonl_log import JsonlLog


class DecisionRecord:
    """Implements ``application.ports.DecisionLog``, and adds the joined read side."""

    def __init__(self, log: JsonlLog, approvals: ApprovalStore):
        self.log = log
        self.approvals = approvals

    # --- write side: the engine's requirement ---------------------------------

    def append(self, entry: dict) -> dict:
        """Record one decision, append-only and in arrival order."""
        return self.log.append(entry)

    def is_approved(self, pattern: str) -> bool:
        """Consume a standing approval for a call pattern, if one exists."""
        return self.approvals.is_approved(pattern)

    def queue(self, call_id: str, command: str, pattern: str, why: dict) -> Approval:
        """Pause an escalated call for human review."""
        return self.approvals.pending(call_id, command, pattern, why)

    # --- read side: the viewer's and the eval loop's requirement ---------------

    def pending(self) -> list[Approval]:
        """Escalated calls still awaiting a human."""
        return [a for a in self.approvals.approvals.values() if a.status == "pending"]

    def decide(self, call_id: str, choice: str) -> Approval:
        """Answer one escalation: approve once, approve for the session, or deny."""
        return self.approvals.decide(call_id, choice)

    def records(self) -> list[dict]:
        """Every decision, with the human `outcome` joined in — the spec §10 view."""
        return [self._joined(entry) for entry in self.log.lines()]

    def _joined(self, entry: dict) -> dict:
        outcome = self.approvals.outcome(entry.get("call_id", ""))
        return {**entry, "outcome": outcome} if outcome else entry
