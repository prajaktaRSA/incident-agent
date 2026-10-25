"""Runs one tool call with the same timing, error handling and audit logging everywhere.

Used by the investigation playbooks and by the chat agent, so a check behaves identically
whether code or the LLM asked for it.
"""

import json
import time
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool

from incident_agent.audit import AuditLog
from incident_agent.redact import redact


def run_tool_call(
    tools_by_name: dict[str, BaseTool],
    call: dict[str, Any],
    audit: AuditLog | None = None,
    *,
    refuse: str | None = None,
) -> ToolMessage:
    """Execute `call` ({"id", "name", "args"}); never raises. `refuse` skips execution with that reason."""
    started = time.perf_counter()
    if refuse is not None:
        message = error_message(call, refuse)
    elif call["name"] not in tools_by_name:
        message = error_message(call, f"Unknown tool '{call['name']}'.")
    else:
        try:
            message = tools_by_name[call["name"]].invoke({**call, "type": "tool_call"})
        except Exception as exc:  # bad arguments from the model, or a backend failure
            message = error_message(call, redact(f"{type(exc).__name__}: {exc}"))

    artifact = dict(message.artifact or {})
    artifact["duration_ms"] = round((time.perf_counter() - started) * 1000)
    message.artifact = artifact
    if audit:
        audit.write(
            "tool_call",
            call_id=call["id"],
            tool=call["name"],
            args=call["args"],
            ok=artifact.get("ok", False),
            summary=artifact.get("summary"),
            duration_ms=artifact["duration_ms"],
        )
    return message


def error_message(call: dict[str, Any], error: str) -> ToolMessage:
    return ToolMessage(
        content=json.dumps({"error": error}),
        tool_call_id=call["id"],
        name=call["name"],
        artifact={"ok": False, "summary": error},
    )
