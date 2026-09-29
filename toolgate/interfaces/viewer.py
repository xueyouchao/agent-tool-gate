"""Live web view of the calls the gate has decided.

A small stdlib-only HTTP server: ``/`` serves the page and ``/api/calls`` serves the current
snapshot as JSON, which the page re-fetches on an interval so calls appear as the gateway
records them. No third-party dependency and no WebSocket — one poll a second is plenty for a
log that grows by one line per tool call.

It reads through ``DecisionRecord``, the same joined view ``toolgate log`` prints, so the
"outcome" correlation is not re-implemented here. It does build a *fresh* record per request:
``JsonlLog.lines()`` re-reads the file every call, but ``ApprovalStore`` loads only when it is
constructed — so a long-lived record would show new calls next to stale outcomes.

``stages_of`` reconstructs each call's journey boundary by boundary from what the record holds.
What the record does *not* hold is named in that stage's ``absent`` field, so the page can say
"not recorded" instead of quietly implying the story is complete.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

from ..domain.battery import QUESTIONS
from . import topology

if TYPE_CHECKING:
    from ..infrastructure.decision_record import DecisionRecord

# every label the page knows about, so the counters read the same before and after the first call
STATUSES = ("allowed", "blocked", "pending", "approved", "denied")
_DECISION_STATUS = {"allow": "allowed", "block": "blocked"}
_OUTCOME_STATUS = {"human_approved": "approved", "human_denied": "denied"}

# the questions gateway mode actually asks, in declaration order
_ASKED = tuple(q for q in QUESTIONS if q.mode == "gateway")


def status_of(entry: dict) -> str:
    """One label per call: allowed, blocked, or an escalation's pending/approved/denied."""
    decision = entry.get("decision")
    if decision != "escalate":
        return _DECISION_STATUS.get(decision, str(decision))
    outcome = entry.get("outcome") or {}
    return _OUTCOME_STATUS.get(outcome.get("type"), "pending")


def _conversation(answers: dict | None) -> list[dict]:
    """The judge's side of the call: each question asked, its answer, and the bar it faced.

    Rebuilt from the recorded answers plus the battery declaration in `domain`, so the question
    text and thresholds come from the one place that defines them — never re-typed here.
    """
    if not answers:
        return []
    said = []
    for question in _ASKED:
        answered = answers.get(question.id)
        if answered is None:
            continue
        said.append({
            "id": question.id,
            "question": question.instructions,
            "kind": question.kind,
            "feeds": question.context,
            "thresholds": question.thresholds,
            "criteria": list(question.criteria),
            "answered": answered,
        })
    return said


def stages_of(entry: dict, recorded: dict | None = None) -> list[dict]:
    """The call's journey, boundary by boundary, built only from what the record holds.

    ``recorded`` is this call's boundary trace, when the gateway wrote one: every boundary it
    covers has its payload filled in and its warning withdrawn. A boundary still missing a
    payload says so in `absent`, rather than leaving the reader to assume the list is complete.
    """
    recorded = recorded or {}
    escalated = entry.get("decision") == "escalate"
    stages = [
        {
            "boundary": "ingress",
            "title": "MCP tools/call reaches the gateway",
            "facts": {"tool": entry.get("tool"), "arguments digest": entry.get("args_digest")},
            "payload": {"tool": entry.get("tool"), "args_digest": entry.get("args_digest"),
                        "command": entry.get("command")},
            "absent": "the raw arguments — the log keeps a digest and the scrubbed command",
        },
        {
            "boundary": "normalize",
            "title": "reduced to an authorization request",
            "facts": {"principal": entry.get("principal"), "action": entry.get("action"),
                      "resource": entry.get("resource")},
            "payload": {"principal": entry.get("principal"), "action": entry.get("action"),
                        "resource": entry.get("resource"), "session": entry.get("session")},
        },
        {
            "boundary": "gate",
            "title": "phase 1 — deterministic Cedar gate",
            "facts": {"verdict": entry.get("fast_path_decision") or "not decisive (gray)",
                      "policies": ", ".join(entry.get("determining_policies") or [])
                                  or "none fired"},
            "payload": {"fast_path_decision": entry.get("fast_path_decision"),
                        "determining_policies": entry.get("determining_policies")},
            "absent": "the Cedar request and response bodies",
        },
    ]

    if entry.get("phase") == 2:
        answers = entry.get("answers")
        stages.append({
            "boundary": "judgment",
            "title": "phase 2 — judgment model (Jev)",
            "facts": {"outcome": entry.get("decision_reason") or "answered",
                      "rank_score": entry.get("rank_score"),
                      "tokens": f"{entry.get('tokens_in')} in / {entry.get('tokens_out')} out",
                      "cost": f"${entry.get('cost_usd') or 0:.6f}"},
            "conversation": _conversation(answers),
            "payload": {"answers": answers, "rank_score": entry.get("rank_score"),
                        "tokens_in": entry.get("tokens_in"),
                        "tokens_out": entry.get("tokens_out"),
                        "cost_usd": entry.get("cost_usd"),
                        "latency_ms": entry.get("latency_ms")},
            "absent": None if answers else
                      "the prompt sent and any reply — the model was never reached "
                      f"({entry.get('decision_reason')})",
        })

    stages.append({
        "boundary": "verdict",
        "title": "the gate's decision",
        "facts": {"decision": entry.get("decision"), "mode": entry.get("mode"),
                  "policy_version": entry.get("policy_version")},
        "payload": {"decision": entry.get("decision"),
                    "decision_reason": entry.get("decision_reason"),
                    "mode": entry.get("mode"), "policy_version": entry.get("policy_version")},
    })

    if escalated:
        outcome = entry.get("outcome")
        stages.append({
            "boundary": "human",
            "title": "paused for a human",
            "facts": {"status": "answered" if outcome else "still waiting",
                      "outcome": (outcome or {}).get("type"),
                      "choice": (outcome or {}).get("choice")},
            "payload": {"outcome": outcome, "call_id": entry.get("call_id"),
                        "command": entry.get("command")},
            "absent": None if outcome else "the answer — nobody has decided yet",
        })
    else:
        stages.append({
            "boundary": "result",
            "title": "returned to the caller",
            "facts": {"shipped": "forwarded upstream unchanged"
                                 if entry.get("decision") == "allow"
                                 else "synthesized blocking result"},
            "absent": "the upstream's reply — the gateway does not log it",
        })

    if entry.get("budget"):
        stages.append({
            "boundary": "budget",
            "title": "spend after this call",
            "facts": {"remaining": f"${entry['budget'].get('remaining_usd')}",
                      "calls left": entry["budget"].get("calls_remaining")},
            "payload": entry["budget"],
        })

    for stage in stages:
        hit = recorded.get(stage["boundary"])
        if hit is not None:                       # the gateway kept this one: show the real thing
            stage["payload"] = hit.get("payload")
            stage["recorded"] = True
            stage["absent"] = None
    return stages


