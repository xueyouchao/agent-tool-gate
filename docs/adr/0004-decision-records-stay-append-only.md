# Decision records stay append-only

Status: accepted (2026-09-19)

A decision is recorded the moment it is made, but a human's approval or denial arrives later — or
never. The log line in the spec shows `outcome` inline; that line is the *view* the eval loop
reads, not what is appended. Rather than rewriting the stored record when an outcome lands, one
module owns both stores and joins them on read.

## Considered options

**Rewrite the decision line when the outcome arrives**, so `outcome` appears inline exactly as the
spec's example shows. Rejected: it trades an append-only audit trail for a cosmetic match to the
spec, and reintroduces the concurrent-write hazard that decision serialization just removed.

## Consequences

- "What the gate decided" stays permanently distinct from "what a human later did" — the audit
  distinction escalation depends on.
- The eval loop and the log viewer read the same joined view, so the join exists in one place.
- The persisted line does not literally match the spec's log example; the example is a view.
