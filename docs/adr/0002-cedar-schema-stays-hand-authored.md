# The Cedar schema stays hand-authored

Status: accepted (2026-09-19)

The battery's question set is declared once, and the threshold table, the context-field mapping
and the adapter's defaults are all derived from it. `schema.cedar` is deliberately *not* generated
from that declaration. The policies are artifacts a human authors and reviews, and generating the
schema would turn a reviewed file into build output and add a codegen step to a repo that has none.
A test asserts the schema agrees with the declaration, so the two cannot drift.

## Considered options

**Generate `schema.cedar` from the question declaration.** Removes the possibility of drift
entirely, at the cost of a less reviewable policy file and a build step. Rejected — the test gives
the same guarantee for free.
