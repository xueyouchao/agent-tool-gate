"""The policy version is the provenance spec §10 stamps on every decision line.

A policy edit that keeps the old label silently costs the eval loop its ability to explain a
decision diff, so the artifacts are pinned here: change one and this fails, which is the prompt
to bump the matching component in ``policy_set.POLICY_VERSION`` and re-pin the digest.
"""
from __future__ import annotations

from toolgate.infrastructure.policy_set import (AUTHORED_DIGESTS, POLICY_VERSION,
                                                artifact_digests)


def test_the_pin_covers_every_artifact_a_policy_set_loads():
    assert set(AUTHORED_DIGESTS) == {"schema.cedar", "gate.cedar", "judgment.cedar"}


def test_cedar_artifacts_match_the_pinned_version():
    changed = {name for name, digest in artifact_digests().items()
               if AUTHORED_DIGESTS[name] != digest}
    assert not changed, (
        f"{sorted(changed)} changed while policy_version is still {POLICY_VERSION!r}. "
        "Bump the matching component in POLICY_VERSION and re-pin AUTHORED_DIGESTS."
    )
