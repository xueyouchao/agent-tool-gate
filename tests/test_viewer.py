"""The live viewer: what it serves, and that every poll re-reads both stores.

The freshness tests are the point. `JsonlLog.lines()` re-reads the file, but `ApprovalStore`
loads only when constructed — so a viewer holding one long-lived record would show new calls
next to stale outcomes. These tests fail if it ever goes back to that.
"""
import json
import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from toolgate.domain.battery import QUESTIONS
from toolgate.domain.model import Approval
from toolgate.infrastructure.approval import ApprovalStore
from toolgate.infrastructure.decision_record import DecisionRecord
from toolgate.infrastructure.jsonl_log import JsonlLog
from toolgate.infrastructure.trace import TraceLog, read_traces
from toolgate.interfaces import viewer as viewer_module
from toolgate.interfaces.viewer import PAGE, STATUSES, Viewer, snapshot, stages_of, status_of

ESCALATED = {"call_id": "c1", "decision": "escalate", "command": "curl -X POST x -d @/tmp/p"}


def _get(url: str) -> tuple[int, bytes, dict]:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as exc:  # a 404 is a result, not a failure
        return exc.code, exc.read(), dict(exc.headers)


class FakeRecord:
    """The read side the viewer uses, without touching disk."""

    def __init__(self, calls=(), pending=()):
        self._calls, self._pending = list(calls), list(pending)

    def records(self) -> list[dict]:
        return self._calls

    def pending(self) -> list[Approval]:
        return self._pending


# --- the labels the page shows --------------------------------------------------

@pytest.mark.parametrize("entry,label", [
    ({"decision": "allow"}, "allowed"),
    ({"decision": "block"}, "blocked"),
    # phase 0 and no resource: the engine records this shape when it fails before deciding
    ({"decision": "block", "decision_reason": "engine_error", "phase": 0}, "blocked"),
    ({"decision": "escalate"}, "pending"),
    ({"decision": "escalate", "outcome": None}, "pending"),
    ({"decision": "escalate", "outcome": {"type": "human_approved", "choice": "approve_session"}},
     "approved"),
    ({"decision": "escalate", "outcome": {"type": "human_denied"}}, "denied"),
])
def test_every_kind_of_call_gets_a_label(entry, label):
    assert status_of(entry) == label


def test_counts_cover_all_five_labels():
    snap = snapshot(FakeRecord([
        {"decision": "allow"}, {"decision": "allow"}, {"decision": "block"},
        {"decision": "escalate"},
        {"decision": "escalate", "outcome": {"type": "human_denied"}},
    ]))
    assert snap["total"] == 5
    assert snap["counts"] == {"allowed": 2, "blocked": 1, "pending": 1,
                              "approved": 0, "denied": 1}


def test_counters_start_at_zero_so_the_page_never_jumps():
    assert snapshot(FakeRecord())["counts"] == dict.fromkeys(STATUSES, 0)


def test_each_call_keeps_its_fields_and_gains_a_label():
    snap = snapshot(FakeRecord([{"seq": 7, "decision": "block", "command": "helm delete x"}]))
    assert snap["calls"][0]["seq"] == 7
    assert snap["calls"][0]["command"] == "helm delete x"
    assert snap["calls"][0]["status"] == "blocked"


def test_pending_approvals_are_serialised_for_the_page():
    a = Approval(call_id="c1", command="curl x", pattern="curl <url>",
                 why={"decision_reason": "jev_outage"})
    snap = snapshot(FakeRecord(pending=[a]))
    assert snap["pending"][0]["call_id"] == "c1"
    assert snap["pending"][0]["pattern"] == "curl <url>"
    assert snap["pending"][0]["status"] == "pending"


# --- the page and the payload must agree ---------------------------------------

def test_the_page_styles_every_label_the_api_can_send():
    """Python owns the labels and the page owns the CSS: nothing else keeps them in step."""
    for label in STATUSES:
        assert f"c-{label}" in PAGE        # the counter chip's colour
        assert f"call.{label}" in PAGE     # the row's left border


