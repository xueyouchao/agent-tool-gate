"""The command-line layer: `_parse_args` and the argv it reads.

`_parse_args` turns typed words into the `argparse.Namespace` the container consumes. Two things
are worth pinning: that an explicitly handed-in list is honoured, and that handing in nothing
makes argparse fall back to the real `sys.argv` — the same words, read from a different place.
"""
import sys

import pytest

from toolgate import __main__ as tg_main
from toolgate.__main__ import _parse_args
from toolgate.interfaces.gateway import ToolGateProxy

# what the user typed, minus the program name
TYPED = ["--log", "demo/decisions.jsonl", "--consistency-samples", "1"]


def test_argv_handed_in_is_used():
    ns = _parse_args(TYPED)
    assert ns.log == "demo/decisions.jsonl"
    assert ns.consistency_samples == 1


def test_nothing_handed_in_reads_the_real_command_line(monkeypatch):
    """`None` means "go read sys.argv" — that is how `main()` reaches this function."""
    monkeypatch.setattr(sys, "argv", ["toolgate", *TYPED])
    assert _parse_args(None).log == "demo/decisions.jsonl"
    assert _parse_args().log == "demo/decisions.jsonl"   # omitting it entirely is identical


def test_the_two_places_give_the_same_answer(monkeypatch):
    """Explicit argv and sys.argv are the same words — two roads, same destination."""
    monkeypatch.setattr(sys, "argv", ["toolgate", *TYPED])
    assert _parse_args(TYPED) == _parse_args(None)


def test_defaults_apply_to_anything_not_typed():
    ns = _parse_args([])
    assert ns.log == "decisions.jsonl"                  # relative to the current folder
    assert ns.approvals == "pending_approvals.json"     # relative to the current folder
    assert ns.agent_name == "cli-agent"
    assert ns.consistency_samples == 3
    assert ns.upstream == []


def test_upstream_can_be_given_more_than_once():
    ns = _parse_args(["--upstream", '{"command": "a"}', "--upstream", '{"command": "b"}'])
    assert ns.upstream == ['{"command": "a"}', '{"command": "b"}']


def test_a_non_number_sample_count_is_rejected():
    with pytest.raises(SystemExit):                     # argparse exits; it does not return
        _parse_args(["--consistency-samples", "three"])


def test_an_unknown_option_is_an_error_not_a_crash():
    with pytest.raises(SystemExit):
        _parse_args(["--nope"])


async def test_main_async_honours_the_argv_it_is_handed(monkeypatch, tmp_path):
    """The parameter is functional: `_main([...])` really does parse what it is given.

    `sys.argv` is neutered first, so that if `_main` ever stops forwarding its argument the
    failure is a plain assertion about the defaults — not argparse choking on pytest's own
    command line, which is what it reads instead.
    """
    monkeypatch.setattr(sys, "argv", ["toolgate"])
    seen = {}

    async def fake_run_stdio(self):
        seen["log"] = self.engine.decisions.log.path
        seen["samples"] = self.engine.consistency_samples

    monkeypatch.setattr(ToolGateProxy, "run_stdio", fake_run_stdio)
    log = tmp_path / "d.jsonl"
    await tg_main._main(["--log", str(log), "--consistency-samples", "2"])

    assert seen["log"] == log                            # the typed --log reached the engine
    assert seen["samples"] == 2                          # and so did the sample count


def test_main_hands_nothing_to_main_async(monkeypatch):
    """Characterization: `main()` fills in no argv — the parameter always arrives empty.

    This test documents today's behaviour on purpose. If it starts failing because someone
    began passing argv through, that is a deliberate change: update this test to match.
    """
    seen = []

    async def fake_main(*args, **kwargs):
        seen.append((args, kwargs))

    monkeypatch.setattr(tg_main, "_main", fake_main)
    monkeypatch.setattr(sys, "argv", ["toolgate", *TYPED])
    tg_main.main()

    assert seen == [((), {})]                            # called with no arguments at all
