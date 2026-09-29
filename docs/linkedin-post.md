How do you let a probabilistic model influence a security decision without ever letting it BE the decision?

My first version scored risk with a weighted allowlist — and the score could authorize things. That's the part I wanted gone. So I went to two phases: the policy engine makes the decision, the model just describes what it sees.

Every call:

1. The adapter parses the arguments into facts. No model. ~1 ms
2. Cedar gate policies rule on those facts. $0
🚫 BLOCK · ✅ ALLOW — the model never even sees it · ⚠️ nothing matched → hand it off
3. A judgment model answers 8 questions about what the call means. ~275 ms, $0.00007
4. Those answers become typed attributes, and Cedar judgment policies rule on them.
🚫 BLOCK · ✅ ALLOW · ⚠️ nothing matched → a human decides

Nobody wrote any of those three bands. That's the bit I like. Cedar only says yes when a permit matches and no forbid does — so "nothing matched" is already its own outcome. The gate reads it as "hand it to judgment" in phase 1, "hand it to a human" in phase 2. Default-deny stops being a refusal and becomes a routing signal, and there's no custom band logic to get wrong.

The trick is the confidence floor. Cedar attributes are just true or false, so "undecided" can't be a third value. Instead, if a critical answer comes back undecided, it sets confidence_floor=1, and the permit only fires when confidence_floor is 0. So a vague answer can never satisfy a permit. Not because I remembered to check — because it's impossible. I ask the same question 3 times in parallel; if the answers drift more than 0.2, it escalates.

Jev tells us what's going on. Cedar decides what's allowed. Nothing decides twice.

Fail-closed isn't a slogan here — it's a short list: model down, nonsense answer, budget gone. All three go to a human. One exception: a bug inside the engine. Nothing got paused, and the caller already knows the call failed, so it records a block — not a phantom approval in someone's queue.

All three defects I found were the same shape: a rule that was true when I wrote it, and quietly stopped being true where it ran. The gate's credential list was shorter than the scrubber's, so three credential stores got ALLOWED while the record redacted them. For each fix I put the bug back and made a test go red.

245 tests | 5/5 credential paths at phase 1 | phase 1 ~1.0 ms median at $0 | phase 2 ~275 ms median at ~$0.000074 per judgment | p95 314 ms | 23 real judgments, $0.0017 total

One thing I'll admit: what I send the model isn't scrubbed yet. The record and the trace are. A guardrail that leaks what it inspects has failed at its own premise — it's one line, and it's in the README's limitations.

Try the real gate (it authorizes and records, never executes): https://toolgate.srv1567269.hstgr.cloud/

Source: https://github.com/xueyouchao/agent-tool-gate

#AIAgents #Security #Cedar #PolicyAsCode
