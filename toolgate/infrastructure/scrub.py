"""Command scrubbing for the decision log and echoed results (spec §10, §17.3).

The authorization path always sees the **raw** command — policies must judge it. Only what
we persist or hand back is scrubbed, so the gate never becomes the leak (spec §5).
"""
from __future__ import annotations

import re

from ..domain.model import SECRET_PATH_SHAPES

REDACTED = "«redacted»"
REDACTED_PATH = "«redacted-path»"

# Built from the one declaration in `domain.model`, which the adapter reads too: a credential store
# has to be recognised by the module that redacts it *and* the module that refuses it. This broadens
# redaction very slightly — `.env` no longer needs a word boundary — which is the safe direction for
# a second copy of a command, and it is what keeps the two consumers from drifting apart again.
_SECRET_PATH_RE = re.compile(
    r"\S*(?:" + "|".join(re.escape(shape) for shape in SECRET_PATH_SHAPES) + r")\S*")

# Ordered most-specific first; each rule replaces only the credential-shaped span.
_RULES: tuple[tuple[re.Pattern, str], ...] = (
    # URL userinfo: https://user:pass@host
    (re.compile(r"(?i)\b(https?://)[^/\s@]+:[^/\s@]+@"), rf"\1{REDACTED}@"),
    # Authorization / Proxy-Authorization header values (leave the closing quote intact)
    (re.compile(r"(?i)\b((?:proxy-)?authorization\s*:\s*\S+\s+)[^\s\"]+"), rf"\1{REDACTED}"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), rf"\1 {REDACTED}"),
    # key-shaped tokens: sk-…, apikey_…, ghp_…, xoxb-…
    (re.compile(r"\b(?:sk|pk|apikey|ghp|gho|ghs|xox[baprs])[-_][A-Za-z0-9_-]{8,}"), REDACTED),
    # NAME=value where NAME looks like a credential
    (re.compile(r"(?i)\b([A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|APIKEY|API_KEY"
                r"|CREDENTIAL|PRIVATE_KEY)[A-Za-z0-9_]*=)\S+"), rf"\1{REDACTED}"),
    # --password / --token / --api-key style flags (space- or =-separated)
    (re.compile(r"(?i)(--?[a-z0-9-]*(?:password|passwd|token|secret|api-?key)[a-z0-9-]*"
                r"(?:=|\s+))\S+"), rf"\1{REDACTED}"),
    # secret-path references (~/.ssh, ~/.aws, .netrc, id_rsa, /etc/shadow, …)
    (_SECRET_PATH_RE, REDACTED_PATH),
)


def scrub_command(command: str) -> str:
    """Return ``command`` with credential-shaped spans replaced by placeholders."""
    out = command
    for pattern, repl in _RULES:
        out = pattern.sub(repl, out)
    return out


def scrub_payload(value):
    """Recursively scrub every string in a JSON-shaped value, keys left alone.

    The trace records whole payloads — argument dicts, Cedar requests, judgment state — so the
    rule set above has to reach inside them. It is reused rather than restated, so a credential
    shape is defined in exactly one place and cannot be scrubbed on one path but not another.
    """
    if isinstance(value, str):
        return scrub_command(value)
    if isinstance(value, dict):
        return {key: scrub_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_payload(item) for item in value]
    return value
