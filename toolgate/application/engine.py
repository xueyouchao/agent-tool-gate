"""Engine — the authorize-tool-call use case: Cedar fast path, then Jev on the gray middle.

The engine is handed its world: two authorizers, a decision record, a judgment client and a
budget. It reads no policy files and builds none of the collaborators it could be given, so a
test supplies a double without needing a path to dodge.

It owns the decision *and* its log line. Standing human overrides (spec §9) are applied here,
so an approved call is recorded as an `allow` with `decision_reason: "session_approved"` —
never an `escalate` that quietly proceeded.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

from ..domain.battery import JevMalformed, JevOutage
from ..domain.model import Budget, Principal, call_pattern
from ..domain.thresholds import detect_drift, rank_score, threshold
from ..infrastructure.adapter import normalize
from ..infrastructure.scrub import scrub_command
from .ports import Authorizer, DecisionLog, Jev, Trace
from .session_slot import SessionSlot

# A decided phase-1 gate maps 1:1 onto the log vocabulary. NO_DECISION (the GRAY middle) has
# no band here — it falls through to phase 2 instead of becoming a decision.
_PHASE1_BAND = {"DENY": "block", "ALLOW": "allow"}

# The facts the viewer's last stage reads. Declared once so the decided path and the failed path
# cannot trace different shapes for the same boundary.
_VERDICT_FACTS = ("decision", "decision_reason", "determining_policies", "phase",
                  "rank_score", "latency_ms", "budget")


@dataclass(frozen=True)
class _Call:
    """A tool call under authorization — the values every phase's log line needs."""
    call_id: str
    session: str
    tool: str
    command: str
    request: dict


