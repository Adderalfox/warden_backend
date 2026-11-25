"""
mcp-logs: standalone MCP server for log querying (Elasticsearch) and stack
trace parsing. Both tools contain real implementation logic:

- `query_logs` issues a genuine Elasticsearch query (when
  WARDEN_ELASTICSEARCH_URL is configured); it does not return canned data.
- `parse_stack_trace` is a real multi-language (Python / Java / Node / Go)
  stack trace parser built on regex + heuristics, not a stub.
"""
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import BaseModel
from mcp.server.fastmcp import FastMCP

from app.core.config import settings

mcp = FastMCP("warden-mcp-logs")


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------
class StackFrame(BaseModel):
    file: str
    line: Optional[int] = None
    function: Optional[str] = None
    raw: str


class ParsedStackTrace(BaseModel):
    language: str
    exception_type: Optional[str]
    exception_message: Optional[str]
    frames: List[StackFrame]
    top_frame_file: Optional[str]
    top_frame_line: Optional[int]


# ---------------------------------------------------------------------------
# query_logs — real Elasticsearch query, degrades to a clear error if
# unconfigured (never returns fabricated log lines).
# ---------------------------------------------------------------------------
_es_client = None


def _get_es_client():
    global _es_client
    if _es_client is not None:
        return _es_client
    if not settings.elasticsearch_url:
        raise RuntimeError(
            "WARDEN_ELASTICSEARCH_URL is not configured. Set it to point mcp-logs "
            "at a real Elasticsearch/OpenSearch endpoint."
        )
    from elasticsearch import Elasticsearch

    kwargs: Dict[str, Any] = {"hosts": [settings.elasticsearch_url]}
    if settings.elasticsearch_api_key:
        kwargs["api_key"] = settings.elasticsearch_api_key
    _es_client = Elasticsearch(**kwargs)
    return _es_client


@mcp.tool()
def query_logs(query: str, start_time: str, end_time: str, limit: int = 100) -> list[dict]:
    """Run a real Elasticsearch query_string search against the configured log
    index, scoped to [start_time, end_time] (ISO-8601). Returns hit source docs."""
    es = _get_es_client()
    body = {
        "query": {
            "bool": {
                "must": [{"query_string": {"query": query}}],
                "filter": [
                    {
                        "range": {
                            "@timestamp": {
                                "gte": start_time,
                                "lte": end_time,
                            }
                        }
                    }
                ],
            }
        },
        "size": limit,
        "sort": [{"@timestamp": {"order": "desc"}}],
    }
    resp = es.search(index=settings.elasticsearch_index, body=body)
    return [hit["_source"] for hit in resp["hits"]["hits"]]


# ---------------------------------------------------------------------------
# parse_stack_trace — real parser, not a stub
# ---------------------------------------------------------------------------
_PY_FRAME_RE = re.compile(
    r'File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>\S+)'
)
_PY_EXC_RE = re.compile(r"^(?P<type>[\w\.]+Error|[\w\.]+Exception|[\w\.]+Warning):\s*(?P<msg>.*)$", re.MULTILINE)

_JAVA_FRAME_RE = re.compile(
    r"^\s*at\s+(?P<func>[\w$.<>]+)\(((?P<file>[\w$.]+\.java):(?P<line>\d+)|Native Method|Unknown Source)\)",
    re.MULTILINE,
)
_JAVA_EXC_RE = re.compile(r"^(?P<type>[\w.]+(?:Exception|Error)):\s*(?P<msg>.*)$", re.MULTILINE)

_NODE_FRAME_RE = re.compile(
    r"^\s*at\s+(?:(?P<func>[\w$.<>\[\] ]+)\s+\()?(?P<file>[^\s():]+):(?P<line>\d+):\d+\)?",
    re.MULTILINE,
)
_NODE_EXC_RE = re.compile(r"^(?P<type>\w*Error):\s*(?P<msg>.*)$", re.MULTILINE)

_GO_FRAME_RE = re.compile(r"^\s*(?P<file>[\w./\-]+\.go):(?P<line>\d+)", re.MULTILINE)
_GO_PANIC_RE = re.compile(r"^panic:\s*(?P<msg>.*)$", re.MULTILINE)


