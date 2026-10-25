"""Shared helpers for tools: time handling and the (content, artifact) result shape."""

import json
from datetime import UTC, datetime
from typing import Any

# What every tool returns: JSON for the LLM, plus an artifact the terminal chat and the audit
# log read (a one-line summary). Tools use response_format="content_and_artifact".
ToolResult = tuple[str, dict[str, Any]]


def to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def ok(payload: dict[str, Any], summary: str) -> ToolResult:
    return json.dumps(payload, separators=(",", ":")), {"ok": True, "summary": summary}


def error(message: str) -> ToolResult:
    """Errors go back to the model as data so it can correct its inputs and try again."""
    return json.dumps({"error": message}), {"ok": False, "summary": message}