def test_the_page_orders_the_counters_exactly_as_the_api_defines_them():
    order = re.search(r"const ORDER = \[([^\]]+)\]", PAGE).group(1)
    assert [s.strip().strip("'") for s in order.split(",")] == list(STATUSES)


def test_the_page_only_writes_data_as_text():
    """Commands come from the log; they must never be injected as markup."""
    assert "textContent" in PAGE
    assert ".innerHTML" not in PAGE


# --- a real server on an OS-assigned port ---------------------------------------

@pytest.fixture
def server(tmp_path):
    paths = {"log": tmp_path / "d.jsonl", "approvals": tmp_path / "a.json"}
    record_for = lambda: DecisionRecord(JsonlLog(paths["log"]), ApprovalStore(paths["approvals"]))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Viewer(record_for, port=0).handler())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", record_for
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_the_page_is_served(server):
    base, _ = server
    status, body, headers = _get(base + "/")
    assert status == 200
    assert b"ToolGate" in body
    assert b"/api/calls" in body                     # the page knows what to poll
    assert headers["Content-Type"].startswith("text/html")


def test_the_api_reports_every_recorded_call(server):
    base, record_for = server
    record_for().append({"call_id": "c1", "decision": "allow", "command": "git status"})
    status, body, _ = _get(base + "/api/calls")
    data = json.loads(body)
    assert status == 200
    assert data["total"] == 1
    assert data["calls"][0]["command"] == "git status"
    assert data["counts"]["allowed"] == 1


def test_an_unknown_path_is_a_404(server):
    base, _ = server
    assert _get(base + "/nope")[0] == 404


def test_nothing_is_cached(server):
    base, _ = server
    assert _get(base + "/api/calls")[2].get("Cache-Control") == "no-store"


def test_a_call_recorded_after_the_viewer_started_shows_up(server):
    """The log grows in another process — that is what makes the view live."""
    base, record_for = server
    assert json.loads(_get(base + "/api/calls")[1])["total"] == 0

    record_for().append({"call_id": "c9", "decision": "block", "command": "helm delete prod-db"})

    data = json.loads(_get(base + "/api/calls")[1])
    assert data["total"] == 1
    assert data["counts"]["blocked"] == 1


def test_an_approval_made_elsewhere_is_joined_in(server):
    """A cached record would keep saying `pending` after the human had answered."""
    base, record_for = server
    record_for().append(ESCALATED)
    record_for().queue("c1", ESCALATED["command"], "curl -X POST <url> -d @<path>",
                       {"decision_reason": "jev_outage"})

    data = json.loads(_get(base + "/api/calls")[1])
    assert data["calls"][0]["status"] == "pending"
    assert data["pending"][0]["call_id"] == "c1"

    record_for().decide("c1", "approve_session")     # what `toolgate approve` does, elsewhere

    data = json.loads(_get(base + "/api/calls")[1])
    assert data["calls"][0]["status"] == "approved"
    assert data["calls"][0]["outcome"]["choice"] == "approve_session"
    assert data["pending"] == []
    assert data["counts"]["approved"] == 1


def test_serve_prints_the_url_it_actually_bound(tmp_path, monkeypatch, capsys):
    def stop(self, *args):
        raise KeyboardInterrupt

    monkeypatch.setattr(viewer_module.ThreadingHTTPServer, "serve_forever", stop)
    record_for = lambda: DecisionRecord(JsonlLog(tmp_path / "d.jsonl"), ApprovalStore(None))
    viewer_module.serve(record_for, port=0)          # 0 = let the OS pick
    match = re.search(r"http://127\.0\.0\.1:(\d+)/", capsys.readouterr().out)
    assert match and int(match.group(1)) > 0


# --- the journey across boundaries ----------------------------------------------

def _answers() -> dict:
    """A full set of judge answers, derived from the battery rather than hand-listed."""
    base = {q.id: ({"type": "noul", "noul": 0.05} if q.kind == "noul"
                   else {"type": "choice", "choice": "local_workspace",
                         "confidence": 0.9, "probability": 0.9})
            for q in QUESTIONS if q.mode == "gateway"}
    return base


