# toolgate — Two-Phase Authorization for AI Agent Tool Calls

I built this to work out one question: how do you let a probabilistic model influence a security
decision without ever letting it *be* the decision? The first version was a hand-rolled
allowlist/blocklist with a weighted risk score — and the score could authorize things, which is exactly
the property I wanted to delete. So I replaced it with a two-phase Cedar authorize design: the policy
engine became the sole decision owner, and the model got demoted to perception.

```
tool call → adapter: args → entities + context        (parse only — never a model)
   → Cedar #1 "gate policies" — facts: paths, scopes, subcommands, verbs       ~1 ms, $0
        forbid → BLOCK
        permit → ALLOW        (the model never sees it)
        nothing → GRAY
   → Jev battery — one call, 8 questions in parallel, "what does this mean?"   ~275 ms, $0.00007
   → threshold: dead-band + confidence floor → typed tri-state attributes
   → Cedar #2 "judgment policies"
        forbid → BLOCK
        permit → ALLOW
        nothing → ESCALATE    (a human decides)
   → one JSONL decision record
```

The part I like most is that the three bands are not implemented anywhere. Cedar authorizes a request
iff some permit matches and no forbid does — so "nothing matched" is already a distinct outcome, and
the gate just reads it as *hand off to judgment* in phase 1 and *hand off to a human* in phase 2.
Default-deny becomes a routing signal instead of a refusal, and there is no bespoke band logic to get
wrong.

The confidence floor is the trick that makes a model safe to consume. Cedar attributes are `Bool`, so
"undecided" cannot be a third value; instead an undecided *critical* answer sets `confidence_floor = 1`,
and the permit requires `confidence_floor == 0`. An ambiguous answer therefore cannot satisfy a permit —
structurally, not by convention. The same state is asked three times in parallel, and any drift above
0.2 forces an escalation. Adding a question is a policy edit: no retraining, no latency change.

Jev labels reality. Cedar decides what's permitted. Nothing decides twice. The model can never allow or
deny — it can only change which attributes the policy engine sees.

Microservice topology

```
[mcp]       MCP gateway — gates tools/call, passes other channels through
[adapter]   tool args → Cedar entities + context (deterministic, never asks a model)
[cedar]     cedarpy in-process — gate policies (facts) + judgment policies (semantics)
[jev]       TypeSafe judgment model — one call, 8 questions, ~1,770 input tok
[thresh]    probabilities → tri-state context + confidence floor + drift
[approval]  escalations: approve once / for the session (by call pattern) / deny
[budget]    per-session spend ceiling — exhaustion escalates, never allows
[record]    append-only decisions.jsonl + boundary trace, joined on read
[cli]       log replay · approve · interactive prompt · live 2D viewer with a decide box
```

Every call is one JSONL line: the policy ids that decided it, the model's raw answers, latency, tokens,
cost, and later what the human did about it. The log is the audit trail, the eval set and the tuning
data at once — the design principle is that yesterday's traffic is tomorrow's policy regression suite.

Fail-closed is a closed vocabulary rather than a slogan: an unreachable model, a malformed response and
an exhausted budget all route to a human. The one exception is a defect *inside* the engine — nothing was
paused and the caller was already told its call failed, so that line is recorded as `block` with
`decision_reason: engine_error` and the original error is re-raised unchanged.

Three defects found in review were all the same shape — a declaration that was still true where it was
written and no longer true where it was enforced. The gate's credential-path list was shorter than the
scrubber's, so three credential stores were *allowed* while the record redacted them. The judgment
client's default silently reached a billed API, and seven tests were passing on real calls because an
outage and a real answer land in the same band. Each fix was proven by reverting it and requiring the
new test to fail; two of those tests turned out to assert things that were also true when nothing
happened at all.

It runs on my VPS (Python 3.12, dependency-injector, a single composition root), with a public decide
box so you can put a command to the real gate.

245 tests · 5/5 credential paths blocked at phase 1 | phase 1 ~1.0 ms median at $0 | phase 2 ~275.2 ms
median at ~$0.000074 per judgment (~1,770 input tok), p95 314 ms | 23 real judgments, $0.0017 total |
demo: allowed 1 · blocked 3 · pending 1 · approved 1 · denied 1

Known gap: the state sent to the judgment model is not scrubbed before it leaves the machine (the
decision record and the trace are). A guardrail that leaks what it inspects has failed at its own
premise — it is one line, and it is in the README's known limitations.

Try it here: https://toolgate.srv1567269.hstgr.cloud/ (the decide box runs the real gate — it authorizes
and records; it does not execute) and have fun.

Source code: https://github.com/xueyouchao/agent-tool-gate

Deeper write-ups in the repo: [the design](technical-write-up.md) — two-phase authorization, the three
bands, thresholding — and [the three defects](defect-case-studies.md), with their measurements.
