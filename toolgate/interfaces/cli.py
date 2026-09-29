"""CLI subcommands: `toolgate log` (replay viewer), `toolgate approve`, `toolgate prompt`.

`main` builds the one decision record the subcommands act on, so a command handler never
constructs storage of its own — it reads and writes through the record's interface.
"""
from __future__ import annotations

import argparse
import json

from ..infrastructure.approval import ApprovalStore
from ..infrastructure.decision_record import DecisionRecord
from ..infrastructure.jsonl_log import JsonlLog

# single-key answers to the interactive prompt → the choice vocabulary of `toolgate approve`
_PROMPT_CHOICES = {"a": "approve_once", "s": "approve_session", "d": "deny"}


def record_for(args) -> DecisionRecord:
    """The record a subcommand works on. `log` names a decision file; the others an approvals
    file — so a missing one means "no outcomes to correlate", not "no record"."""
    approvals = getattr(args, "approvals", None)
    return DecisionRecord(
        JsonlLog(getattr(args, "file", None) or "decisions.jsonl"),
        ApprovalStore(approvals) if approvals else ApprovalStore(None),
    )


def cmd_log(args, record: DecisionRecord) -> None:
    print(f"{'seq':>4}  {'phase':>5}  {'decision':9}  {'policies':32}  {'outcome':26}  command")
    for d in record.records():
        outcome = d.get("outcome")
        oc = json.dumps(outcome) if outcome else "-"
        print(f"{d['seq']:>4}  {d['phase']:>5}  {d['decision']:9}  "
              f"{str(d['determining_policies']):32}  {oc:26}  {d['command']}")


def cmd_approve(args, record: DecisionRecord) -> None:
    a = record.decide(args.call_id, args.choice)
    print(f"{a.call_id}: {a.status} ({a.choice})")


def cmd_prompt(args, record: DecisionRecord) -> None:
    pending = record.pending()
    if not pending:
        print("no pending approvals")
        return
    for a in pending:
        print(f"\n[{a.call_id}] {a.command}")
        print(f"  why: {json.dumps(a.why)}")
        try:
            choice = input("  [a]pprove_once  [s]ession  [d]eny  (enter=skip): ").strip().lower()
        except EOFError:  # scripted or closed stdin: leave them pending, never crash
            print("\n(no more input — remaining approvals left pending)")
            return
        if choice in _PROMPT_CHOICES:
            record.decide(a.call_id, _PROMPT_CHOICES[choice])
        else:
            print("  skipped")


def _decider(args):
    """A function that puts a command to the real gate, for the viewer's submit box.

    A fresh engine per submission, for the same reason the page re-reads every store on each
    poll: ``JsonlLog`` numbers entries by counting the lines already there, so one held open
    across a long-running session would hand out sequence numbers a concurrent writer had
    already used. Rebuilding costs about 5ms and re-reads the policies, the approvals and the
    line count together, so the number it writes is the number that is true now.

    ``Engine.authorize_tool_call`` authorizes and records. It does not forward — the gateway is
    what forwards, and it is not in this process — so nothing submitted here can run.
    """
    from ..container import Container

    def decide(command: str) -> dict:
        container = Container()
        container.config.from_dict({
            "log_path": args.file,
            "approvals_path": args.approvals or "pending_approvals.json",
            "trace_path": args.trace,
        })
        return container.engine().authorize_tool_call("bash", {"command": command})

    return decide


def cmd_view(args, record: DecisionRecord) -> None:
    """Serve the live page. `record` is unused on purpose: the viewer is long-running and must
    re-read every store on each poll, so it is handed factories instead of one record."""
    from ..infrastructure.trace import read_traces
    from .viewer import serve
    serve(lambda: record_for(args), host=args.host, port=args.port,
          trace_for=lambda: read_traces(args.trace),
          decide=_decider(args) if args.allow_decide else None)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="toolgate", description="ToolGate CLI")
    sub = p.add_subparsers(dest="cmd")

    sp_log = sub.add_parser("log", help="replay the decision log")
    sp_log.add_argument("file")
    sp_log.add_argument("--approvals", help="approval store to correlate outcomes")
    sp_log.set_defaults(func=cmd_log)

    sp_approve = sub.add_parser("approve", help="decide a pending approval")
    sp_approve.add_argument("call_id")
    sp_approve.add_argument("--choice", required=True,
                            choices=["approve_once", "approve_session", "deny"])
    sp_approve.add_argument("--approvals", default="pending_approvals.json")
    sp_approve.set_defaults(func=cmd_approve)

    sp_prompt = sub.add_parser("prompt", help="interactive a/s/d prompt for pending approvals")
    sp_prompt.add_argument("--approvals", default="pending_approvals.json")
    sp_prompt.set_defaults(func=cmd_prompt)

    sp_view = sub.add_parser("view", help="live web view of the calls (re-reads as they arrive)")
    sp_view.add_argument("file", help="decision log to watch")
    sp_view.add_argument("--approvals", help="approval store to correlate outcomes")
    sp_view.add_argument("--trace", default="traces.jsonl",
                         help="boundary trace, which fills in what the decision log omits")
    sp_view.add_argument("--host", default="127.0.0.1", help="loopback by default: the log holds commands")
    sp_view.add_argument("--port", type=int, default=8770)
    sp_view.add_argument("--allow-decide", action="store_true",
                         help="add a box to the page that puts a command to the real gate. It is "
                              "DECIDED AND RECORDED, NEVER RUN — the engine authorizes, nothing "
                              "forwards. Still, it writes to the log and approvals above, so do "
                              "not point it at files a live gateway is also writing")
    sp_view.set_defaults(func=cmd_view)

    args = p.parse_args(argv)
    if not hasattr(args, "func"):
        p.print_help()
        return
    args.func(args, record_for(args))
