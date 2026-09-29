"""``python -m toolgate`` — run the stdio gateway over configured upstream MCP servers."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from mcp import ClientSessionGroup, StdioServerParameters

from .application.engine import Engine
from .container import Container
from .interfaces.gateway import ToolGateProxy


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="toolgate", description="ToolGate MCP stdio gateway")
    p.add_argument("--upstream", action="append", default=[], metavar="JSON",
                   help='upstream server as JSON: {"command": "...", "args": [...]}')
    p.add_argument("--agent-name", default="cli-agent", help="principal Agent name")
    p.add_argument("--log", default="decisions.jsonl", help="decision log path")
    p.add_argument("--approvals", default="pending_approvals.json",
                   help="approval store path (ESCALATE pause/approve/deny)")
    p.add_argument("--trace", default="traces.jsonl",
                   help="boundary trace path the viewer reads (scrubbed)")
    p.add_argument("--trace-raw", action="store_true",
                   help="record boundary payloads UNSCRUBBED — the trace then holds whatever "
                        "the agent was about to send, in the clear")
    p.add_argument("--consistency-samples", type=int, default=3)
    return p.parse_args(argv)


def make_container(args: argparse.Namespace) -> Container:
    """Bind CLI args onto the container's config (defaults cover everything else)."""
    container = Container()
    container.config.from_dict({
        "agent_name": args.agent_name,
        "log_path": args.log,
        "approvals_path": args.approvals,
        "trace_path": args.trace,
        "trace_raw": args.trace_raw,
        "consistency_samples": args.consistency_samples,
    })
    return container


def build_engine(args: argparse.Namespace) -> Engine:
    """Container-wired engine (convenience for library users and tests)."""
    return make_container(args).engine()


async def _main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    container = make_container(args)

    group = ClientSessionGroup()
    for spec in args.upstream:
        cfg = json.loads(spec)
        await group.connect_to_server(StdioServerParameters(command=cfg["command"], args=cfg.get("args", [])))

    proxy: ToolGateProxy = container.proxy(upstream=group)
    await proxy.run_stdio()


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("log", "approve", "prompt", "view"):
        from .interfaces.cli import main as cli_main
        cli_main(sys.argv[1:])
    else:
        asyncio.run(_main())


if __name__ == "__main__":
    main()
