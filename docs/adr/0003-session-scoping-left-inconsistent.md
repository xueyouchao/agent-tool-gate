# Session scoping is deliberately inconsistent

Status: accepted (2026-09-19)

Three things are described as per-session but are not keyed by session: the spend budget is one
instance per engine, the decision record's sequence counter is global to the log file, and the
gateway never emits a session other than the default. Only decision serialization is genuinely
per-session.

This is deliberate, not an oversight. With a single session in practice all three coincide, so no
test can distinguish them, and unifying them is a larger change with no current driver. Do not
"fix" this without a workload that actually runs concurrent sessions.
