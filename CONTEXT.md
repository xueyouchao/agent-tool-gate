# ToolGate

ToolGate is a guardrail that authorizes an AI agent's tool calls before they run. A call is judged
either by deterministic policy alone, or — in the uncertain middle — with a judgment model's read
of the situation.

## Language

### The decision

**The gate**:
The authorization point every tool call passes through before it is forwarded.
_Avoid_: proxy, middleware, interceptor

**The three bands**:
The gate's only outcomes — BLOCK, ALLOW, GRAY. They are not computed; they are the policy engine's
evaluation result.
_Avoid_: verdict, status, result

**The gray middle**:
The GRAY band: no policy forbid matched and no permit matched, so the call is undecided and must
reach judgment.
_Avoid_: uncertain, unknown, maybe

**Gate policies** (phase 1):
Policies over static facts that no model should ever be asked about.
_Avoid_: static rules, prefilter, level 1

**Judgment policies** (phase 2):
Policies over semantics the policy engine cannot see, consuming attributes thresholded from the battery.
_Avoid_: semantic rules, model rules, level 2

**Escalation**:
The phase-2 outcome when nothing matched: the call is paused for a human instead of being decided.
_Avoid_: pending, queued, review

**Decision record**:
The durable account of one call — what the gate decided, and once known, what a human did about it.
_Avoid_: log line, audit entry, event

### The judgment

**The battery**:
The fixed set of questions put to the judgment model about one call.
_Avoid_: prompt, questionnaire, survey

**Thresholding**:
Turning the model's probabilities into the typed attributes policies consume, so that an
undecided critical question can never satisfy a permit.
_Avoid_: scoring, mapping, post-processing

**Blast radius**:
The one battery question answered by choice rather than probability, and the only one carrying
real confidence.
_Avoid_: scope, impact

**Self-consistency**:
Asking the same question set several times, so that disagreement is itself a reason to escalate.
_Avoid_: sampling, variance, retries

**Drift**:
Disagreement across self-consistency samples.
_Avoid_: instability, variance

### Governance

**Approval**:
A human's decision on an escalated call — allowed once, allowed for the session, or denied.
_Avoid_: override, consent, sign-off

**Call pattern**:
A command with its variable parts — URLs, paths — replaced by placeholders, so one approval can
cover later calls of the same shape.
_Avoid_: fingerprint, signature, template

**Session**:
The scope over which spend, approvals and decisions are accounted.
_Avoid_: conversation, run, context

**Budget**:
The spend ceiling bounding a session's autonomy. Exhaustion escalates; it never allows.
_Avoid_: quota, limit, cap

**Policy version**:
The provenance label recording which policy and threshold artifacts decided a call.
_Avoid_: revision, policy hash

### Modes and boundaries

**Gateway mode**:
Operating as a transparent proxy, where the battery is reduced to questions answerable without
conversation context.
_Avoid_: proxy mode, reduced mode

**SDK mode**:
Operating inside an agent that can supply intent, allowing the full battery.
_Avoid_: full mode, embedded mode

**The adapter**:
The deterministic parse from a tool call into policy entities and context — the garbage-in
boundary. It never asks a model.
_Avoid_: normalizer, mapper, translator

**Principal**:
The agent a call is attributed to.
_Avoid_: user, caller, actor
