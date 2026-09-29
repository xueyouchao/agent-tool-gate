"""Embedded Cedar authorizer over ``cedarpy`` — the §3 engine seam."""
from __future__ import annotations

import json

import cedarpy

from ..domain.model import AuthzResult

_DECISION_MAP = {"Allow": "ALLOW", "Deny": "DENY", "NoDecision": "NO_DECISION"}


class CedarEmbeddedAuthorizer:
    def __init__(self, schema_text: str, policy_text: str):
        self.schema_text = schema_text
        self.policy_text = policy_text
        self.schema = cedarpy.Schema.from_str(schema_text)
        self.policies = cedarpy.PolicySet.from_str(policy_text)
        self._validate()

    def _validate(self) -> None:
        # startup validation (spec §4): refuse to run on an invalid policy set
        vr = cedarpy.validate_policies(self.policy_text, self.schema)
        if not vr.validation_passed:
            raise ValueError("policy validation failed: " + "; ".join(str(e) for e in vr.errors))

    def authorize(self, request: dict, entities: list[dict]) -> AuthzResult:
        res = cedarpy.is_authorized(request, self.policies, json.dumps(entities), self.schema)
        idmap = res.diagnostics.id_annotations_by_reason
        ids = [idmap.get(r, r) for r in res.diagnostics.reasons]

        name = res.decision.name
        if res.diagnostics.errors:
            # skip-on-error: a runtime evaluation error fails closed (never a hard BLOCK)
            decision = "NO_DECISION"
        elif name == "Deny" and not ids:
            # Cedar default-deny: no permit and no forbid matched → the GRAY band
            decision = "NO_DECISION"
        else:
            decision = _DECISION_MAP[name]
        return AuthzResult(decision=decision, determining_policies=ids)
