I built this to answer one question: how do you let a probabilistic model influence a security decision without ever letting it BE the decision?

My first version scored risk with a weighted allowlist/blocklist — and the score could authorize things. That is the property I set out to delete. So: two-phase authorization, where the policy engine owns the decision and the model is demoted to perception.

tool call → adapter parses args into facts (never a model)
→ Cedar #1, facts: paths, scopes, verbs — ~1 ms, $0
   forbid → BLOCK | permit → ALLOW | nothing → GRAY
→ Jev battery — one call, 8 questions in parallel, ~275 ms, $0.00007
→ threshold: dead-band + confidence floor → typed tri-state attributes
→ Cedar #2, semantics
   forbid → BLOCK | permit → ALLOW | nothing → ESCALATE to a human

The three bands are not implemented anywhere. Cedar authorizes iff a permit matches and no forbid does, so "nothing matched" is already a distinct outcome — read as hand off to judgment in phase 1, hand off to a human in phase 2. Default-deny becomes a routing signal instead of a refusal, with no bespoke band logic to get wrong.

The confidence floor is what makes a model safe to consume. Cedar attributes are Bool, so "undecided" cannot be a third value: an undecided critical answer sets confidence_floor=1 and the permit requires confidence_floor==0. An ambiguous answer therefore cannot satisfy a permit — structurally, not by convention. The same state is asked 3x in parallel; drift above 0.2 escalates.

Jev labels reality. Cedar decides what is permitted. Nothing decides twice.

Fail-closed is a closed vocabulary, not a slogan: unreachable model, malformed response, exhausted budget — all route to a human. The one exception is a defect inside the engine: nothing was paused and the caller was already told its call failed, so it records a block, not a phantom approval in someone's queue.

Three defects found in review were the same shape: a declaration still true where it was written, no longer true where it was enforced. The gate's credential list was shorter than the scrubber's, so three credential stores were ALLOWED while the record redacted them. Each fix was proven by reverting it and requiring the new test to fail.

245 tests | 5/5 credential paths blocked at phase 1 | phase 1 ~1.0 ms median at $0 | phase 2 ~275 ms median at ~$0.000074 per judgment | p95 314 ms | 23 real judgments, $0.0017 total

Known gap: the state sent to the judgment model is not scrubbed (the record and trace are). A guardrail that leaks what it inspects has failed at its own premise — it is one line, and it is in the README's limitations.

Try the real gate (it authorizes and records; it does not execute): https://toolgate.srv1567269.hstgr.cloud/
Source: https://github.com/xueyouchao/agent-tool-gate