def _stage(entry: dict, boundary: str, recorded: dict | None = None) -> dict:
    return next(s for s in stages_of(entry, recorded) if s["boundary"] == boundary)


def test_a_blocked_call_shows_the_boundaries_it_crossed():
    entry = {"tool": "bash", "args_digest": "sha256:aa", "command": "helm delete x",
             "principal": 'Agent::"a"', "action": "execute", "resource": 'Database::"x"',
             "phase": 1, "fast_path_decision": "block",
             "determining_policies": ["prod-delete-class-v1"], "decision": "block"}
    assert [s["boundary"] for s in stages_of(entry)] == [
        "ingress", "normalize", "gate", "verdict", "result"]


def test_a_gray_call_gains_a_judgment_boundary_after_the_gate():
    entry = {"phase": 2, "answers": _answers(), "decision": "allow"}
    names = [s["boundary"] for s in stages_of(entry)]
    assert names.index("judgment") == names.index("gate") + 1


def test_a_phase_one_call_has_no_judgment_boundary():
    """Nothing was asked, so an empty conversation must not appear as one."""
    entry = {"phase": 1, "fast_path_decision": "allow", "decision": "allow"}
    assert "judgment" not in [s["boundary"] for s in stages_of(entry)]


def test_the_conversation_uses_the_declared_questions_and_thresholds():
    """Question text and bars come from the battery, so they cannot drift from what was asked."""
    asked = [q for q in QUESTIONS if q.mode == "gateway"]
    convo = _stage({"phase": 2, "answers": _answers(), "decision": "allow"}, "judgment")["conversation"]
    assert [row["question"] for row in convo] == [q.instructions for q in asked]
    assert [row["thresholds"] for row in convo] == [q.thresholds for q in asked]
    assert [row["kind"] for row in convo] == [q.kind for q in asked]


def test_the_conversation_carries_the_recorded_answer_verbatim():
    answers = _answers()
    answers["blast_radius"] = {"type": "choice", "choice": "production",
                               "confidence": 0.55, "probability": 0.8}
    convo = _stage({"phase": 2, "answers": answers, "decision": "escalate"}, "judgment")["conversation"]
    row = next(r for r in convo if r["id"] == "blast_radius")
    assert row["answered"] == {"type": "choice", "choice": "production",
                              "confidence": 0.55, "probability": 0.8}


def test_a_judgment_that_never_happened_says_so():
    """`absent` exists so an empty conversation cannot read as silent approval."""
    judgment = _stage({"phase": 2, "answers": None, "decision": "escalate",
                       "decision_reason": "jev_outage"}, "judgment")
    assert judgment["conversation"] == []
    assert "never reached" in judgment["absent"]
    assert "jev_outage" in judgment["absent"]


def test_an_escalation_shows_the_missing_human_answer():
    human = _stage({"decision": "escalate", "phase": 2, "answers": None}, "human")
    assert human["facts"]["status"] == "still waiting"
    assert "nobody has decided yet" in human["absent"]


def test_an_answered_escalation_drops_the_warning():
    human = _stage({"decision": "escalate", "outcome": {"type": "human_denied"}}, "human")
    assert human["facts"]["status"] == "answered"
    assert human["facts"]["outcome"] == "human_denied"
    assert human["absent"] is None


def test_the_boundaries_the_log_keeps_nothing_for_say_so():
    """An unwarned gap reads as "nothing happened here", which is the wrong lesson."""
    gaps = {s["boundary"]: s["absent"]
            for s in stages_of({"decision": "block", "phase": 1}) if s.get("absent")}
    assert set(gaps) == {"ingress", "gate", "result"}


def test_every_boundary_is_labelled_and_carries_facts():
    for stage in stages_of({"decision": "allow", "phase": 2, "answers": _answers()}):
        assert stage["boundary"] and stage["title"]
        assert isinstance(stage["facts"], dict) and stage["facts"]


