"""Tools for specific events: current state in the tables, and full delivery history from logs."""

import json
from datetime import UTC, datetime, timedelta

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from incident_agent.backends.base import FailedEventsBackend, LogsBackend
from incident_agent.redact import redact
from incident_agent.tools.common import ToolResult, iso, ok, to_ms

MAX_EVENT_IDS = 20


class EventIdsInput(BaseModel):
    event_ids: list[str] = Field(max_length=MAX_EVENT_IDS, description="Event IDs, e.g. ['evt_8k2lq9'].")
    site_id: str | None = Field(default=None, description="Site / tenant ID, if known.")


class EventHistoryInput(EventIdsInput):
    lookback_hours: int = Field(default=72, ge=1, le=168, description="How far back to search the logs.")


LOOKUP_DESCRIPTION = """\
Current state of specific events in the failed and DLQ tables (exact key lookups by event ID):
`retrying` (in the failed table, retries left), `dlq` (moved to the DLQ), or `not_found` (in
neither table: delivered, filtered, or never processed). Never returns event payloads."""

HISTORY_DESCRIPTION = """\
Full delivery history of specific events from the service logs: whether the producer published
it, then every delivery attempt in order (time, attempt number, destination, request_id, status,
time taken, message). Outcome per event: delivered / retrying / dlq / filtered / no_logs
(no_logs means webhook-service never processed the event)."""


def lookup_events(backend: FailedEventsBackend, event_ids: list[str], site_id: str | None = None) -> ToolResult:
    events = []
    for event_id in event_ids:
        failed = backend.get_by_event_id("failed", event_id)
        dlq = backend.get_by_event_id("dlq", event_id)
        if site_id:
            failed = [i for i in failed if i["tenant_id"] == site_id]
            dlq = [i for i in dlq if i["tenant_id"] == site_id]
        state = "dlq" if dlq else "retrying" if failed else "not_found"
        events.append(
            {
                "event_id": event_id,
                "state": state,
                "rows": [_row(i, "failed") for i in failed] + [_row(i, "dlq") for i in dlq],
            }
        )
    summary = " · ".join(f"{e['event_id']}: {e['state']}" for e in events)
    return ok({"events": events}, summary)


def _row(item: dict, table: str) -> dict:
    return {
        "table": table,
        "destination_id": item["destinationId"],
        "event_type": _event_type(item),
        "status": item["failureStatusCode"],
        "message": redact(item.get("failureMessage") or ""),
        "retry_attempts": item["retryAttempts"],
        "failure_type": item.get("failureType"),
        "dlq_reason": item.get("dlqReason"),
        "last_failed_at": iso(item.get("failedAt") or item["updatedAt"]),
    }


def _event_type(item: dict) -> str:
    try:
        return json.loads(item["eventContent"]).get("type") or "unknown"
    except (KeyError, TypeError, ValueError):
        return "unknown"


def get_event_delivery_history(
    backend: LogsBackend, event_ids: list[str], site_id: str | None = None, lookback_hours: int = 72
) -> ToolResult:
    now = datetime.now(UTC)
    start_ms, end_ms = to_ms(now - timedelta(hours=lookback_hours)), to_ms(now)
    filters = {"event_ids": event_ids, "tenant_id": site_id}
    delivery = backend.search("webhook_service", start_ms, end_ms, filters)
    published = backend.search("events_service", start_ms, end_ms, filters)

    events = []
    for event_id in event_ids:
        lines = [line for line in delivery if line["event_id"] == event_id]
        attempts = [line for line in lines if line["attempt"] is not None]
        publish = next((line for line in published if line["event_id"] == event_id), None)
        delivered = next((a for a in attempts if a["status"] and 200 <= a["status"] < 300), None)
        if not lines:
            outcome = "no_logs"
        elif delivered:
            outcome = "delivered"
        elif any("DLQ" in line["msg"] for line in lines):
            outcome = "dlq"
        elif any("filtered" in line["msg"] for line in lines):
            outcome = "filtered"
        else:
            outcome = "retrying"
        event_type = next((line["event_type"] for line in lines + ([publish] if publish else [])), None)
        events.append(
            {
                "event_id": event_id,
                "event_type": event_type,
                "published_at": iso(publish["time_ms"]) if publish else None,
                "published_msg": publish["msg"] if publish else None,
                "outcome": outcome,
                "attempt_count": len(attempts),
                "delivered": {
                    "at": iso(delivered["time_ms"]),
                    "attempt": delivered["attempt"],
                    "request_id": delivered["request_id"],
                    "status": delivered["status"],
                    "destination_id": delivered["destination_id"],
                } if delivered else None,
                "attempts": [
                    {
                        "at": iso(a["time_ms"]),
                        "attempt": a["attempt"],
                        "destination_id": a["destination_id"],
                        "request_id": a["request_id"],
                        "status": a["status"],
                        "time_taken_ms": a["time_taken_ms"],
                        "msg": redact(a["msg"]),
                    }
                    for a in attempts
                ],
                "other_lines": [redact(line["msg"]) for line in lines if line["attempt"] is None][:5],
            }
        )
    summary = " · ".join(_event_summary(e) for e in events)
    return ok({"lookback_hours": lookback_hours, "events": events}, summary)


def _event_summary(event: dict) -> str:
    if event["outcome"] == "delivered":
        d = event["delivered"]
        return f"{event['event_id']}: delivered on attempt {d['attempt']} ({d['request_id']}, {d['status']})"
    if event["outcome"] == "no_logs":
        published = "published" if event["published_at"] else "no publish log either"
        return f"{event['event_id']}: no delivery logs ({published})"
    return f"{event['event_id']}: {event['outcome']} after {event['attempt_count']} attempt(s)"


def make_lookup_events_tool(backend: FailedEventsBackend) -> BaseTool:
    def _run(event_ids: list[str], site_id: str | None = None) -> ToolResult:
        return lookup_events(backend, event_ids, site_id)

    return StructuredTool.from_function(
        func=_run, name="lookup_events", description=LOOKUP_DESCRIPTION,
        args_schema=EventIdsInput, response_format="content_and_artifact",
    )


def make_event_history_tool(backend: LogsBackend) -> BaseTool:
    def _run(event_ids: list[str], site_id: str | None = None, lookback_hours: int = 72) -> ToolResult:
        return get_event_delivery_history(backend, event_ids, site_id, lookback_hours)

    return StructuredTool.from_function(
        func=_run, name="get_event_delivery_history", description=HISTORY_DESCRIPTION,
        args_schema=EventHistoryInput, response_format="content_and_artifact",
    )
