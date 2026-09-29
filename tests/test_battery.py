"""The battery declaration is the single authority — these pin every view derived from it.

Each view serves a different consumer: the Jev payload, the thresholder, the adapter's phase-1
defaults, and the Cedar schema. Nothing ties them together at runtime, so it is tied here — a
question added, renamed or re-moded must break one of these rather than silently diverge.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fakes import clean_answers

from toolgate.domain.battery import (BATTERY, BLAST_RADIUS_CHOICES, QUESTIONS, JevMalformed,
                                     validate_jev_response)
from toolgate.domain.thresholds import QUESTION_TO_CONTEXT, THRESHOLDS
from toolgate.infrastructure.adapter import DERIVED_CONTEXT_FIELDS, PHASE2_CONTEXT_DEFAULTS

SCHEMA = Path(__file__).parent.parent / "toolgate" / "domain" / "policies" / "schema.cedar"

_CONTEXT_ATTRS = {q.context for q in QUESTIONS if q.context}


def _schema_context_fields() -> set[str]:
    """The field names of the schema's `action execute` context block."""
    block = re.search(r"context:\s*\{(.*?)\}", SCHEMA.read_text(), re.S)
    assert block, "schema.cedar declares no context block"
    return set(re.findall(r"(\w+)\s*:", block.group(1)))


def test_schema_declares_exactly_the_declared_context():
    """Every question attribute, plus the fields the gate derives without asking."""
    assert _schema_context_fields() == _CONTEXT_ATTRS | set(DERIVED_CONTEXT_FIELDS)


def test_battery_is_the_reduced_gateway_battery():
    """Ticket 08: gateway mode asks 8 questions; the SDK-only two are declared but not asked."""
    assert len(BATTERY) == 8
    assert {"intent_match", "injected"}.isdisjoint(BATTERY)
    assert {q.id for q in QUESTIONS if q.mode == "sdk"} == {"intent_match", "injected"}


def test_every_gateway_noul_question_is_thresholded():
    """An unthresholded question would silently land `False` in every permit condition."""
    assert set(THRESHOLDS) == {q.id for q in QUESTIONS if q.mode == "gateway" and q.kind == "noul"}


def test_every_question_feeds_a_context_attribute():
    assert all(q.context for q in QUESTIONS), "a question with no context attribute is dead weight"
    assert set(QUESTION_TO_CONTEXT) == {q.id for q in QUESTIONS}


def test_adapter_defaults_declare_every_context_field():
    """Cedar requires every declared field present, including on the phase-1 path."""
    assert set(PHASE2_CONTEXT_DEFAULTS) == _CONTEXT_ATTRS | {"confidence_floor"}
    assert set(DERIVED_CONTEXT_FIELDS) == {"tool", "subcommand", "command", "reads_secret_path",
                                           "confidence_floor"}


def test_exactly_one_choice_question_supplies_the_blast_radius_options():
    choices = [q for q in QUESTIONS if q.kind == "choice"]
    assert len(choices) == 1, "more than one choice question would make `blast_radius` ambiguous"
    assert BLAST_RADIUS_CHOICES == choices[0].criteria
    assert "unknown" in BLAST_RADIUS_CHOICES      # the fail-closed option must exist


def test_question_ids_are_unique():
    ids = [q.id for q in QUESTIONS]
    assert len(ids) == len(set(ids))


def test_every_noul_question_declares_a_full_threshold():
    for q in QUESTIONS:
        if q.kind == "noul":
            assert q.thresholds is not None, f"`{q.id}` declares no thresholds"
            assert set(q.thresholds) == {"low", "high", "critical"}


def test_payload_shape_is_what_jev_receives():
    """Wire-identical: `type` + `instructions`, plus `criteria` for the choice question."""
    for qid, spec in BATTERY.items():
        assert set(spec) == {"type", "instructions"} | ({"criteria"} if qid == "blast_radius"
                                                       else set())


def test_validation_requires_every_gateway_answer():
    validate_jev_response(clean_answers())
    without_well_formed = {k: v for k, v in clean_answers().items() if k != "well_formed"}
    with pytest.raises(JevMalformed):
        validate_jev_response(without_well_formed)
