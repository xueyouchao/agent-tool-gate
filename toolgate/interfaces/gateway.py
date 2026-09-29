"""MCP stdio gateway — a proxy over upstream servers with the ToolGate authorization seam.

The gate authorizes only ``tools/call`` (§2.5). ALLOW is exact passthrough (forward the
upstream ``CallToolResult`` unchanged, with trace context injected into ``_meta``); BLOCK /
ESCALATE are synthesized ``CallToolResult``s with ``is_error=True`` carrying the structured
"why", so the model sees a result — never a JSON-RPC crash (research 02 §3).
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Protocol

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from ..application.engine import Engine


def decision_to_result(decision: dict) -> CallToolResult:
    """Map an engine decision entry to a structured block/escalate ``CallToolResult``."""
    call_id = decision["call_id"]
    if decision["decision"] == "block":
        structured = {
            "status": "blocked",
            "call_id": call_id,
            "why": {"policy_ids": decision["determining_policies"]},
        }
    else:  # escalate
        structured = {
            "status": "blocked_pending_approval",
            "call_id": call_id,
            "summary": decision["command"],
            "why": {
                "policy_ids": decision["determining_policies"],
                "answers": decision.get("answers"),
                "decision_reason": decision.get("decision_reason"),
            },
            "options": ["approve_once", "approve_session", "deny"],
        }
    return _result(structured)


def _error_result(exc: Exception) -> CallToolResult:
    """Fail-closed guard: the model gets a structured result, never an internal error leak."""
    return _result({
        "status": "error",
        "error": type(exc).__name__,
        "message": "authorization failed; call blocked (fail-closed)",
    })


def _result(structured: dict) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(structured))],
        structured_content=structured,
        is_error=True,
    )


def _trace_meta(call_id: str) -> dict:
    """SEP-414 trace context for the forwarded call, correlated to the decision's call_id."""
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    return {
        "traceparent": f"00-{trace_id}-{span_id}-01",
        "baggage": f"toolgate_call_id={call_id}",
    }


class Upstream(Protocol):
    tools: dict[str, Tool]

    async def call_tool(self, name: str, arguments: dict | None, **kw) -> CallToolResult: ...


class ToolGateProxy:
    def __init__(self, engine: Engine, upstream: Upstream):
        self.engine = engine
        self.upstream = upstream

    async def list_tools(self, ctx, params) -> ListToolsResult:
        return ListToolsResult(tools=list(self.upstream.tools.values()))

    async def call_tool(self, ctx, params: types.CallToolRequestParams) -> CallToolResult:
        name = params.name
        arguments = params.arguments or {}
        try:
            # the engine is synchronous (Cedar + blocking Jev HTTP) — keep it off the loop
            decision = await asyncio.to_thread(self.engine.authorize_tool_call, name, arguments)
        except Exception as exc:  # noqa: BLE001 — fail closed, never crash the session
            return _error_result(exc)

        if decision["decision"] == "allow":
            return await self._forward(name, arguments, decision)
        return decision_to_result(decision)

    async def _forward(self, name, arguments, decision) -> CallToolResult:
        # exact passthrough (§2.5): forward unchanged, inject trace context only
        return await self.upstream.call_tool(name, arguments, meta=_trace_meta(decision["call_id"]))

    def server(self) -> Server:
        return Server("toolgate", on_list_tools=self.list_tools, on_call_tool=self.call_tool)

    async def run_stdio(self) -> None:
        server = self.server()
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
