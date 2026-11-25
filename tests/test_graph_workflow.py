"""
Exercises the real compiled StateGraph end-to-end. Per the cross-cutting
testability requirement, node *dependencies* (MCP calls, the Anthropic
client) are mocked at their boundary -- the graph wiring, state transitions,
and interrupt/resume mechanism under test are all real.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from langgraph.types import Command

from app.graph.state import IncidentState
from app.graph.workflow import build_graph


def _initial_state() -> IncidentState:
    return {
        "incident_id": "inc-test-001",
        "service_name": "checkout-service",
        "status": "INGESTED",
        "error_summary": "",
        "raw_stack_trace": 'File "/app/checkout.py", line 10, in charge\nValueError: bad amount',
        "logs_collected": [],
        "git_context": {},
        "proposed_diff": None,
        "test_passed": False,
        "pr_url": None,
        "agent_logs": [],
        "repo_url": "/tmp/fake-repo",
        "repo_path": "",
        "affected_files": [],
        "namespace": "default",
        "approval_decision": None,
        "failure_reason": None,
    }


async def _fake_mcp_call(server, tool, **kwargs):
    if server == "logs" and tool == "parse_stack_trace":
        return {
            "language": "python",
            "exception_type": "ValueError",
            "exception_message": "bad amount",
            "frames": [],
            "top_frame_file": "/app/checkout.py",
            "top_frame_line": 10,
        }
    if server == "logs" and tool == "query_logs":
        return [{"message": "charge failed"}]
    if server == "k8s" and tool == "get_pod_status":
        return {"pods": [{"pod_name": "checkout-abc123"}], "found": True}
    if server == "k8s" and tool == "get_pod_metrics":
        return {"found": True, "pods": [{"pod_name": "checkout-abc123", "mem_pct_of_limit": 95.0}], "insights": []}
    if server == "k8s" and tool == "get_container_logs":
        return "some log line"
    if server == "git" and tool == "get_recent_commits":
        return [{"sha": "abc123", "author": "a", "date": "2024-01-01", "message": "fix"}]
    if server == "git" and tool == "get_file_content":
        return {"found": True, "content": "def charge(a):\n    return old\n"}
    if server == "git" and tool == "create_isolated_branch":
        return True
    if server == "git" and tool == "apply_patch_and_test":
        return {"applied": True, "passed": True, "exit_code": 0, "stdout": "", "stderr": ""}
    if server == "git" and tool == "open_pull_request":
        return {"url": "https://github.com/example/repo/pull/1", "number": 1, "dry_run": False}
    raise AssertionError(f"unexpected mcp call {server}.{tool}")


def _fake_gemini_response():
    return SimpleNamespace(text="--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n")


@pytest.mark.asyncio
async def test_graph_pauses_at_approval_then_resumes_to_completed():
    graph = build_graph()
    config = {"configurable": {"thread_id": "inc-test-001"}}

    with patch("app.graph.nodes.mcp_manager.call", new=AsyncMock(side_effect=_fake_mcp_call)), \
         patch("app.graph.nodes.settings.gemini_api_key", "fake-key"), \
         patch("app.graph.nodes.genai") as mock_genai:
        mock_genai.GenerativeModel.return_value.generate_content.return_value = _fake_gemini_response()

        result = await graph.ainvoke(_initial_state(), config=config)
        assert result["status"] == "AWAITING_APPROVAL"
        assert result["proposed_diff"] is not None
        assert len(result["agent_logs"]) >= 4
        assert result["git_context"]["pod_metrics"]["found"] is True   # pod analytics reach the agent

        result2 = await graph.ainvoke(Command(resume="APPROVE"), config=config)
        assert result2["status"] == "COMPLETED"
        assert result2["pr_url"] == "https://github.com/example/repo/pull/1"
