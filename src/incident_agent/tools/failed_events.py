"""`query_failed_events`: failed and DLQ deliveries for one site, summarized for the LLM."""

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from incident_agent.backends.base import FailedEventsBackend
from incident_agent.redact import redact
from incident_agent.tools.common import ToolResult, error, iso, ok, to_ms

MAX_WINDOW = timedelta(days=7)
SAMPLE_MESSAGES_PER_STATUS = 5
SAMPLE_EVENT_IDS_PER_STATUS = 3
TOP_N = 10

DESCRIPTION = """\
Failed webhook deliveries for one site (tenant), grouped for investigation.

Reads two tables: `failed` (deliveries still being retried, up to 7 attempts) and `dlq`
(deliveries that exhausted retries, plus event-level CONFIG_FETCH_ERROR rows written when the
webhook service could not load the site's webhook config from the internal API).

Returns totals, counts by HTTP status (each with its destinations and event types), by
destination (endpoint) and by event type, retry attempt distribution, first/last failure
times, sample failure messages and sample event IDs.
Config-fetch failures are reported separately from merchant delivery failures.

Times are ISO-8601 UTC. The window may be at most 7 days."""


class QueryFailedEventsInput(BaseModel):
    site_id: str = Field(description="Site / tenant ID, e.g. 'acme'.")
    start: datetime = Field(description="Window start, ISO-8601 UTC, e.g. 2026-10-25T13:00:00Z.")
    end: datetime = Field(description="Window end, ISO-8601 UTC. Use the current time for 'now'.")
    source: Literal["failed", "dlq", "both"] = Field(
        default="both", description="Which table(s) to read."
    )


def query_failed_events(
    backend: FailedEventsBackend,
    site_id: str,
    start: datetime,
    end: datetime,
    source: Literal["failed", "dlq", "both"] = "both",
) -> ToolResult:
    start_ms, end_ms = to_ms(start), to_ms(end)
    if start_ms >= end_ms:
        return error("start must be before end.")
    if end_ms - start_ms > MAX_WINDOW.total_seconds() * 1000:
        return error("Window is longer than 7 days; narrow it.")
    if end_ms > to_ms(datetime.now(UTC) + timedelta(minutes=5)):
        return error("end is in the future; use the current time for 'now'.")

    failed = (
        backend.query_by_tenant("failed", site_id, start_ms, end_ms) if source != "dlq" else []
    )
    dlq = backend.query_by_tenant("dlq", site_id, start_ms, end_ms) if source != "failed" else []

    payload = summarize(site_id, start_ms, end_ms, source, failed, dlq)
    totals = payload["totals"]
    if totals["failed"] + totals["dlq"] == 0:
        summary = "no failures in window"
    else:
        top = payload["by_status"][0] if payload["by_status"] else None
        summary = f"{totals['failed']} failed · {totals['dlq']} DLQ"
        if top:
            summary += f" · top status {top['status']} ({top['count']})"
        if payload["config_fetch_failures"]["count"]:
            summary += f" · {payload['config_fetch_failures']['count']} config-fetch"
    return ok(payload, summary)


def summarize(
    site_id: str,
    start_ms: int,
    end_ms: int,
    source: str,
    failed: list[dict[str, Any]],
    dlq: list[dict[str, Any]],
) -> dict[str, Any]:
    deliveries = [("failed", i) for i in failed] + [
        ("dlq", i) for i in dlq if i.get("failureType") != "CONFIG_FETCH_ERROR"
    ]
    config_fetch = [i for i in dlq if i.get("failureType") == "CONFIG_FETCH_ERROR"]

    return {
        "site_id": site_id,
        "window": {"start": iso(start_ms), "end": iso(end_ms)},
        "source": source,
        "totals": {"failed": len(failed), "dlq": len(dlq)},
        "by_status": _by_status(deliveries),
        "by_destination": _by_destination(deliveries),
        "by_event_type": [
            {"type": t, "count": c}
            for t, c in Counter(_event_type(i) for _, i in deliveries).most_common(TOP_N)
        ],
        "retry_attempts": dict(sorted(Counter(i["retryAttempts"] for i in failed).items())),
        "dlq_reasons": dict(Counter(i.get("dlqReason", "UNKNOWN") for i in dlq)),
        "config_fetch_failures": {
            "count": len(config_fetch),
            "by_status": dict(Counter(i["failureStatusCode"] for i in config_fetch)),
            "first_seen": _first(config_fetch),
            "last_seen": _last(config_fetch),
            "sample_messages": _sample_messages(config_fetch),
        },
    }


def _by_status(deliveries: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    groups: dict[int, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for table, item in deliveries:
        groups[item["failureStatusCode"]].append((table, item))
    rows = []
    for status, members in groups.items():
        items = [i for _, i in members]
        rows.append(
            {
                "status": status,
                "count": len(members),
                "failed": sum(1 for t, _ in members if t == "failed"),
                "dlq": sum(1 for t, _ in members if t == "dlq"),
                "destinations": dict(Counter(i["destinationId"] for i in items).most_common(TOP_N)),
                "event_types": dict(Counter(_event_type(i) for i in items).most_common(TOP_N)),
                "first_seen": _first(items),
                "last_seen": _last(items),
                "sample_messages": _sample_messages(items),
                "sample_event_ids": [i["eventId"] for i in items[:SAMPLE_EVENT_IDS_PER_STATUS]],
            }
        )
    return sorted(rows, key=lambda r: r["count"], reverse=True)


def _by_destination(deliveries: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for _, item in deliveries:
        groups[item["destinationId"]].append(item)
    rows = [
        {
            "destination_id": dest,
            "count": len(items),
            "statuses": dict(Counter(i["failureStatusCode"] for i in items)),
            "first_seen": _first(items),
            "last_seen": _last(items),
        }
        for dest, items in groups.items()
    ]
    return sorted(rows, key=lambda r: r["count"], reverse=True)[:TOP_N]


def _event_type(item: dict[str, Any]) -> str:
    # Only the event type is read from eventContent; the rest may hold customer or payment data
    # and must never reach the LLM.
    try:
        return json.loads(item["eventContent"]).get("type") or "unknown"
    except (KeyError, TypeError, ValueError):
        return "unknown"


def _time(item: dict[str, Any]) -> int:
    return item.get("failedAt") or item["created_at"]


def _first(items: list[dict[str, Any]]) -> str | None:
    return iso(min(_time(i) for i in items)) if items else None


def _last(items: list[dict[str, Any]]) -> str | None:
    return iso(max(_time(i) for i in items)) if items else None


def _sample_messages(items: list[dict[str, Any]]) -> list[str]:
    messages = Counter(redact(i.get("failureMessage") or "") for i in items)
    return [m for m, _ in messages.most_common(SAMPLE_MESSAGES_PER_STATUS)]


def make_query_failed_events_tool(backend: FailedEventsBackend) -> BaseTool:
    def _run(
        site_id: str,
        start: datetime,
        end: datetime,
        source: Literal["failed", "dlq", "both"] = "both",
    ) -> ToolResult:
        return query_failed_events(backend, site_id, start, end, source)

    return StructuredTool.from_function(
        func=_run,
        name="query_failed_events",
        description=DESCRIPTION,
        args_schema=QueryFailedEventsInput,
        response_format="content_and_artifact",
    )
