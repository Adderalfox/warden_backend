"""
Real LangGraph node implementations. Nodes never touch infrastructure
directly -- all k8s/log/git work goes through `mcp_manager.call(...)`, which
talks to the real MCP servers. The only non-MCP call is the LLM call in
PatchGenerationNode, which uses the real Anthropic SDK.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict

import google.generativeai as genai

from app.core.config import settings
from app.graph.state import IncidentState
from app.mcp_clients.manager import mcp_manager

_SERVICE_NAME_RE = re.compile(r'"?service"?\s*[:=]\s*"?([\w\-]+)"?', re.IGNORECASE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(state: IncidentState, node: str, summary: str, tool_calls: list[str]) -> None:
    state["agent_logs"].append(
        {"node": node, "timestamp": _now(), "summary": summary, "tool_calls": tool_calls}
    )


async def _safe_run(node_name: str, state: IncidentState, coro):
    """Wrap node body: on any exception, record it into agent_logs, set
    status=FAILED, and never let the exception crash the FastAPI process /
    graph executor."""
    try:
        return await coro
    except Exception as e:  # noqa: BLE001 - intentional broad catch at node boundary
        state["status"] = "FAILED"
        state["failure_reason"] = f"{node_name}: {e!r}"
        _log(state, node_name, f"FAILED: {e}", [])
        return state


# ---------------------------------------------------------------------------
# IngestNode
# ---------------------------------------------------------------------------
async def ingest_node(state: IncidentState) -> IncidentState:
    async def _run():
        trace = state["raw_stack_trace"] or ""
        m = _SERVICE_NAME_RE.search(trace) or _SERVICE_NAME_RE.search(state.get("error_summary", ""))
        if m and not state.get("service_name"):
            state["service_name"] = m.group(1)

        # Pull a first-pass structured parse via mcp-logs so downstream nodes
        # have a real exception signature to search on.
        parsed = await mcp_manager.call("logs", "parse_stack_trace", raw_log=trace)
        exc_type = parsed.get("exception_type") if isinstance(parsed, dict) else None
        exc_msg = parsed.get("exception_message") if isinstance(parsed, dict) else None
        if exc_type or exc_msg:
            state["error_summary"] = state.get("error_summary") or f"{exc_type}: {exc_msg}"
        if isinstance(parsed, dict) and parsed.get("top_frame_file"):
            state["affected_files"] = list({parsed["top_frame_file"], *state.get("affected_files", [])})

        state["status"] = "DIAGNOSING"
        _log(state, "IngestNode", f"Parsed incident for service={state['service_name']}", ["mcp-logs.parse_stack_trace"])
        return state

    return await _safe_run("IngestNode", state, _run())


# ---------------------------------------------------------------------------
# DiagnosticNode — parallel k8s + logs calls
# ---------------------------------------------------------------------------
async def diagnostic_node(state: IncidentState) -> IncidentState:
    import asyncio

    async def _run():
        namespace = state.get("namespace") or settings.k8s_namespace_default
        service_name = state["service_name"]

        pod_status_task = mcp_manager.call("k8s", "get_pod_status", namespace=namespace, service_name=service_name)
        end = datetime.now(timezone.utc)
        start = end.replace(hour=max(end.hour - 1, 0))
        logs_task = mcp_manager.call(
            "logs", "query_logs",
            query=f'service:"{service_name}" AND level:ERROR',
            start_time=start.isoformat(),
            end_time=end.isoformat(),
            limit=50,
        )

        pod_metrics_task = mcp_manager.call(
            "k8s", "get_pod_metrics", service_name=service_name, namespace=namespace
        )

        pod_status, log_hits, pod_metrics = await asyncio.gather(
            pod_status_task, logs_task, pod_metrics_task, return_exceptions=True
        )

        tool_calls = ["mcp-k8s.get_pod_status", "mcp-logs.query_logs", "mcp-k8s.get_pod_metrics"]

        # Pod analytics are best-effort context: never fail the diagnosis over them.
        if isinstance(pod_metrics, Exception):
            _log(state, "DiagnosticNode", f"pod metrics lookup failed: {pod_metrics}", tool_calls)
        elif isinstance(pod_metrics, dict) and pod_metrics.get("found"):
            state["git_context"]["pod_metrics"] = pod_metrics

        if isinstance(pod_status, Exception):
            _log(state, "DiagnosticNode", f"pod status lookup failed: {pod_status}", tool_calls)
            pod_status = {"pods": [], "found": False}
        else:
            for pod in pod_status.get("pods", []):
                pod_logs = await mcp_manager.call(
                    "k8s", "get_container_logs", pod_name=pod["pod_name"], lines=200, tail=True
                )
                state["logs_collected"].append(pod_logs)
                tool_calls.append("mcp-k8s.get_container_logs")

        if isinstance(log_hits, Exception):
            _log(state, "DiagnosticNode", f"log query failed: {log_hits}", tool_calls)
        elif isinstance(log_hits, list):
            state["logs_collected"].extend(json.dumps(hit) for hit in log_hits)

        state["git_context"]["pod_status"] = pod_status
        state["status"] = "DIAGNOSING"
        _log(
            state, "DiagnosticNode",
            f"Collected {len(state['logs_collected'])} log entries; "
            f"{len(pod_status.get('pods', []))} pods found",
            tool_calls,
        )
        return state

    return await _safe_run("DiagnosticNode", state, _run())


# ---------------------------------------------------------------------------
# ContextGatherNode — recent commits/blame via mcp-git
# ---------------------------------------------------------------------------
async def context_gather_node(state: IncidentState) -> IncidentState:
    async def _run():
        tool_calls = []
        commits_by_file: Dict[str, Any] = {}
        file_contents: Dict[str, Any] = {}
        for filepath in state.get("affected_files") or [state["service_name"]]:
            commits = await mcp_manager.call(
                "git", "get_recent_commits", repo=state["repo_url"], filepath=filepath, limit=5
            )
            commits_by_file[filepath] = commits
            tool_calls.append("mcp-git.get_recent_commits")

            content = await mcp_manager.call(
                "git", "get_file_content", repo=state["repo_url"], filepath=filepath
            )
            file_contents[filepath] = content
            tool_calls.append("mcp-git.get_file_content")

        state["git_context"]["recent_commits"] = commits_by_file
        state["git_context"]["file_contents"] = file_contents
        _log(state, "ContextGatherNode", f"Gathered commit history + file contents for {len(commits_by_file)} file(s)", tool_calls)
        return state

    return await _safe_run("ContextGatherNode", state, _run())


# ---------------------------------------------------------------------------
# PatchGenerationNode — real Anthropic call, no MCP
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = """You are Warden, an SRE remediation agent. Given an incident's \
stack trace, collected logs, recent commit history, and the CURRENT CONTENTS of the \
affected file(s) (under git_context.file_contents), propose a minimal, safe unified \
diff (git apply compatible) that fixes the root cause. The diff's context lines must \
match the provided file contents exactly. Respond with ONLY the raw unified diff (no \
markdown fences, no commentary). If git_context.file_contents shows a file as not \
found, or you cannot confidently produce a fix, respond with exactly: \
NO_PATCH: <reason>."""


async def patch_generation_node(state: IncidentState) -> IncidentState:
    async def _run():
        if not settings.gemini_api_key:
            raise RuntimeError("WARDEN_GEMINI_API_KEY is not configured; cannot generate a patch")

        genai.configure(api_key=settings.gemini_api_key)
        user_content = json.dumps(
            {
                "error_summary": state["error_summary"],
                "raw_stack_trace": state["raw_stack_trace"],
                "logs_collected": state["logs_collected"][:20],
                "git_context": state["git_context"],
                "affected_files": state.get("affected_files"),
            },
            default=str,
        )

        model = genai.GenerativeModel(
            model_name=settings.gemini_model,
            system_instruction=_SYSTEM_PROMPT,
        )
        resp = model.generate_content(user_content)
        text = resp.text.strip()

        if text.startswith("NO_PATCH"):
            state["status"] = "FAILED"
            state["failure_reason"] = text
            _log(state, "PatchGenerationNode", text, ["anthropic.messages.create"])
            return state

        state["proposed_diff"] = text
        state["status"] = "PATCH_GENERATED"
        _log(state, "PatchGenerationNode", "Generated candidate patch", ["anthropic.messages.create"])
        return state

    return await _safe_run("PatchGenerationNode", state, _run())


# ---------------------------------------------------------------------------
# ValidationNode — apply diff + run tests via mcp-git
# ---------------------------------------------------------------------------
async def validation_node(state: IncidentState) -> IncidentState:
    async def _run():
        if not state.get("proposed_diff"):
            raise RuntimeError("No proposed_diff to validate")

        branch = f"warden/fix-{state['incident_id']}"
        await mcp_manager.call(
            "git", "create_isolated_branch",
            repo=state["repo_url"], base_branch="main", new_branch=branch,
        )
        result = await mcp_manager.call(
            "git", "apply_patch_and_test",
            repo=state["repo_url"], diff=state["proposed_diff"],
            test_cmd="pytest -q", branch=branch,
        )

        state["test_passed"] = bool(result.get("passed"))
        state["git_context"]["validation_result"] = result
        state["git_context"]["branch"] = branch

        tool_calls = ["mcp-git.create_isolated_branch", "mcp-git.apply_patch_and_test"]
        if not result.get("applied"):
            state["status"] = "FAILED"
            state["failure_reason"] = result.get("apply_error") or "patch failed to apply"
            _log(state, "ValidationNode", f"Patch apply failed: {state['failure_reason']}", tool_calls)
            return state

        if not state["test_passed"]:
            state["status"] = "FAILED"
            state["failure_reason"] = "Tests failed after applying patch"
            _log(state, "ValidationNode", "Patch applied but tests failed", tool_calls)
            return state

        state["status"] = "AWAITING_APPROVAL"
        _log(state, "ValidationNode", "Patch applied and tests passed", tool_calls)
        return state

    return await _safe_run("ValidationNode", state, _run())


# ---------------------------------------------------------------------------
# HumanApprovalNode — halts graph execution via LangGraph interrupt
# ---------------------------------------------------------------------------
async def human_approval_node(state: IncidentState) -> IncidentState:
    from langgraph.types import interrupt

    state["status"] = "AWAITING_APPROVAL"
    _log(state, "HumanApprovalNode", "Waiting for human approval", [])

    decision = interrupt(
        {
            "incident_id": state["incident_id"],
            "proposed_diff": state["proposed_diff"],
            "message": "Approve or reject this patch",
        }
    )
    state["approval_decision"] = decision
    return state


# ---------------------------------------------------------------------------
# RemediationNode — push branch + open PR via mcp-git
# ---------------------------------------------------------------------------
async def remediation_node(state: IncidentState) -> IncidentState:
    async def _run():
        if state.get("approval_decision") != "APPROVE":
            state["status"] = "FAILED"
            state["failure_reason"] = "Patch was rejected by reviewer"
            _log(state, "RemediationNode", "Reviewer rejected patch; not opening PR", [])
            return state

        branch = state["git_context"].get("branch", f"warden/fix-{state['incident_id']}")
        rca = (
            f"## Root Cause Analysis — {state['incident_id']}\n\n"
            f"**Service:** {state['service_name']}\n\n"
            f"**Error:** {state['error_summary']}\n\n"
            f"**Diagnosis:** Automated investigation via Warden. "
            f"See `agent_logs` for the full tool-call trace.\n\n"
            f"**Fix:** See attached diff.\n"
        )
        pr = await mcp_manager.call(
            "git", "open_pull_request",
            repo=state["repo_url"],
            title=f"Warden auto-fix: {state['error_summary'][:72]}",
            body=rca,
            head=branch,
            base="main",
        )
        state["pr_url"] = pr.get("url")
        state["status"] = "COMPLETED"
        _log(state, "RemediationNode", f"Opened PR: {state['pr_url']}", ["mcp-git.open_pull_request"])
        return state

    return await _safe_run("RemediationNode", state, _run())
