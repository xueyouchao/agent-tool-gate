"""Thresholding — Jev answers → phase-2 Cedar context (spec §6). Pure domain logic.

Tri-state on the noul probability; the confidence gate lives on `blast_radius` (the only
question that emits real confidence) and on self-consistency. A breached confidence floor is
encoded as `confidence_floor: 1`, which makes every phase-2 permit unsatisfiable → ESCALATE.
"""
from __future__ import annotations

from .battery import QUESTIONS

# The tuned artifact (spec §6) lives on each question in `battery.QUESTIONS`; these are its
# views. Critical attributes force ESCALATE when undecided, non-critical undecided just lands
# false. Gateway mode asks exactly `BATTERY`, so it thresholds exactly those questions — a mode
# asking a different set derives its own view from the same declaration.
THRESHOLDS: dict[str, dict] = {
    q.id: q.thresholds for q in QUESTIONS if q.thresholds and q.mode == "gateway"
}

BLAST_RADIUS_MIN_CONFIDENCE = 0.70
DRIFT_LIMIT = 0.2

# battery question id → schema context attribute (spec §4), for every question that has one —
# including the SDK-mode questions the gateway never asks.
QUESTION_TO_CONTEXT: dict[str, str] = {
    q.id: q.context for q in QUESTIONS if q.context
}


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
        else:  # dead-band → undecided; represented as false, flagged if critical
            val = False
            if cfg["critical"]:
                breached = True
        ctx[QUESTION_TO_CONTEXT[qid]] = val

    br = answers["blast_radius"]
    ctx["blast_radius"] = br["choice"]
    if br["confidence"] < BLAST_RADIUS_MIN_CONFIDENCE or br["choice"] == "unknown":
        breached = True

    ctx["confidence_floor"] = 1 if breached else 0
    return ctx


def detect_drift(samples: list[dict]) -> bool:
    """Self-consistency: any noul question drifting > DRIFT_LIMIT, or the ``blast_radius``
    choice flapping, across samples → breach."""
    if len(samples) < 2:
        return False
    for qid in THRESHOLDS:
        vals = [s[qid]["noul"] for s in samples]
        if max(vals) - min(vals) > DRIFT_LIMIT:
            return True
    return len({s["blast_radius"]["choice"] for s in samples}) > 1


def rank_score(answers: dict) -> float:
    """Display-only escalation ordering score (never authorizes). Weighted noul mean ×10."""
    nouls = [answers[q]["noul"] for q in THRESHOLDS]
    return round(10 * sum(nouls) / len(nouls), 1) if nouls else 0.0
