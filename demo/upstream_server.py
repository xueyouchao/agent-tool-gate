"""A minimal upstream MCP server for the ToolGate demo.

It exposes one ``bash`` tool and executes nothing — it echoes the command back. The demo is
therefore safe to run on any machine, while ToolGate still sees exactly the call an agent
would make (including the command string its policies match on).
"""
from mcp.server.mcpserver import MCPServer

server = MCPServer("demo-upstream")


@server.tool(description="Run a shell command.")
def bash(command: str) -> str:
    return f"[demo upstream] ran: {command}"


if __name__ == "__main__":
    server.run("stdio")
