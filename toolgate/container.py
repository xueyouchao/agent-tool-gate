"""Composition root — the ``dependency-injector`` container that wires the object graph.

Every environment-specific value comes from ``config`` (with safe defaults, so the container
is usable with no configuration). Tests inject doubles with ``container.<provider>.override(...)``
or ``container.override_providers(...)`` — see ``tests/test_container.py``.
"""
from __future__ import annotations

from dependency_injector import containers, providers

from .application.engine import Engine
from .application.session_slot import SessionSlot
from .domain.model import Budget, Principal
from .infrastructure.approval import ApprovalStore
from .infrastructure.decision_record import DecisionRecord
from .infrastructure.env_file import load_env_file
from .infrastructure.jev import JevClient
from .infrastructure.jsonl_log import JsonlLog
from .infrastructure.policy_set import PolicySet
from .infrastructure.trace import TraceLog
from .interfaces.gateway import ToolGateProxy

# `.env.example` tells you to copy it to `.env`, so read it before anything binds a credential:
# `JevClient` looks in `os.environ`, and an unread file would make a configured key look exactly
# like one that was never set. Existing variables win, so an explicit export is never overridden.
load_env_file()

DEFAULTS: dict = {
    "agent_id": "agent-1",
    "agent_name": "cli-agent",
    "log_path": "decisions.jsonl",
    "approvals_path": "pending_approvals.json",
    # the boundary trace the viewer reads: scrubbed unless trace_raw is asked for by name
    "trace_path": "traces.jsonl",
    "trace_raw": False,
    "consistency_samples": 3,
    "typesafe_api_key": None,  # None → JevClient falls back to TYPESAFE_API_KEY / JEV_API_KEY
    "budget": {"session_calls": 500, "session_usd": 1.00},
}


class Container(containers.DeclarativeContainer):
    """Wires domain → infrastructure → application → interface."""

    config = providers.Configuration(default=DEFAULTS)

    principal = providers.Factory(Principal, id=config.agent_id, name=config.agent_name)

    # The policies are loaded and validated once at startup; the engine is handed the loaded
    # authorizers rather than a path, so an invalid policy refuses to boot (spec §4).
    policy_set = providers.Singleton(PolicySet.load)

    # shared singletons: one Jev client, one approval store, one append-only log (its sequence
    # counter is per-instance), one decision record over both, and one session slot (a
    # per-engine slot would serialize nothing across engines)
    jev_client = providers.Singleton(JevClient, api_key=config.typesafe_api_key)
    approval_store = providers.Singleton(ApprovalStore, path=config.approvals_path)
    jsonl_log = providers.Singleton(JsonlLog, path=config.log_path)
    decision_record = providers.Singleton(DecisionRecord, log=jsonl_log, approvals=approval_store)
    session_slot = providers.Singleton(SessionSlot)
    # one trace sink, scrubbed by default — see infrastructure/trace.py for why
    trace_log = providers.Singleton(TraceLog, path=config.trace_path, raw=config.trace_raw)

    budget = providers.Factory(Budget, session_calls=config.budget.session_calls,
                               session_usd=config.budget.session_usd)

    engine = providers.Factory(
        Engine,
        principal=principal,
        gate_authorizer=policy_set.provided.gate_authorizer,
        judgment_authorizer=policy_set.provided.judgment_authorizer,
        decisions=decision_record,
        policy_version=policy_set.provided.version,
        jev=jev_client,
        budget=budget,
        consistency_samples=config.consistency_samples,
        slot=session_slot,
        trace=trace_log,
    )

    # the upstream only exists after connecting to servers → supplied at call time:
    #   proxy = container.proxy(upstream=group)
    proxy = providers.Factory(ToolGateProxy, engine=engine)
