"""Approval store — JSON persistence for paused ESCALATEs and their human decisions.

Overrides are exposed as `outcome` and fed to the eval loop (spec §9): a human ALLOW on a
forbidden pattern is a policy bug report.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from ..domain.model import Approval


class ApprovalStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.approvals: dict[str, Approval] = {}
        self.one_shot_approved: set[str] = set()   # call patterns allowed exactly once
        self.session_approved: set[str] = set()    # call patterns allowed for the session
        self._load()

    def _load(self) -> None:
        if self.path and self.path.exists():
            data = json.loads(self.path.read_text())
            self.approvals = {k: Approval(**v) for k, v in data.get("approvals", {}).items()}
            self.one_shot_approved = set(data.get("one_shot_approved", []))
            self.session_approved = set(data.get("session_approved", []))

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "approvals": {k: asdict(v) for k, v in self.approvals.items()},
            "one_shot_approved": sorted(self.one_shot_approved),
            "session_approved": sorted(self.session_approved),
        }
        self.path.write_text(json.dumps(data, indent=2))

    def pending(self, call_id: str, command: str, pattern: str, why: dict) -> Approval:
        """`command` is stored for human review (already scrubbed); `pattern` drives matching
        and is derived from the raw command by the caller."""
        a = Approval(call_id=call_id, command=command, pattern=pattern, why=why)
        self.approvals[call_id] = a
        self._save()
        return a

    def is_approved(self, pattern: str) -> bool:
        """Consume one-shot approvals; session approvals persist."""
        if pattern in self.session_approved:
            return True
        if pattern in self.one_shot_approved:
            self.one_shot_approved.discard(pattern)
            self._save()
            return True
        return False

    def decide(self, call_id: str, choice: str) -> Approval:
        a = self.approvals[call_id]
        if choice == "approve_once":
            self.one_shot_approved.add(a.pattern)
            a.status = "approved_once"
        elif choice == "approve_session":
            self.session_approved.add(a.pattern)
            a.status = "approved_session"
        elif choice == "deny":
            a.status = "denied"
        else:
            raise ValueError(f"unknown choice: {choice}")
        a.choice = choice
        a.decided_at = datetime.now(timezone.utc).isoformat()
        self._save()
        return a

    def outcome(self, call_id: str) -> dict | None:
        """The spec §10 `outcome` object for an approved/denied call."""
        a = self.approvals.get(call_id)
        if not a or a.status == "pending":
            return None
        if a.status == "denied":
            return {"type": "human_denied"}
        return {"type": "human_approved", "choice": a.choice}
