"""Shared helpers for tools: time handling and the (content, artifact) result shape."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

# What every tool returns: JSON for the LLM, plus an artifact the terminal and the audit log
# read (a one-line summary). Tools use response_format="content_and_artifact".
ToolResult = tuple[str, dict[str, Any]]

FUTURE_TOLERANCE = timedelta(minutes=5)


def to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def hhmm(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%H:%M")


def window_error(start: datetime, end: datetime, max_window: timedelta) -> str | None:
    """Why a time window is invalid, or None if it's fine."""
    start_ms, end_ms = to_ms(start), to_ms(end)
    if start_ms >= end_ms:
        return "start must be before end."
    if end_ms - start_ms > max_window.total_seconds() * 1000:
        return f"Window is longer than {_human(max_window)}; narrow it."
    if end_ms > to_ms(datetime.now(UTC) + FUTURE_TOLERANCE):
        return "end is in the future; use the current time for 'now'."
    return None


def ok(payload: dict[str, Any], summary: str) -> ToolResult:
    return json.dumps(payload, separators=(",", ":")), {"ok": True, "summary": summary}


def error(message: str) -> ToolResult:
    """Errors go back to the caller as data so the model can correct its inputs and try again."""
    return json.dumps({"error": message}), {"ok": False, "summary": message}


def _human(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    return f"{int(hours // 24)} days" if hours >= 48 else f"{int(hours)} hours"
