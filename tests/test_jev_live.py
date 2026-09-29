"""The live judgment path — a real tool call judged by the real Jev over the network.

Every other test in this suite substitutes ``FakeJev``, and ``test_jev.py`` proves the adapter's
edges (fault → outage, body → the §5.2 contract, no key → outage) through an internal opener seam.
Neither touches a socket. This file is where the wire is real: engine → ``JevClient`` → HTTPS →
the battery's answers → Cedar → a decision.

The two network tests are **skipped unless** ``TYPESAFE_API_KEY`` (or ``JEV_API_KEY``) is set — in
the environment, or in a ``.env`` beside the code, which this file reads the same way the app does.
They need credentials, the network and money, and they are not deterministic — a model's answers can
differ between runs — so every assertion here is about the *shape* of what comes back and never
about a particular verdict:

    .venv/bin/python -m pytest tests/test_jev_live.py -v          # with the key in .env
    TYPESAFE_API_KEY=... .venv/bin/python -m pytest tests/test_jev_live.py -v

The first test is deliberately *not* skipped, because it must never reach the network: it is the
proof that wiring a real, unauthenticated judge cannot make the cheap path expensive.
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from fakes import make_engine  # noqa: E402

from toolgate.domain.battery import BATTERY  # noqa: E402
from toolgate.infrastructure.env_file import load_env_file  # noqa: E402
from toolgate.infrastructure.jev import JevClient  # noqa: E402

# The app reads `.env` when `toolgate.container` is first imported, which is later than this — and
# pytest evaluates the marker below at *collection* time. So without this line a perfectly good
# `.env` still skipped both network tests, which reads exactly like a key that does not work.
# Ask the same question the app asks, at the moment it would ask it.
load_env_file()

live = pytest.mark.skipif(
    not (os.environ.get("TYPESAFE_API_KEY") or os.environ.get("JEV_API_KEY")),
    reason="needs TYPESAFE_API_KEY / JEV_API_KEY, in the environment or in .env — "
           "this is the live judgment path")


def _state(command: str = "echo hello") -> dict:
    """The state the engine hands the judge (spec §5.1)."""
    return {"tool": "bash", "args": {"command": command}, "cwd": "/workspace",
            "user_intent": "", "project_facts": "", "recent_actions": [],
            "environment": "local"}


def test_a_phase_one_call_never_touches_the_model(tmp_path):
    """The cheap path stays cheap even with a real judge wired.

    The judge is a real ``JevClient``, passed by name, so if a phase-1 call leaked into the model it
    would call out for real — and this assertion would catch it. Runs on every invocation, key or no
    key, precisely because it must never call out. With a key present it proves the stronger thing:
    the cheap path stays cheap even when the judge is wired, reachable, and paid for.
    """
    out = make_engine(tmp_path / "d.jsonl", jev=JevClient()).authorize_tool_call(
        "bash", {"command": "git status"})

    assert out["phase"] == 1
    assert out["decision"] == "allow"
    assert out["answers"] is None, "a policy decided this; no judgment was bought"


@live
def test_the_real_jev_answers_the_whole_battery():
    """The adapter returns the §5.2 answers for every question the gateway asks.

    ``validate_jev_response`` already rejects an off-contract body, so reaching the assertion at
    all means the service answered properly; this pins *which* questions were answered.
    """
    answers = JevClient().invoke(_state()).answers

    assert set(BATTERY) <= set(answers), "every gateway question came back answered"


@live
def test_a_gray_call_is_really_judged_and_not_an_outage(tmp_path):
    """The point of the live path: a gray call comes back with a model's judgment.

    Without a key this same call escalates on `jev_outage` — the fail-closed stand-in. The
    assertion that matters is therefore the negative one: a real judgment must not be that.
    """
    out = make_engine(tmp_path / "d.jsonl", jev=JevClient()).authorize_tool_call(
        "bash", {"command": "echo hello"})

    assert out["phase"] == 2
    # `.get`, because a judgment that actually landed carries *no* reason at all: the record only
    # sets `decision_reason` when something went wrong. Reaching for the key directly asserted that
    # a healthy judgment had failed, and this test had never run far enough to find that out.
    assert out.get("decision_reason") != "jev_outage", "the judge was reached for real"
    assert out["decision"] in ("allow", "block", "escalate")
    assert out["answers"], "a judged call records the answers it was judged on"
