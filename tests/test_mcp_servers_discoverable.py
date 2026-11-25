"""
Real MCP-protocol-level test: for each of the three servers, spawn it as a
real subprocess over stdio and use the actual `mcp` SDK ClientSession to
list its tools -- this is the "confirm each tool is discoverable and
callable" acceptance criterion from Phase 1, exercised against the real
transport rather than a simulated one.
"""
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

_BACKEND_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_TOOLS = {
    "k8s_server.py": {"get_pod_status", "get_container_logs", "describe_deployment", "get_pod_metrics", "get_pod_events"},
    "log_server.py": {"query_logs", "parse_stack_trace"},
    "git_server.py": {
        "get_recent_commits",
        "get_file_content",
        "create_isolated_branch",
        "apply_patch_and_test",
        "open_pull_request",
    },
}


@pytest.mark.parametrize("server_file,expected", EXPECTED_TOOLS.items())
async def test_server_tools_discoverable(server_file, expected):
    script = _BACKEND_ROOT / "mcp_servers" / server_file
    params = StdioServerParameters(command=sys.executable, args=[str(script)])

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert expected.issubset(names)
