"""PolicySet — locate, load, validate and version the Cedar policy artifacts.

Startup validation (spec §4) happens here: building the authorizers refuses to run on an
invalid policy set, so neither entry point can boot with a broken policy. The version is the
provenance label spec §10 stamps on every decision line.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from ..application.ports import Authorizer
from .authorizer import CedarEmbeddedAuthorizer

# Authored artifact versions (spec §10) — a provenance label, not a hash. Bump the matching
# component whenever the artifact changes; `tests/test_policy_version.py` fails until you do.
POLICY_VERSION = "gate=v4,judgment=v1,thresholds=v1,battery=v1"

# Each artifact as it stood when POLICY_VERSION was authored. The pin is a checkpoint, not a
# proof: it cannot tell a bump from a re-pin, it only guarantees a policy edit stops here.
AUTHORED_DIGESTS: dict[str, str] = {
    "schema.cedar": "sha256:dd005883d5ce0e1d06bc5e46be2a26cb48e08e53a5b50458bca129f0c8a29212",
    "gate.cedar": "sha256:8bac1f5e84f405c5febd51e76cd94de191296b2a7aedd78fec9eb56e519f6a7c",
    "judgment.cedar": "sha256:1deebd41627dae45c3045e350b239a573eaf9004ea0e8aef03c854d3474c3fc6",
}

_POLICY_DIR = Path(__file__).resolve().parent.parent / "domain" / "policies"


def artifact_digests(directory: Path | None = None) -> dict[str, str]:
    """The current digests of the pinned artifacts (the version test recomputes these)."""
    directory = _POLICY_DIR if directory is None else Path(directory)
    return {name: "sha256:" + hashlib.sha256((directory / name).read_bytes()).hexdigest()
            for name in AUTHORED_DIGESTS}


@dataclass(frozen=True)
class PolicySet:
    """The policy artifacts the gate decides with, loaded and validated once at startup."""
    schema_text: str
    gate_authorizer: Authorizer
    judgment_authorizer: Authorizer
    version: str

    @classmethod
    def load(cls, directory: Path | None = None) -> "PolicySet":
        directory = _POLICY_DIR if directory is None else Path(directory)
        schema = (directory / "schema.cedar").read_text()
        return cls(
            schema_text=schema,
            gate_authorizer=CedarEmbeddedAuthorizer(
                schema, (directory / "gate.cedar").read_text()),
            judgment_authorizer=CedarEmbeddedAuthorizer(
                schema, (directory / "judgment.cedar").read_text()),
            version=POLICY_VERSION,
        )