def topology_data() -> dict:
    """The diagram as the page needs it: boxes, routed arrows, and what a click on each one opens.

    Sent with every snapshot rather than fetched once, because it is small, constant, and the page
    then has exactly one thing to ask the server for.
    """
    return {
        "viewbox": list(topology.VIEWBOX),
        "node_w": topology.NODE_W,
        "node_h": topology.NODE_H,
        "nodes": [{**n, "boundary": topology.BOUNDARY_OF[n["id"]]} for n in topology.NODES],
        "edges": [{"id": e["id"], "src": e["src"], "dst": e["dst"], "label": e["label"],
                   "points": [list(p) for p in e["points"]],
                   "opens": topology.OPENS[e["id"]],
                   "boundary": topology.BOUNDARY_OF[e["dst"]]} for e in topology.EDGES],
    }


def snapshot(record: DecisionRecord, traces: dict | None = None,
             can_decide: bool = False) -> dict:
    """The whole current state as plain data: what was called, how it was decided, by whom."""
    traces = traces or {}
    calls = []
    counts = dict.fromkeys(STATUSES, 0)
    for entry in record.records():
        label = status_of(entry)
        counts[label] = counts.get(label, 0) + 1
        # labelled, staged and routed here, so the page only renders what Python already decided
        calls.append({**entry, "status": label, "path": topology.path_of(entry),
                      "stages": stages_of(entry, traces.get(entry.get("call_id")))})
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total": len(calls),
        "counts": counts,
        # the page shows the submit box only when the server will actually answer it
        "can_decide": can_decide,
        "pending": [asdict(a) for a in record.pending()],
        "topology": topology_data(),
        "calls": calls,
    }


class Viewer:
    """Serves the call log. `record_for` and `trace_for` are called per request, so reads are
    fresh — both files are written by the gateway process, not by this one."""

    def __init__(self, record_for: Callable[[], DecisionRecord],
                 host: str = "127.0.0.1", port: int = 8770,
                 trace_for: Callable[[], dict] | None = None,
                 decide: Callable[[str], dict] | None = None):
        self.record_for = record_for
        self.trace_for = trace_for
        self.decide = decide
        self.host = host
        self.port = port

    def snapshot_json(self) -> bytes:
        traces = self.trace_for() if self.trace_for else None
        return json.dumps(snapshot(self.record_for(), traces,
                                   can_decide=self.decide is not None)).encode()

    def handler(self) -> type[BaseHTTPRequestHandler]:
        viewer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"  # keep-alive: the page polls once a second

            def do_GET(self) -> None:
                try:
                    if self.path.startswith("/api/calls"):
                        self._send(200, "application/json", viewer.snapshot_json())
                    elif self.path in ("/", "/index.html"):
                        self._send(200, "text/html; charset=utf-8", PAGE.encode())
                    else:
                        self._send(404, "text/plain; charset=utf-8", b"not found")
                except Exception as exc:  # the page shows a dead indicator instead of hanging
                    self._send(500, "text/plain; charset=utf-8", str(exc).encode())

            def do_POST(self) -> None:
                """Ask the gate what it would do with a command. It decides; it never runs it.

                Disabled unless the server was started with a decider, and even then guarded
                against drive-by posts: this is an unauthenticated loopback server, so any page
                in any browser you have open could otherwise reach it. Requiring a JSON
                content-type makes the request non-simple, so the browser must preflight it —
                and `OPTIONS` is answered with 405, which stops it there. A plain HTML form
                cannot send this content-type at all.
                """
                if self.path.split("?")[0] != "/api/decide" or viewer.decide is None:
                    self._send(404, "text/plain; charset=utf-8", b"not found")
                    return
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                    self._send(415, "application/json",
                               b'{"error":"expected application/json"}')
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                if not 0 < length <= 4096:
                    self._send(413, "application/json", b'{"error":"bad length"}')
                    return
                try:
                    command = json.loads(self.rfile.read(length))["command"]
                    if not isinstance(command, str) or not command.strip():
                        raise ValueError("command must be a non-empty string")
                except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                    self._send(400, "application/json",
                               b'{"error":"expected {\\"command\\": \\"...\\"}"}')
                    return
                try:
                    entry = viewer.decide(command)
                except Exception as exc:  # a bad command must not kill the server
                    body = json.dumps({"error": f"{type(exc).__name__}: {exc}"[:300]}).encode()
                    self._send(500, "application/json", body)
                    return
                self._send(200, "application/json", json.dumps({
                    "call_id": entry.get("call_id"), "status": status_of(entry),
                    "decision": entry.get("decision"), "seq": entry.get("seq"),
                    "policies": entry.get("determining_policies") or [],
                    "reason": entry.get("decision_reason"),
                }).encode())

            def do_OPTIONS(self) -> None:
                """No CORS, deliberately: a preflight that is never answered is the CSRF guard."""
                self._send(405, "text/plain; charset=utf-8", b"no cross-origin requests")

            def _send(self, code: int, ctype: str, body: bytes) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                """Silence per-request logging; a poll a second would bury the console."""

        return Handler