class Engine:
    def __init__(
        self,
        principal: Principal,
        *,
        gate_authorizer: Authorizer,
        judgment_authorizer: Authorizer,
        decisions: DecisionLog,
        policy_version: str,
        jev: Jev,
        budget: Budget | None = None,
        consistency_samples: int = 3,
        slot: SessionSlot | None = None,
        mode: str = "enforce",
        trace: Trace | None = None,
    ):
        self.principal = principal
        self.policy_version = policy_version
        self.mode = mode
        self.consistency_samples = consistency_samples
        self.gate_authorizer = gate_authorizer
        self.judgment_authorizer = judgment_authorizer
        self.decisions = decisions
        # Required, like the authorizers: the engine is handed its world, so it cannot quietly
        # build a client that reaches the network. A default here meant a caller that forgot this
        # argument made billed HTTP calls — the port was declared and then bypassed.
        self.jev = jev
        self.budget = budget or Budget()
        # The composition root supplies the shared Singleton; a private slot would serialize
        # nothing once two engines exist.
        self.slot = slot or SessionSlot()
        # Optional: a boundary trace for the viewer. Absent means the boundaries stay unnamed.
        self.trace = trace

    def authorize_tool_call(self, tool: str, args: dict, *, session: str = "s1") -> dict:
        # Spec §10: the gate issues one decision at a time. The slot spans the whole decision,
        # because the ceiling is checked before the judgment call and recorded after it.
        with self.slot.hold(session):
            return self._authorize(tool, args, session=session)

    def _authorize(self, tool: str, args: dict, *, session: str) -> dict:
        command = args.get("command", "")
        call_id = str(uuid.uuid4())[:8]
        start = time.perf_counter()
        self._trace(call_id, session, "ingress", {"tool": tool, "args": args})
        try:
            entities, request = normalize(command, self.principal)
            call = _Call(call_id, session, tool, command, request)
            self._trace(call_id, session, "normalize",
                        {"command": command, "entities": entities, "request": request})

            r1 = self.gate_authorizer.authorize(request, entities)
            # Only the answer. The inputs are the pair `normalize` recorded one boundary back,
            # handed to Cedar unchanged — repeating them here doubled the bulk of nearly every
            # trace line and made it look as though Cedar wanted the entities in two places.
            # `test_trace.py` pins that this decision is the one that recorded pair produces.
            self._trace(call_id, session, "gate",
                        {"decision": r1.decision,
                         "determining_policies": r1.determining_policies})
            band = _PHASE1_BAND.get(r1.decision)
            if band is None:  # GRAY → phase 2 (Jev judgment)
                entry = self._phase2(call, args, entities, start)
            else:
                entry = self._build(call, phase=1, decision=band, fast_path=band,
                                    policies=r1.determining_policies,
                                    latency=time.perf_counter() - start)
            entry = self._record(entry, command)
            # after _record, so the trace shows the decision that was actually taken — including
            # a standing human override, which turns an escalate into an allow here
            self._trace_verdict(call_id, session, entry)
            return entry
        except Exception:
            # A defect anywhere above used to leave no record at all, which made the one call the
            # gate could not account for the one it happened to fail on. Record it, then re-raise:
            # the caller still gets a fail-closed refusal, exactly as it did before.
            self._record_failure(call_id, session, tool, command, start)
            raise

    def _trace(self, call_id: str, session: str, boundary: str, payload: dict) -> None:
        """Emit one boundary event, and never let diagnostics break the decision.

        The decision log is the security record; this is a convenience. A trace sink that fails —
        a full disk, an unwritable path — must not become a way to refuse tool calls.
        """
        if self.trace is None:
            return
        try:
            self.trace.append({"call_id": call_id, "session": session, "boundary": boundary,
                               "ts": datetime.now(timezone.utc).isoformat(), "payload": payload})
        except Exception:  # noqa: BLE001 — see the docstring
            pass

    def _trace_verdict(self, call_id: str, session: str, entry: dict) -> None:
        self._trace(call_id, session, "verdict",
                    {key: entry.get(key) for key in _VERDICT_FACTS})

    def _record_failure(self, call_id: str, session: str, tool: str, command: str,
                        start: float) -> None:
        """Last resort: the gate could not decide, so the record still has to say so.

        Only an *unexpected* failure reaches here. The modelled ones — ``jev_outage``,
        ``jev_malformed``, ``budget_exhausted`` — escalate through ``_escalate`` by design, because a
        human can still answer them. A defect is different: nothing was paused for anyone, and the
        caller is told the call was refused, so the band recorded here is BLOCK — the record and the
        caller agree.

        Appended directly and never queued: offering a human an approval for a call the model was
        told had failed would be an approval of nothing, and approving it would rewrite this line to
        an `allow` for a call that never ran.

        Guarded, because failing to record a failure must not replace the original error with a
        worse one.
        """
        try:
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "session": session,
                "call_id": call_id,
                "tool": tool,
                "args_digest": "sha256:" + hashlib.sha256(command.encode()).hexdigest()[:8],
                "command": scrub_command(command),
                "principal": f'Agent::"{self.principal.id}"',
                "action": "execute",
                "resource": "unknown",     # `normalize` may be what failed, so there may be none
                "phase": 0,                # never completed a phase
                "mode": self.mode,
                "policy_version": self.policy_version,
                "fast_path_decision": None,
                "determining_policies": [],
                "answers": None,
                "decision": "block",
                "decision_reason": "engine_error",
                "rank_score": None,
                "latency_ms": round((time.perf_counter() - start) * 1000, 3),
                "tokens_in": None,
                "tokens_out": None,
                "cost_usd": 0.0,
                "budget": self.budget.remaining(),
            }
            self.decisions.append(entry)
            self._trace_verdict(call_id, session, entry)
        except Exception:  # noqa: BLE001 — see the docstring
            pass

    def _phase2(self, call, args, entities, start) -> dict:
        if self.budget.exhausted():
            self._trace(call.call_id, call.session, "judgment",
                        {"answers": None, "error": "budget_exhausted"})
            return self._escalate(call, start, "budget_exhausted")

        state = {
            "tool": call.tool,
            "args": args,
            "cwd": "/workspace",
            "user_intent": "",
            "project_facts": "",
            "recent_actions": [],
            "environment": "local",
        }
        try:
            samples = self._jev_samples(state)
        except JevOutage:
            self._trace(call.call_id, call.session, "judgment",
                        {"state": state, "answers": None, "error": "jev_outage"})
            return self._escalate(call, start, "jev_outage")
        except JevMalformed:
            self._trace(call.call_id, call.session, "judgment",
                        {"state": state, "answers": None, "error": "jev_malformed"})
            return self._escalate(call, start, "jev_malformed")

        answers = samples[0].answers
        ctx2 = threshold(answers)
        drift = detect_drift([s.answers for s in samples])
        if drift:
            ctx2["confidence_floor"] = 1

        call.request["context"].update(ctx2)
        r2 = self.judgment_authorizer.authorize(call.request, entities)
        self._trace(call.call_id, call.session, "judgment",
                    {"state": state, "samples": [s.answers for s in samples], "answers": answers,
                     "thresholds": ctx2, "drift": drift, "decision": r2.decision,
                     "determining_policies": r2.determining_policies})

        cost_usd = sum(s.cost_usd for s in samples)
        self.budget.record(cost_usd, calls=len(samples))
        return self._build(call, phase=2,
                           decision={"DENY": "block", "ALLOW": "allow",
                                     "NO_DECISION": "escalate"}[r2.decision],
                           fast_path=None, policies=r2.determining_policies,
                           latency=time.perf_counter() - start, answers=answers,
                           tokens_in=sum(s.input_tokens for s in samples),
                           tokens_out=sum(s.output_tokens for s in samples),
                           cost_usd=cost_usd)

    def _jev_samples(self, state: dict) -> list:
        """Self-consistency (§6): N samples in parallel → single-call wall time, not N×."""
        if self.consistency_samples <= 1:
            return [self.jev.invoke(state)]
        with ThreadPoolExecutor(max_workers=self.consistency_samples) as ex:
            return list(ex.map(lambda _: self.jev.invoke(state),
                               range(self.consistency_samples)))

    def _escalate(self, call, start, reason) -> dict:
        return self._build(call, phase=2, decision="escalate", fast_path=None, policies=[],
                           latency=time.perf_counter() - start, reason=reason)

    def _record(self, entry: dict, command: str) -> dict:
        """Apply a standing human override, else queue the escalation; write one decision line."""
        if entry["decision"] == "escalate":
            pattern = call_pattern(command)
            if self.decisions.is_approved(pattern):
                entry["decision"] = "allow"
                entry["decision_reason"] = "session_approved"
            else:
                self.decisions.queue(entry["call_id"], entry["command"], pattern,
                                     {"policy_ids": entry["determining_policies"],
                                      "answers": entry.get("answers"),
                                      "decision_reason": entry.get("decision_reason")})
        # return what was recorded, not what was handed in: `append` is where the sequence number
        # is assigned, and a caller that wants to point at this line needs it
        return self.decisions.append(entry)

    def _build(self, call, *, phase, decision, fast_path, policies, latency, answers=None,
               tokens_in=None, tokens_out=None, cost_usd: float = 0.0, reason=None) -> dict:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session": call.session,
            "call_id": call.call_id,
            "tool": call.tool,
            # raw command is judged, never persisted: digest for correlation, scrubbed for review
            "args_digest": "sha256:" + hashlib.sha256(call.command.encode()).hexdigest()[:8],
            "command": scrub_command(call.command),
            "principal": f'Agent::"{self.principal.id}"',
            "action": "execute",
            "resource": f'{call.request["resource"]["type"].split("::")[-1]}'
                        f'::"{call.request["resource"]["id"]}"',
            "phase": phase,
            "mode": self.mode,
            "policy_version": self.policy_version,
            "fast_path_decision": fast_path,
            "determining_policies": policies,
            "answers": answers,
            "decision": decision,
            "rank_score": rank_score(answers) if answers else None,
            "latency_ms": round(latency * 1000, 3),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": round(cost_usd, 6),
            "budget": self.budget.remaining(),
        }
        if reason:
            entry["decision_reason"] = reason
        return entry
