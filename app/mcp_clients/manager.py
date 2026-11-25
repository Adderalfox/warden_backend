"""
Real MCP client connection pool. Graph nodes never import k8s/git/ES clients
directly -- they go through this manager, which spawns/connects to the three
MCP servers over stdio (or SSE, if WARDEN_MCP_TRANSPORT=sse and URLs are set)
and calls tools via the official `mcp` SDK ClientSession.
"""
from __future__ import annotations

import json
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.core.config import settings

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_SERVERS = {
    "k8s": _BACKEND_ROOT / "mcp_servers" / "k8s_server.py",
    "logs": _BACKEND_ROOT / "mcp_servers" / "log_server.py",
    "git": _BACKEND_ROOT / "mcp_servers" / "git_server.py",
}


class MCPClientManager:
    """Lazily connects to each MCP server on first use and keeps the session
    open for the lifetime of the manager. One manager per FastAPI process."""

    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        self._started = False

    async def _connect(self, name: str) -> ClientSession:
        if name in self._sessions:
            return self._sessions[name]

        if settings.mcp_transport == "sse":
            from mcp.client.sse import sse_client

            url = {
                "k8s": settings.mcp_k8s_url,
                "logs": settings.mcp_logs_url,
                "git": settings.mcp_git_url,
            }[name]
            if not url:
                raise RuntimeError(f"WARDEN_MCP_{name.upper()}_URL is not set but transport=sse")
            read, write = await self._stack.enter_async_context(sse_client(url))
        else:
            script = _SERVERS[name]
            params = StdioServerParameters(command=sys.executable, args=[str(script)])
            read, write = await self._stack.enter_async_context(stdio_client(params))

        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._sessions[name] = session
        return session

    async def call(self, server: str, tool: str, timeout: float = 120.0, **kwargs: Any) -> Any:
        import asyncio
        session = await self._connect(server)
        try:
            result = await asyncio.wait_for(session.call_tool(tool, arguments=kwargs), timeout=timeout)
        except asyncio.TimeoutError as e:
            raise RuntimeError(f"MCP call {server}.{tool} timed out after {timeout}s") from e
        if result.isError:
            text = "; ".join(c.text for c in result.content if hasattr(c, "text"))
            raise RuntimeError(f"MCP tool {server}.{tool} failed: {text}")

        # FastMCP tools return a single structured-content block (dict/list)
        # or a text block containing JSON.
        for block in result.content:
            if hasattr(block, "text"):
                try:
                    return json.loads(block.text)
                except json.JSONDecodeError:
                    return block.text
        return None

    async def list_tools(self, server: str) -> list[str]:
        session = await self._connect(server)
        resp = await session.list_tools()
        return [t.name for t in resp.tools]

    async def aclose(self) -> None:
        await self._stack.aclose()
        self._sessions.clear()


# Process-wide singleton used by graph nodes and FastAPI startup/shutdown.
mcp_manager = MCPClientManager()
