@id("secret-egress-v1")
forbid (principal, action, resource)
when { context.secret_exposure == true };

@id("clean-call-permit-v1")
permit (principal, action, resource)
when {
  context.is_destructive != true &&
  context.secret_exposure != true &&
  context.spends_money != true &&
  context.well_formed == true &&
  context.confidence_floor == 0
};
// NB: gateway mode runs the reduced battery (ticket 08) — `intent_match` and `injected`
// are not asked, so the `injection-signature-v1` forbid and the `intent_match == true`
// condition of the full battery are omitted here. `!= true` on a tri-state attribute means
// "false OR undecided", so any undecided critical attribute raises `confidence_floor` (via
// the thresholding layer) and makes this permit unsatisfiable → ESCALATE.