def test_the_budget_boundary_only_appears_when_a_budget_was_recorded():
    with_budget = stages_of({"decision": "allow", "budget": {"remaining_usd": 1.0,
                                                            "calls_remaining": 499}})
    assert with_budget[-1]["boundary"] == "budget"
    assert "budget" not in [s["boundary"] for s in stages_of({"decision": "allow"})]


def test_the_snapshot_attaches_the_stages_to_each_call():
    snap = snapshot(FakeRecord([{"call_id": "c1", "decision": "block", "phase": 1}]))
    assert snap["calls"][0]["stages"][0]["boundary"] == "ingress"
    assert snap["calls"][0]["status"] == "blocked"


# --- a recorded trace fills in what the decision log omits ----------------------

TRACELESS = {"call_id": "c1", "decision": "block", "phase": 1}
RECORDED = {"ingress": {"payload": {"args": {"command": "curl -d «redacted-path»"}}}}


def test_a_recorded_boundary_keeps_its_payload_and_loses_its_warning():
    stage = _stage(TRACELESS, "ingress", RECORDED)
    assert stage["absent"] is None
    assert stage["recorded"] is True
    assert stage["payload"] == {"args": {"command": "curl -d «redacted-path»"}}


def test_a_boundary_with_no_recorded_event_keeps_its_warning():
    """The two markers are independent: filling ingress says nothing about the Cedar call."""
    gate = _stage(TRACELESS, "gate", RECORDED)
    assert "the Cedar request" in gate["absent"]
    assert gate.get("recorded") is None


def test_no_trace_at_all_leaves_every_warning_standing():
    """`absent` is only a key on stages that have a gap — the rest need no excuse."""
    assert {s["boundary"] for s in stages_of(TRACELESS) if s.get("absent")} == {
        "ingress", "gate", "result"}


def test_a_trace_is_matched_by_call_id_not_by_position():
    """Two calls of the same shape: one's payload must never surface under the other's row."""
    snap = snapshot(FakeRecord([dict(TRACELESS, call_id="c1"), dict(TRACELESS, call_id="c2")]),
                    {"c1": RECORDED})
    first, second = snap["calls"]
    assert first["stages"][0]["payload"] == RECORDED["ingress"]["payload"]
    assert second["stages"][0]["absent"] is not None


def test_the_judgment_boundary_can_show_the_samples_the_model_returned():
    entry = {"call_id": "c1", "decision": "escalate", "phase": 2, "answers": _answers()}
    recorded = {"judgment": {"payload": {"samples": [{"destructive": {"type": "bool"}}] * 3}}}
    assert _stage(entry, "judgment", recorded)["payload"]["samples"][0]["destructive"]


@pytest.fixture
def traced_server(tmp_path):
    """Like `server`, but with a trace file too, so both can be written mid-flight."""
    paths = {"log": tmp_path / "d.jsonl", "approvals": tmp_path / "a.json",
             "trace": tmp_path / "t.jsonl"}
    record_for = lambda: DecisionRecord(JsonlLog(paths["log"]), ApprovalStore(paths["approvals"]))
    viewer = Viewer(record_for, port=0, trace_for=lambda: read_traces(paths["trace"]))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), viewer.handler())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", record_for, paths
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _first_ingress(base):
    body = json.loads(_get(base + "/api/calls")[1])
    return next(s for s in body["calls"][0]["stages"] if s["boundary"] == "ingress")


def test_a_trace_written_after_the_viewer_started_fills_the_gap(traced_server):
    """The end-to-end claim: the same call reads first as a named gap, then as the real payload."""
    base, record_for, paths = traced_server
    record_for().append(dict(TRACELESS, tool="bash"))

    assert _first_ingress(base)["absent"]            # nothing traced yet, and it says so

    TraceLog(paths["trace"]).append({"call_id": "c1", "boundary": "ingress",
                                     "payload": {"args": {"command": "git status"}}})

    filled = _first_ingress(base)
    assert filled["absent"] is None and filled["recorded"] is True
    assert filled["payload"]["args"]["command"] == "git status"