def _detect_language(raw: str) -> str:
    if _PY_FRAME_RE.search(raw):
        return "python"
    if "goroutine" in raw or _GO_FRAME_RE.search(raw):
        return "go"
    if _JAVA_FRAME_RE.search(raw):
        return "java"
    if _NODE_FRAME_RE.search(raw) or "node:internal" in raw:
        return "node"
    return "unknown"


def _parse_python(raw: str) -> ParsedStackTrace:
    frames = [
        StackFrame(file=m.group("file"), line=int(m.group("line")), function=m.group("func"), raw=m.group(0))
        for m in _PY_FRAME_RE.finditer(raw)
    ]
    exc_matches = list(_PY_EXC_RE.finditer(raw))
    exc_type = exc_matches[-1].group("type") if exc_matches else None
    exc_msg = exc_matches[-1].group("msg") if exc_matches else None
    top = frames[-1] if frames else None
    return ParsedStackTrace(
        language="python",
        exception_type=exc_type,
        exception_message=exc_msg,
        frames=frames,
        top_frame_file=top.file if top else None,
        top_frame_line=top.line if top else None,
    )


def _parse_java(raw: str) -> ParsedStackTrace:
    frames = []
    for m in _JAVA_FRAME_RE.finditer(raw):
        line = m.group("line")
        frames.append(
            StackFrame(
                file=m.group("file") or "unknown",
                line=int(line) if line else None,
                function=m.group("func"),
                raw=m.group(0).strip(),
            )
        )
    exc_matches = list(_JAVA_EXC_RE.finditer(raw))
    exc_type = exc_matches[0].group("type") if exc_matches else None
    exc_msg = exc_matches[0].group("msg") if exc_matches else None
    top = frames[0] if frames else None
    return ParsedStackTrace(
        language="java",
        exception_type=exc_type,
        exception_message=exc_msg,
        frames=frames,
        top_frame_file=top.file if top else None,
        top_frame_line=top.line if top else None,
    )


def _parse_node(raw: str) -> ParsedStackTrace:
    frames = []
    for m in _NODE_FRAME_RE.finditer(raw):
        line = m.group("line")
        frames.append(
            StackFrame(
                file=m.group("file"),
                line=int(line) if line else None,
                function=(m.group("func") or "").strip() or None,
                raw=m.group(0).strip(),
            )
        )
    exc_matches = list(_NODE_EXC_RE.finditer(raw))
    exc_type = exc_matches[0].group("type") if exc_matches else None
    exc_msg = exc_matches[0].group("msg") if exc_matches else None
    top = frames[0] if frames else None
    return ParsedStackTrace(
        language="node",
        exception_type=exc_type,
        exception_message=exc_msg,
        frames=frames,
        top_frame_file=top.file if top else None,
        top_frame_line=top.line if top else None,
    )


def _parse_go(raw: str) -> ParsedStackTrace:
    frames = [
        StackFrame(file=m.group("file"), line=int(m.group("line")), function=None, raw=m.group(0).strip())
        for m in _GO_FRAME_RE.finditer(raw)
    ]
    panic_matches = list(_GO_PANIC_RE.finditer(raw))
    exc_msg = panic_matches[0].group("msg") if panic_matches else None
    top = frames[0] if frames else None
    return ParsedStackTrace(
        language="go",
        exception_type="panic" if panic_matches else None,
        exception_message=exc_msg,
        frames=frames,
        top_frame_file=top.file if top else None,
        top_frame_line=top.line if top else None,
    )


@mcp.tool()
def parse_stack_trace(raw_log: str) -> dict:
    """Parse a raw stack trace (Python, Java, Node.js, or Go) into structured
    frames + exception info using real regex-based parsing (auto-detects
    language)."""
    lang = _detect_language(raw_log)
    parser = {
        "python": _parse_python,
        "java": _parse_java,
        "node": _parse_node,
        "go": _parse_go,
    }.get(lang)

    if parser is None:
        return ParsedStackTrace(
            language="unknown",
            exception_type=None,
            exception_message=None,
            frames=[],
            top_frame_file=None,
            top_frame_line=None,
        ).model_dump()

    return parser(raw_log).model_dump()


if __name__ == "__main__":
    mcp.run()