def serve(record_for: Callable[[], DecisionRecord],
          host: str = "127.0.0.1", port: int = 8770,
          trace_for: Callable[[], dict] | None = None,
          decide: Callable[[str], dict] | None = None) -> None:
    """Serve the viewer until interrupted, printing the URL it is reachable at.

    Binding to loopback by default: the log holds the commands agents tried to run.

    With ``decide``, the page gains a box to submit a command. It is *authorized and recorded,
    never forwarded* — the engine decides; the gateway is what runs things, and it is not in this
    process. So the worst a submitted command can do is add a line to the log.
    """
    httpd = ThreadingHTTPServer((host, port),
                                Viewer(record_for, host, port, trace_for, decide).handler())
    # flush: stdout is block-buffered when piped, and the URL is the only thing worth seeing
    print(f"ToolGate viewer: http://{host}:{httpd.server_address[1]}/   (ctrl-c to stop)",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    finally:
        httpd.server_close()


PAGE = """
<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>ToolGate — live</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { --bg:#0d1117; --panel:#161b22; --sunk:#0b0f14; --line:#2b323f; --fg:#e6e9ef;
          --dim:#8b95a7; --allow:#3fb950; --block:#f85149; --wait:#d29922; --ok:#2f81f7;
          --wire:#39414d; --hot:#58a6ff; }
  * { box-sizing:border-box }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace }
  header { position:sticky; top:0; z-index:5; background:var(--panel);
           border-bottom:1px solid var(--line); padding:12px 20px;
           display:flex; align-items:center; gap:14px; flex-wrap:wrap }
  h1 { font-size:15px; margin:0; font-weight:600; letter-spacing:.02em }
  .dot { width:9px; height:9px; border-radius:50%; background:var(--dim); display:inline-block }
  .dot.live { background:var(--allow); box-shadow:0 0 8px var(--allow) }
  .dot.dead { background:var(--block) }
  .meta { color:var(--dim); font-size:12px }
  .chips { display:flex; gap:8px; margin-left:auto; flex-wrap:wrap }
  .chip { border:1px solid var(--line); border-radius:999px; padding:2px 10px; font-size:12px }
  .chip b { font-weight:600 }
  .c-allowed { color:var(--allow) } .c-blocked { color:var(--block) }
  .c-pending { color:var(--wait) }  .c-approved { color:var(--ok) } .c-denied { color:var(--dim) }

  main { padding:14px 20px 60px; max-width:1420px; margin:0 auto }
  h2 { font-size:12px; text-transform:uppercase; letter-spacing:.08em; color:var(--dim);
       margin:22px 0 8px }
  .hint { color:var(--dim); font-size:12px; margin:-4px 0 10px }

  /* ---- the diagram ---- */
  .board { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:4px }
  svg#topo { width:100%; height:auto; display:block }
  /* below this the labels stop being legible, so scroll rather than shrink further */
  @media (max-width: 820px) { .board { overflow-x:auto } svg#topo { width:820px } }
  .wire { fill:none; stroke:var(--wire); stroke-width:1.5 }
  .wire.hot { stroke:var(--hot); stroke-width:2.8 }
  .wire.human { stroke-dasharray:5 3 }
  .wlabel { fill:var(--dim); font:10px ui-monospace,monospace; pointer-events:none }
  .node rect { fill:var(--panel); stroke:var(--line); stroke-width:1.4; cursor:pointer }
  .node.frontend rect { stroke:#2f81f7 }
  .node.backend rect { stroke:#a371f7 }
  .node.security rect { stroke:#d29922 }
  .node.database rect { stroke:#3fb950 }
  .node.external rect { stroke:#6e7681; stroke-dasharray:5 3 }
  .node .nlabel { fill:var(--fg); font:600 13px ui-monospace,monospace; pointer-events:none }
  .node .nsub { fill:var(--dim); font:10px ui-monospace,monospace; pointer-events:none }
  .node.sel rect { stroke-width:2.8 }
  .node.active rect { stroke-width:2.8; filter:url(#glow) }
  .packet.allowed { fill:var(--allow) } .packet.blocked { fill:var(--block) }
  .packet.pending { fill:var(--wait) }  .packet.approved { fill:var(--ok) }
  .packet.denied { fill:var(--dim) }
  .packet { filter:url(#glow) }

  .legend { display:flex; gap:16px; flex-wrap:wrap; align-items:center; color:var(--dim);
            font-size:11.5px; padding:8px 8px 4px }
  .key { display:flex; align-items:center; gap:6px }
  .swatch { width:16px; height:10px; border-radius:2px; border:1.4px solid }

  /* ---- the controls ---- */
  .controls { display:flex; gap:8px; align-items:center; flex-wrap:wrap;
              border-top:1px solid var(--line); margin-top:6px; padding:10px 8px 4px }
  .controls button { background:var(--sunk); color:var(--fg); border:1px solid var(--line);
                     border-radius:6px; padding:5px 12px; font:12px ui-monospace,monospace;
                     cursor:pointer }
  .controls button:hover { border-color:var(--hot); color:#fff }
  .controls button.primary { border-color:var(--hot); color:var(--hot) }
  .controls label { color:var(--dim); font-size:12px; display:flex; gap:6px; align-items:center }
  .controls select { background:var(--sunk); color:var(--fg); border:1px solid var(--line);
                     border-radius:6px; padding:4px 6px; font:12px ui-monospace,monospace }
  .controls select#pick { max-width:340px }
  /* only rendered when the server was started with --allow-decide.
     `display:flex` above is an author rule, so it beats the browser's own `[hidden]{display:none}`
     — without this line the box sits on screen on a read-only viewer while `el.hidden` reads true. */
  .controls[hidden] { display:none }
  /* sits ABOVE the playback row on purpose: the diagram is tall, and at laptop height a row below
     the playback controls falls past the fold — the box was rendered but you had to scroll for it */
  .controls.decide { border-bottom:1px dashed var(--line); padding-bottom:10px }
  .controls input#cmd { background:var(--sunk); color:var(--fg); border:1px solid var(--line);
                        border-radius:6px; padding:5px 9px; font:inherit; min-width:340px }
  .controls input#cmd:focus { outline:none; border-color:var(--hot) }
  .controls .decided { font-size:12px }
  .controls .decided.ok { color:var(--allow) } .controls .decided.no { color:var(--block) }
  .controls .decided.wait { color:var(--wait) } .controls .decided.err { color:var(--dim) }
  .where { margin-left:auto; color:var(--dim); font-size:12px }
  .where b { color:var(--fg); font-weight:600 }
  .where .bd { color:var(--ok) }

  /* ---- the detail panel ---- */
  .detail { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px;
            margin-top:14px }
  .detail h3 { margin:0 0 4px; font-size:13px; word-break:break-all }
  .detail .sub { color:var(--dim); font-size:12px; margin-bottom:8px }
  .facts { padding:2px 0 4px }
  .fact { display:flex; gap:10px; font-size:12.5px }
  .fkey { color:var(--dim); min-width:140px; flex:none }
  .fval { word-break:break-all }
  .absent { margin:6px 0; color:var(--wait); font-size:12px; font-style:italic }
  .absent::before { content:"⚠  " }
  pre { background:var(--sunk); border:1px solid var(--line); border-radius:4px;
        padding:8px; margin:6px 0; overflow:auto; font-size:12px; color:#c9d1d9; max-height:300px }
  .bnd { display:inline-block; color:var(--ok); font-size:12px; text-transform:uppercase;
         letter-spacing:.06em }
  .convo { margin:6px 0 }
  .qa { border-left:2px solid var(--line); padding:2px 0 6px 10px; margin-bottom:6px }
  .q { color:var(--fg) }
  .a { color:var(--dim); font-size:12.5px }
  .val { color:var(--ok); font-weight:600 }
  .thr { color:var(--dim); font-size:11.5px }
  .steps { display:flex; flex-wrap:wrap; gap:6px; margin:8px 0 4px }
  .step { border:1px solid var(--line); border-radius:999px; padding:1px 9px; font-size:11px;
          color:var(--dim); cursor:pointer }
  .step:hover { border-color:var(--hot) }
  .step.on { border-color:var(--hot); color:var(--fg) }
  .stageblock { border-top:1px solid var(--line); padding-top:8px; margin-top:8px }

  /* ---- the list ---- */
  details.call { background:var(--panel); border:1px solid var(--line); border-left-width:3px;
                 border-radius:6px; margin-bottom:8px }
  details.call.allowed  { border-left-color:var(--allow) }
  details.call.blocked  { border-left-color:var(--block) }
  details.call.pending  { border-left-color:var(--wait) }
  details.call.approved { border-left-color:var(--ok) }
  details.call.denied   { border-left-color:var(--dim) }
  details.call.pend { background:rgba(210,153,34,.08) }
  details.call.sel { outline:1px solid var(--hot) }
  summary { cursor:pointer; padding:10px 12px; list-style:none }
  summary::-webkit-details-marker { display:none }
  summary:hover { background:rgba(255,255,255,.02) }
  summary::before { content:"▸"; color:var(--dim); margin-right:8px }
  details[open] > summary::before { content:"▾" }
  .row { display:flex; gap:10px; align-items:baseline; flex-wrap:wrap }
  .seq { color:var(--dim); font-size:12px; min-width:34px }
  .badge { font-size:11px; text-transform:uppercase; letter-spacing:.06em; font-weight:600 }
  .cmd { flex:1; min-width:260px; word-break:break-all }
  .when { color:var(--dim); font-size:12px }
  .stages { border-top:1px solid var(--line); padding:6px 12px 12px 12px }
  details.stage { border-left:2px solid var(--line); margin:6px 0; padding-left:10px }
  details.stage.recorded { border-left-color:var(--ok) }
  details.stage > summary { padding:4px 0 }
  .stitle { color:var(--fg) }
  .empty { color:var(--dim); padding:16px 0 }
</style></head><body>
<header>
  <h1>ToolGate</h1>
  <span class="dot" id="dot"></span>
  <span class="meta" id="stamp">connecting…</span>
  <span class="chips" id="chips"></span>
</header>
<main>
  <h2>The gate, and the path a call takes</h2>
  <div class="hint">choose a call to replay, then step it across one boundary at a time — each step
    opens the message that crossing produced. clicking a component jumps straight to its step.</div>
  <div class="board">
    <svg id="topo" xmlns="http://www.w3.org/2000/svg" role="img"
         aria-label="ToolGate components and the path a tool call takes through them">
      <defs>
        <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6"
                orient="auto-start-reverse">
          <path d="M0 0 L10 5 L0 10 z" fill="#57606a"></path>
        </marker>
        <filter id="glow" x="-70%" y="-70%" width="240%" height="240%">
          <feGaussianBlur stdDeviation="3" result="b"></feGaussianBlur>
          <feMerge>
            <feMergeNode in="b"></feMergeNode>
            <feMergeNode in="SourceGraphic"></feMergeNode>
          </feMerge>
        </filter>
      </defs>
      <g id="wires"></g>
      <g id="labels"></g>
      <g id="packets"></g>
      <g id="nodes"></g>
    </svg>
    <div class="legend">
      <span class="key"><span class="swatch" style="border-color:#2f81f7"></span>gateway</span>
      <span class="key"><span class="swatch" style="border-color:#a371f7"></span>application</span>
      <span class="key"><span class="swatch" style="border-color:#d29922"></span>policy</span>
      <span class="key"><span class="swatch" style="border-color:#3fb950"></span>store</span>
      <span class="key"><span class="swatch"
            style="border-color:#6e7681;border-style:dashed"></span>outside the gate</span>
    </div>
    <div class="controls decide" id="decidebar" hidden>
      <label>try a command <input id="cmd" type="text" spellcheck="false"
             placeholder="helm delete prod-db -n prod"></label>
      <button id="run" class="primary">ask the gate ▶</button>
      <span class="decided" id="verdict"></span>
    </div>
    <div class="controls">
      <label>replay <select id="pick"></select></label>
      <button id="back">◀ back</button>
      <button id="step" class="primary">step ▶</button>
      <button id="play">▶ play</button>
      <button id="restart">↻ restart</button>
      <label>speed
        <select id="speed">
          <option value="1600">slow</option>
          <option value="900" selected>normal</option>
          <option value="380">fast</option>
        </select>
      </label>
      <span class="where" id="where"></span>
    </div>
  </div>

  <div class="detail" id="detail"></div>

  <h2>Waiting for a human</h2>
  <div id="pending"></div>
  <h2>All calls</h2>
  <div class="hint">click a call to follow it from the start</div>
  <div id="calls"></div>
</main>
<script>
const SVGNS = 'http://www.w3.org/2000/svg';
const dot = document.getElementById('dot'), stamp = document.getElementById('stamp');
const chips = document.getElementById('chips'), pending = document.getElementById('pending');
const calls = document.getElementById('calls'), detail = document.getElementById('detail');
const canvas = document.getElementById('topo');
const wires = document.getElementById('wires'), labels = document.getElementById('labels');
const packets = document.getElementById('packets'), nodeLayer = document.getElementById('nodes');
const where = document.getElementById('where'), speedSel = document.getElementById('speed');
const pickSel = document.getElementById('pick');
const ORDER = ['allowed','blocked','pending','approved','denied'];
const openCalls = new Set(), openStages = new Set();   // survive a live re-render
const PAUSE_AT_EACH = 250;                             // a beat at every component, to read it

let topo = null, state = { calls: [] };
let byEdge = {}, byNode = {}, byCall = {};
const wireEls = {}, nodeEls = {}, callEls = {};
let selection = null;                 // {call_id, boundary} — what the detail panel is showing
let msPerEdge = Number(speedSel.value);
let firstRender = true;

// The one call being followed, and where along its route the packet is parked.
const player = {
  call: null, path: [], pos: 0, pkt: null,
  playing: false, manual: false, seen: new Set(),
  gen: 0,        // bumped on every park, so a glide still in flight is abandoned
};

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);  // never innerHTML
  return n;
}
function s(tag, attrs) {
  const n = document.createElementNS(SVGNS, tag);
  for (const k in (attrs || {})) n.setAttribute(k, attrs[k]);
  return n;
}
function show(v) { return (v === null || v === undefined) ? '—' : String(v); }

function chipsFor(counts) {
  chips.replaceChildren();
  for (const k of ORDER) {
    const c = el('span', 'chip c-' + k);
    c.append(el('b', null, counts[k] ?? 0), ' ' + k);
    chips.append(c);
  }
}

/* ---- the diagram, drawn once from the topology the server sent ---- */

function midpoint(points) {           // the middle of the longest straight run, for the label
  let best = points[0], bestLen = -1;
  for (let i = 0; i < points.length - 1; i++) {
    const a = points[i], b = points[i + 1];
    const len = Math.hypot(b[0] - a[0], b[1] - a[1]);
    if (len > bestLen) { bestLen = len; best = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]; }
  }
  return best;
}

function buildTopology(t) {
  topo = t;
  // The viewBox is what makes the diagram scale to the page instead of being clipped to the
  // default 150px-tall inline SVG viewport. Without it you see the top slice and nothing else.
  canvas.setAttribute('viewBox', '0 0 ' + t.viewbox[0] + ' ' + t.viewbox[1]);
  canvas.setAttribute('preserveAspectRatio', 'xMidYMid meet');

  byEdge = {}; byNode = {};
  for (const e of t.edges) byEdge[e.id] = e;
  for (const n of t.nodes) byNode[n.id] = n;

  wires.replaceChildren(); labels.replaceChildren(); nodeLayer.replaceChildren();
  for (const k in wireEls) delete wireEls[k];
  for (const k in nodeEls) delete nodeEls[k];

  for (const e of t.edges) {
    const d = 'M' + e.points.map(p => p[0] + ' ' + p[1]).join(' L');
    const w = s('path', { d: d, class: 'wire' + (e.id === 'judgment-human' ? ' human' : ''),
                          'marker-end': 'url(#arrow)', 'data-id': e.id });
    w.addEventListener('click', () => pick(e.dst, e.opens, e.id));
    wires.append(w);
    wireEls[e.id] = w;

    const at = midpoint(e.points);
    const lb = s('text', { x: at[0], y: at[1] - 6, class: 'wlabel', 'text-anchor': 'middle' });
    lb.textContent = e.label;
    labels.append(lb);
  }

  for (const n of t.nodes) {
    const g = s('g', { class: 'node ' + n.kind,
                       transform: 'translate(' + n.x + ',' + n.y + ')', 'data-id': n.id });
    g.append(s('rect', { width: t.node_w, height: t.node_h, rx: 8 }));
    const l1 = s('text', { x: 12, y: 27, class: 'nlabel' });
    l1.textContent = n.label;
    const l2 = s('text', { x: 12, y: 45, class: 'nsub' });
    l2.textContent = n.sub;
    g.append(l1, l2);
    g.addEventListener('click', () => pick(n.id, n.boundary, null));
    nodeLayer.append(g);
    nodeEls[n.id] = g;
  }
}

/* ---- following one call, and stepping it ---- */

function nodeAt(pos) {
  if (!player.path.length) return null;
  return pos === 0 ? byEdge[player.path[0]].src : byEdge[player.path[pos - 1]].dst;
}

function place() {                    // park the packet where player.pos says it is
  if (!player.pkt || !player.path.length) return;
  const pts = player.pos === 0 ? byEdge[player.path[0]].points
                               : byEdge[player.path[player.pos - 1]].points;
  const at = player.pos === 0 ? pts[0] : pts[pts.length - 1];
  player.pkt.setAttribute('cx', at[0]);
  player.pkt.setAttribute('cy', at[1]);
}

// the step at which a boundary's message becomes readable, or -1 if this call never crosses it
function stepFor(boundary) {
  if (!boundary) return 0;
  for (let i = 0; i < player.path.length; i++)
    if (byEdge[player.path[i]].opens === boundary) return i + 1;
  return -1;
}

function follow(call, restart) {
  if (!call) return;
  if (player.call && player.call.call_id === call.call_id && !restart) return;
  player.playing = false;
  player.call = call;
  player.path = (call.path || []).filter(id => byEdge[id]);
  if (player.pkt) { player.pkt.remove(); player.pkt = null; }
  if (player.path.length) {
    player.pkt = s('circle', { r: 6, class: 'packet ' + call.status });
    packets.append(player.pkt);
  }
  parkAt(0);
}

// Move the packet to a step and open the message that arriving there produced.
function parkAt(pos) {
  player.gen++;      // abandon any glide still in flight: it belongs to wherever we were before
  player.pos = Math.max(0, Math.min(player.path.length, pos));
  place();
  const edgeId = player.pos > 0 ? player.path[player.pos - 1] : null;
  selection = player.call
    ? { call_id: player.call.call_id, boundary: edgeId ? byEdge[edgeId].opens : null }
    : null;
  paintSelection();
  renderDetail();
  paintPlayer();
}

function hop(then) {                  // glide one edge, then stop at the far end
  if (!player.call || !player.pkt || player.pos >= player.path.length) return false;
  const edgeId = player.path[player.pos], e = byEdge[edgeId];
  const wire = wireEls[edgeId], g = nodeEls[e.dst];
  if (wire) wire.classList.add('hot');
  if (g) g.classList.add('active');
  roll(player.pkt, e.points, () => {
    if (wire) wire.classList.remove('hot');
    if (g) g.classList.remove('active');
    parkAt(player.pos + 1);
    if (then) then();
  });
  return true;
}

function run() {
  if (!player.playing || !player.call) return;
  if (player.pos >= player.path.length) { player.playing = false; paintPlayer(); return; }
  hop(() => { if (player.playing) setTimeout(run, PAUSE_AT_EACH); });
}

function roll(circ, points, done) {
  const gen = player.gen;    // if this changes, someone parked elsewhere and this glide is stale
  const segs = []; let total = 0;
  for (let i = 0; i < points.length - 1; i++) {
    const a = points[i], b = points[i + 1];
    const dx = b[0] - a[0], dy = b[1] - a[1], len = Math.hypot(dx, dy) || 0.0001;
    segs.push({ ax: a[0], ay: a[1], dx: dx, dy: dy, len: len, start: total });
    total += len;
  }
  const t0 = performance.now();
  function frame(now) {
    if (gen !== player.gen) return;   // a newer park took over; do not land on the new call
    const p = Math.min(1, (now - t0) / msPerEdge);
    const want = p * total;
    let seg = segs[segs.length - 1];
    for (const cand of segs) if (want <= cand.start + cand.len) { seg = cand; break; }
    const k = Math.min(1, (want - seg.start) / seg.len);
    circ.setAttribute('cx', seg.ax + seg.dx * k);
    circ.setAttribute('cy', seg.ay + seg.dy * k);
    if (p < 1) requestAnimationFrame(frame); else done();
  }
  requestAnimationFrame(frame);
}

/* ---- clicking: pick the call that went through this component, newest first ---- */

function visited(call, nodeId) {
  return (call.path || []).some(id => byEdge[id] && byEdge[id].dst === nodeId);
}

function newestVisit(nodeId) {
  let found = null;
  for (const c of state.calls || []) if (visited(c, nodeId)) found = c;   // log order → newest
  return found;
}

/* The two sinks (TraceLog, DecisionRecord) are written by the engine rather than travelled
   through, so they sit on the diagram as side branches and appear in no call's path. A click on
   one still has to open something: fall back to the newest call that produced that boundary. */
function newestWithStage(boundary) {
  let found = null;
  for (const c of state.calls || [])
    if ((c.stages || []).some(st => st.boundary === boundary)) found = c;
  return found;
}

function pick(nodeId, boundary, edgeId) {
  let call = selection && byCall[selection.call_id];
  if (!call || !visited(call, nodeId)) {
    call = newestVisit(nodeId) || newestWithStage(boundary);
  }
  if (!call) return;
  // clicking a component means "show me this message", so bring the packet to that step too
  if (player.call && player.call.call_id === call.call_id) {
    const at = edgeId ? player.path.indexOf(edgeId) + 1 : stepFor(boundary);
    if (at > 0) { player.playing = false; parkAt(at); return; }
  }
  selection = { call_id: call.call_id, boundary: boundary };
  paintSelection();
  renderDetail();
  paintPlayer();
}

function paintSelection() {
  const call = selection && byCall[selection.call_id];
  for (const id in nodeEls)
    nodeEls[id].classList.toggle('sel', !!(call && visited(call, id)));
  for (const cid in callEls)
    callEls[cid].classList.toggle('sel', !!(call && call.call_id === cid));
}

// One option per call, carrying the status so it reads at a glance. Rebuilt only when the set of
// calls or their statuses actually changes, so a re-render never closes the list under the reader.
function pickOptions(list) {
  const wanted = list.map(c => c.call_id + ':' + c.status).join('|');
  if (pickSel.dataset.ids === wanted) return;
  const keep = pickSel.value;
  pickSel.replaceChildren();
  for (const c of list) {
    const o = document.createElement('option');
    o.value = c.call_id;
    o.textContent = '#' + c.seq + ' · ' + c.status + ' · ' + (c.command || '').slice(0, 46);
    pickSel.append(o);
  }
  if (!list.length) {
    const o = document.createElement('option');
    o.value = '';
    o.textContent = 'no calls yet';
    pickSel.append(o);
  }
  pickSel.dataset.ids = wanted;
  if (keep) pickSel.value = keep;
}

function paintPlayer() {
  const total = player.path.length;
  where.replaceChildren();
  if (!player.call) { where.append('nothing followed yet'); return; }
  // the dropdown already names the call, so the readout only has to say where it has got to
  where.append(el('b', null, player.pos === 0 ? 'at the start'
    : 'step ' + player.pos + ' of ' + total));
  const at = nodeAt(player.pos);
  const edgeId = player.pos > 0 ? player.path[player.pos - 1] : null;
  if (at) where.append('  ·  at ', el('b', null, byNode[at].label));
  if (edgeId) where.append('  ·  opens ', el('span', 'bd', byEdge[edgeId].opens));
  document.getElementById('play').textContent = player.playing ? '⏸ pause' : '▶ play';
  if (byCall[player.call.call_id]) pickSel.value = player.call.call_id;
}

/* ---- the detail panel ---- */

function convo(rows) {
  const box = el('div', 'convo');
  for (const q of rows) {
    const ans = q.answered || {};
    const r = el('div', 'qa');
    r.append(el('div', 'q', q.question));
    const a = el('div', 'a');
    if (q.kind === 'noul') a.append(el('span', 'val', show(ans.noul)), ' probability');
    else a.append(el('span', 'val', show(ans.choice)), ' · confidence ' + show(ans.confidence));
    if (q.feeds) a.append('   ·   feeds Cedar ', el('span', 'val', q.feeds));
    r.append(a);
    if (q.id === 'blast_radius')
      r.append(el('div', 'thr', 'options: ' + (q.criteria || []).join(' / ')));
    if (q.thresholds)
      r.append(el('div', 'thr', 'thresholds ' + JSON.stringify(q.thresholds)));
    box.append(r);
  }
  return box;
}

function stageBlock(st) {
  const box = el('div', 'stageblock');
  const head = el('div');
  head.append(el('span', 'bnd', st.boundary + '   '), el('span', 'stitle', st.title));
  box.append(head);
  const facts = el('div', 'facts');
  for (const [k, v] of Object.entries(st.facts || {})) {
    const line = el('div', 'fact');
    line.append(el('span', 'fkey', k), el('span', 'fval', show(v)));
    facts.append(line);
  }
  box.append(facts);
  if (st.absent) box.append(el('div', 'absent', 'not recorded: ' + st.absent));
  if (st.conversation && st.conversation.length) box.append(convo(st.conversation));
  if (st.payload !== undefined)
    box.append(el('pre', null, JSON.stringify(st.payload, null, 2)));
  return box;
}

function renderDetail() {
  detail.replaceChildren();
  const call = selection && byCall[selection.call_id];
  if (!call) {
    detail.append(el('div', 'empty',
      'click a component or an arrow above to open the message that crossed it'));
    return;
  }
  detail.append(el('h3', null, '#' + call.seq + '   ' + (call.command || '(no command)')));
  const sub = el('div', 'sub');
  sub.append(el('span', 'badge c-' + call.status, call.status), '   ·   ' +
    ((call.determining_policies || []).join(', ') || 'no policy fired'));
  detail.append(sub);

  const steps = el('div', 'steps');
  const whole = el('span', 'step' + (selection.boundary ? '' : ' on'), 'whole journey');
  whole.addEventListener('click', () => parkAt(0));
  steps.append(whole);
  for (const st of call.stages || []) {
    const chip = el('span', 'step' + (st.boundary === selection.boundary ? ' on' : ''), st.boundary);
    chip.addEventListener('click', () => {
      const at = stepFor(st.boundary);
      if (at > 0 && player.call && player.call.call_id === call.call_id) {
        player.playing = false;
        parkAt(at);
      } else {
        selection = { call_id: call.call_id, boundary: st.boundary };
        paintSelection(); renderDetail(); paintPlayer();
      }
    });
    steps.append(chip);
  }
  detail.append(steps);

  const shown = selection.boundary
    ? (call.stages || []).filter(st => st.boundary === selection.boundary)
    : (call.stages || []);
  for (const st of shown) detail.append(stageBlock(st));
}

/* ---- the calls list ---- */

function callCard(d) {
  const st = d.status;
  const card = document.createElement('details');
  card.className = 'call ' + st + (st === 'pending' ? ' pend' : '');
  card.open = openCalls.has(d.call_id);
  card.addEventListener('toggle', () =>
    card.open ? openCalls.add(d.call_id) : openCalls.delete(d.call_id));
  const sum = document.createElement('summary');
  const row = el('div', 'row');
  row.append(el('span', 'seq', '#' + d.seq));
  row.append(el('span', 'badge c-' + st, st));
  row.append(el('span', 'cmd', d.command || '(no command)'));
  if (d.outcome) row.append(el('span', 'when', d.outcome.type +
    (d.outcome.choice ? ' · ' + d.outcome.choice : '')));
  row.append(el('span', 'when', (d.ts || '').slice(11, 19)));
  const p = d.determining_policies || [];
  row.append(el('span', 'when', p.length ? p.join(', ') : 'no policy fired'));
  sum.append(row);
  card.append(sum);

  const box = el('div', 'stages');
  for (const stage of d.stages || []) {
    const sd = document.createElement('details');
    const key = d.call_id + '/' + stage.boundary;
    sd.className = 'stage' + (stage.recorded ? ' recorded' : '');
    sd.open = openStages.has(key);
    sd.addEventListener('toggle', () =>
      sd.open ? openStages.add(key) : openStages.delete(key));
    const ss = document.createElement('summary');
    ss.append(el('span', 'bnd', stage.boundary), el('span', 'stitle', stage.title));
    sd.append(ss, stageBlock(stage));
    box.append(sd);
  }
  card.append(box);
  card.addEventListener('click', (ev) => {
    if (ev.target.closest('details.stage')) return;   // opening a stage is not a selection
    player.manual = true;
    follow(d, true);
  });
  return card;
}

function pendingCard(a) {
  const card = el('div', 'call pending pend');
  const row = el('div', 'row');
  row.append(el('span', 'badge c-pending', 'waiting'));
  row.append(el('span', 'cmd', a.command || ''));
  card.append(row);
  const why = el('div', 'thr');
  why.append('call_id ', el('span', 'val', a.call_id), '  ·  pattern ',
             el('span', 'val', a.pattern || '-'));
  const w = a.why || {};
  why.append('  ·  reason ', el('span', 'val', w.decision_reason || '-'));
  card.append(why);
  card.append(el('div', 'thr', 'answer with:  python -m toolgate approve ' + a.call_id +
    ' --choice approve_once|approve_session|deny'));
  return card;
}

/* ---- render and poll ---- */

function render(d) {
  state = d;
  byCall = {};
  for (const c of d.calls || []) byCall[c.call_id] = c;

  // keep the followed call pointing at the freshly parsed copy, so its status stays current
  if (player.call) {
    const now = byCall[player.call.call_id];
    if (now) {
      player.call = now;
      player.path = (now.path || []).filter(id => byEdge[id]);
      if (player.pkt) player.pkt.setAttribute('class', 'packet ' + now.status);
    }
  }

  chipsFor(d.counts || {});
  stamp.textContent = 'updated ' + d.generated_at.slice(11, 19) + '  ·  ' + d.total + ' calls';
  if (!topo && d.topology) buildTopology(d.topology);

  const rows = d.pending || [];
  pending.replaceChildren();
  if (!rows.length) pending.append(el('div', 'empty', 'nothing waiting'));
  else for (const a of rows) pending.append(pendingCard(a));

  for (const k in callEls) delete callEls[k];
  calls.replaceChildren();
  if (!d.calls.length) calls.append(el('div', 'empty', 'no calls recorded yet'));
  else for (const c of d.calls) {
    const card = callCard(c);
    callEls[c.call_id] = card;
    calls.append(card);
  }

  const fresh = (d.calls || []).filter(c => !player.seen.has(c.call_id));
  for (const c of fresh) player.seen.add(c.call_id);

  if (firstRender) {
    // on a cold start, park on the newest call and let the reader drive
    firstRender = false;
    if (d.calls.length && !player.call) follow(d.calls[d.calls.length - 1], true);
  } else if (fresh.length) {
    // a call that arrives while you are watching takes over — until you take the wheel
    const newest = fresh[fresh.length - 1];
    if (!player.manual) { follow(newest, true); player.playing = true; run(); }
  }

  paintSelection();
  renderDetail();
  pickOptions(d.calls || []);
  paintPlayer();
  // the submit box only exists when the server will answer it
  document.getElementById('decidebar').hidden = !d.can_decide;
}

/* ---- the controls ---- */

pickSel.addEventListener('change', () => {
  const call = byCall[pickSel.value];
  if (!call) return;
  player.manual = true;
  follow(call, true);          // chosen from the dropdown: always start from the beginning
});

document.getElementById('back').addEventListener('click', () => {
  player.manual = true; player.playing = false;
  parkAt(player.pos - 1);
});
document.getElementById('step').addEventListener('click', () => {
  player.manual = true; player.playing = false;
  if (!player.call) { follow(state.calls[state.calls.length - 1], true); return; }
  if (player.pos >= player.path.length) parkAt(0);   // at the end: wind back to the start
  else hop();
});
document.getElementById('play').addEventListener('click', () => {
  player.manual = true;
  if (!player.call) follow(state.calls[state.calls.length - 1], true);
  if (player.playing) { player.playing = false; paintPlayer(); return; }
  if (player.pos >= player.path.length) parkAt(0);
  player.playing = true;
  paintPlayer();
  run();
});
document.getElementById('restart').addEventListener('click', () => {
  player.manual = true;
  if (!player.call) follow(state.calls[state.calls.length - 1], true);
  parkAt(0);
  player.playing = true;
  paintPlayer();
  run();
});
speedSel.addEventListener('change', () => { msPerEdge = Number(speedSel.value); });

/* ---- asking the gate about a command ------------------------------------------
   The server decides and records; nothing here runs the command. The box is only
   present at all when the viewer was started with --allow-decide. */

const cmdInput = document.getElementById('cmd');
const verdict = document.getElementById('verdict');

async function ask() {
  const command = cmdInput.value.trim();
  if (!command) return;
  const runBtn = document.getElementById('run');
  runBtn.disabled = true;
  verdict.className = 'decided';
  verdict.textContent = 'asking the gate…';
  try {
    const r = await fetch('/api/decide', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command }),
    });
    // read the body as text first: a refusal may not be JSON, and `r.json()` on "not found" throws
    // a SyntaxError that says nothing about what actually happened
    const raw = await r.text();
    let d = {};
    try { d = JSON.parse(raw); } catch (e) { /* not JSON — report it below as it came */ }
    if (!r.ok) {
      verdict.className = 'decided err';
      verdict.textContent = 'refused: ' + (d.error || raw.trim().slice(0, 120) || 'HTTP ' + r.status);
      return;
    }
    const why = (d.policies && d.policies.length) ? ' · ' + d.policies.join(', ')
              : (d.reason ? ' · ' + d.reason : '');
    verdict.className = 'decided ' + (d.status === 'allowed' ? 'ok'
                                   : d.status === 'blocked' ? 'no'
                                   : d.status === 'pending' ? 'wait' : 'err');
    verdict.textContent = '#' + d.seq + ' ' + d.status + why + ' — decided, not run';
    // take the wheel before the poll: otherwise render auto-plays the new call, and its animation
    // is still in flight when we try to park here
    player.manual = true;
    last = '';                       // re-render now rather than waiting out the poll
    await tick();
    const call = byCall[d.call_id];
    if (call) follow(call, true);    // park on it, ready to step
  } catch (e) {
    verdict.className = 'decided err';
    verdict.textContent = 'failed: ' + e;
  } finally {
    runBtn.disabled = false;
  }
}

document.getElementById('run').addEventListener('click', ask);
cmdInput.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') ask(); });

let last = '';
async function tick() {
  try {
    const r = await fetch('/api/calls', { cache: 'no-store' });
    const d = await r.json();
    dot.className = 'dot live';
    const sig = JSON.stringify({ ...d, generated_at: null });   // ignore the clock
    if (sig !== last) { last = sig; render(d); }
    else stamp.textContent = 'updated ' + d.generated_at.slice(11, 19) +
                             '  ·  ' + d.total + ' calls';
  } catch (e) {
    dot.className = 'dot dead';
    stamp.textContent = 'viewer lost the server — retrying';
  }
}
tick();
setInterval(tick, 1000);
</script></body></html>
"""
