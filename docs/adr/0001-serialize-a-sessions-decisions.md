# Serialize a session's decisions, including the judgment call

Status: accepted (2026-09-19)

A session's authorization decisions run one at a time, inside a critical section spanning the
spend-ceiling check, the judgment call, and the decision record. The ceiling is checked before
spending and recorded after, so a lock covering only the record would let concurrent calls all
pass the check before any of them recorded — the ceiling was measurably breached that way.

## Considered options

**Reserve-and-release.** Atomically reserve budget up front, release on failure, leaving judgment
calls concurrent. Rejected: it leaks reservations when a call dies mid-flight, and a spend ceiling
should be obviously correct rather than fast. Concurrency in a single session is not a workload we
have evidence for.

## Consequences

- Concurrent calls in one session queue behind each other.
- A hung judgment backend holds the session for up to the client timeout (~10s), after which the
  call fails closed to escalation.
- The ceiling holds and the decision record is append-ordered, which the eval loop's deterministic
  replay depends on.
