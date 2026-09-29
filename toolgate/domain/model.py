"""Domain model — the values the authorization decision is made of. Pure, no I/O."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

BAND_BLOCK = "block"
BAND_ALLOW = "allow"
BAND_GRAY = "gray"


@dataclass
class Principal:
    """The agent whose call is being authorized (spec §4)."""
    id: str
    name: str
    kind: str = "sdk"
    owner: str = "local"
    trust: str = "high"

    def entity(self) -> dict:
        return {
            "uid": {"type": "toolgate::Agent", "id": self.id},
            "attrs": {"name": self.name, "kind": self.kind, "owner": self.owner, "trust": self.trust},
            "parents": [],
        }


@dataclass
class AuthzResult:
    """A policy evaluation outcome, reduced to the three bands of spec §2.1."""
    decision: str  # "ALLOW" | "DENY" | "NO_DECISION"
    determining_policies: list[str] = field(default_factory=list)

    @property
    def band(self) -> str:
        # DENY→BLOCK, ALLOW→ALLOW, NO_DECISION→GRAY/ESCALATE
        return {"ALLOW": BAND_ALLOW, "DENY": BAND_BLOCK, "NO_DECISION": BAND_GRAY}[self.decision]


@dataclass
class Budget:
    """Per-session spend ceiling (spec §5.1). Exhaustion is fail-closed → ESCALATE."""
    session_calls: int = 500
    session_usd: float = 1.00
    call_usd: float = 0.05
    calls_used: int = 0
    usd_used: float = 0.0

    def exhausted(self) -> bool:
        return self.calls_used >= self.session_calls or self.usd_used >= self.session_usd

    def record(self, cost_usd: float, calls: int = 1) -> None:
        # `calls` counts actual Jev invocations — self-consistency spends N per gray call,
        # and the §5.1 ceiling must reflect real spend, not logical calls.
        self.calls_used += calls
        self.usd_used += cost_usd

    def remaining(self) -> dict:
        return {
            "remaining_usd": round(self.session_usd - self.usd_used, 2),
            "calls_remaining": self.session_calls - self.calls_used,
        }


@dataclass
class Approval:
    """A paused ESCALATE awaiting a human decision (spec §9)."""
    call_id: str
    command: str
    pattern: str
    why: dict
    status: str = "pending"  # pending | approved_once | approved_session | denied
    choice: str | None = None
    decided_at: str | None = None


_URL_RE = re.compile(r"https?://\S+")
_PATH_RE = re.compile(r"(?<![\w])~?/[^\s]*")

# Path shapes that hold credentials rather than content. Declared here because two modules need
# the same answer — the adapter maps a file operation on one into the secret zone, and the scrubber
# redacts it before the command is persisted — and a shape only one of them knows fails open. These
# used to be two lists, and the gate's was the weaker one: `.netrc` and `.env` were redacted in the
# record while the gate read the very same call as an ordinary workspace write — the evidence hidden
# and the action permitted — and `~/.ssh` was matched literally there, so an absolute path to the
# same store passed the gate unremarked.
SECRET_PATH_SHAPES = (".ssh", ".aws", ".gnupg", ".netrc", "id_rsa", "id_ed25519", ".env",
                      "/etc/shadow")


def call_pattern(command: str) -> str:
    """Normalize variable tokens (URLs, paths) to placeholders for `approve_session` matching."""
    s = _URL_RE.sub("<url>", command)
    s = _PATH_RE.sub("<path>", s)
    return s.strip()