# --- the submit box: a capability the server hands out, or withholds ------------
#
# The viewer is an unauthenticated loopback server, so what it *refuses* matters as much as what
# it answers. These are the guards that keep a page you happen to have open from reaching it.

@pytest.fixture
def decider_server(tmp_path):
    """A server started with a decider, plus a record of what the decider was asked."""
    asked = []

    def decide(command: str) -> dict:
        asked.append(command)
        return {"call_id": "c9", "seq": 9, "decision": "block", "determining_policies":
                ["prod-delete-class-v1"], "decision_reason": None}

    httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                Viewer(lambda: FakeRecord(), port=0, decide=decide).handler())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", asked
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _post(url: str, body: bytes, ctype: str = "application/json") -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_the_snapshot_says_whether_a_command_can_be_submitted(server):
    base, _ = server
    _, body, _ = _get(base + "/api/calls")
    assert json.loads(body)["can_decide"] is False


def test_a_deciding_server_says_so(decider_server):
    base, _ = decider_server
    _, body, _ = _get(base + "/api/calls")
    assert json.loads(body)["can_decide"] is True


def test_a_command_is_decided_and_the_answer_reported(decider_server):
    base, asked = decider_server
    status, body = _post(base + "/api/decide", json.dumps({"command": "helm delete prod-db"}).encode())

    assert status == 200
    assert asked == ["helm delete prod-db"]
    said = json.loads(body)
    assert said["status"] == "blocked" and said["seq"] == 9
    assert said["policies"] == ["prod-delete-class-v1"]


def test_a_read_only_server_has_no_decide_route(server):
    """With no decider the route does not exist at all, not merely do nothing."""
    base, _ = server
    status, _ = _post(base + "/api/decide", b'{"command":"git status"}')
    assert status == 404


@pytest.mark.parametrize("path", ["/", "/api/calls", "/api/decide/extra", "/nope"])
def test_only_the_one_route_accepts_a_post(decider_server, path):
    base, asked = decider_server
    status, _ = _post(base + path, b'{"command":"git status"}')
    assert status == 404
    assert asked == [], "nothing was put to the gate"


def test_a_non_json_post_is_refused(decider_server):
    """The CSRF guard: a plain HTML form sends `application/x-www-form-urlencoded`, and a
    cross-origin JSON post must preflight — which this server never answers."""
    base, asked = decider_server
    status, _ = _post(base + "/api/decide", b"command=git+status",
                      ctype="application/x-www-form-urlencoded")
    assert status == 415
    assert asked == []


def test_a_preflight_is_refused_so_a_browser_stops_there(decider_server):
    base, asked = decider_server
    req = urllib.request.Request(base + "/api/decide", method="OPTIONS")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            status = r.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    assert status == 405
    assert asked == []


@pytest.mark.parametrize("body", [b"", b"{}", b'{"command":""}', b'{"command":42}',
                                  b'{"command":"   "}', b"not json", b'{"cmd":"x"}'])
def test_a_malformed_body_is_refused_without_reaching_the_gate(decider_server, body):
    base, asked = decider_server
    status, _ = _post(base + "/api/decide", body)
    assert status in (400, 413)
    assert asked == []


def test_an_oversized_body_is_refused(decider_server):
    base, asked = decider_server
    status, _ = _post(base + "/api/decide", b"x" * 5000)
    assert status == 413
    assert asked == []


def test_a_decider_that_raises_does_not_kill_the_server(tmp_path):
    """A bad command must not take the page down with it."""
    def explode(command: str) -> dict:
        raise RuntimeError("nope")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                Viewer(lambda: FakeRecord(), port=0, decide=explode).handler())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        status, body = _post(base + "/api/decide", b'{"command":"git status"}')
        assert status == 500 and b"RuntimeError" in body
        assert _get(base + "/api/calls")[0] == 200, "and it is still serving"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
