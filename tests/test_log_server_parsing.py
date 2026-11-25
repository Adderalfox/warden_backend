"""
Tests the real parsing logic in mcp_servers/log_server.py directly (pure
functions, no MCP transport, no network) -- this is what was flaky before
when tests exercised a simulated server. Import the module functions
directly and assert on real regex-parsed output.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_servers.log_server import parse_stack_trace  # noqa: E402


PY_TRACE = '''Traceback (most recent call last):
  File "/app/service.py", line 42, in handle_request
    return process(payload)
  File "/app/process.py", line 17, in process
    raise ValueError("bad payload")
ValueError: bad payload'''


def test_parse_python_stack_trace():
    result = parse_stack_trace(PY_TRACE)
    assert result["language"] == "python"
    assert result["exception_type"] == "ValueError"
    assert result["exception_message"] == "bad payload"
    assert result["top_frame_file"] == "/app/process.py"
    assert result["top_frame_line"] == 17
    assert len(result["frames"]) == 2


NODE_TRACE = '''TypeError: Cannot read properties of undefined (reading 'id')
    at getUser (/app/src/users.js:12:5)
    at /app/src/index.js:30:3'''


def test_parse_node_stack_trace():
    result = parse_stack_trace(NODE_TRACE)
    assert result["language"] == "node"
    assert result["exception_type"] == "TypeError"
    assert result["top_frame_file"] == "/app/src/users.js"
    assert result["top_frame_line"] == 12


GO_TRACE = '''panic: runtime error: index out of range [3] with length 3

goroutine 1 [running]:
main.process(...)
	/app/main.go:22
main.main()
	/app/main.go:10 +0x1a'''


def test_parse_go_stack_trace():
    result = parse_stack_trace(GO_TRACE)
    assert result["language"] == "go"
    assert result["exception_type"] == "panic"
    assert "index out of range" in result["exception_message"]
    assert result["top_frame_file"] == "/app/main.go"


def test_parse_unknown_returns_empty_frames():
    result = parse_stack_trace("this is not a stack trace at all")
    assert result["language"] == "unknown"
    assert result["frames"] == []
