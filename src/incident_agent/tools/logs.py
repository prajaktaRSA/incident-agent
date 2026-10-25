"""`search_logs`: service logs (Splunk or CloudWatch Logs), summarized before any raw lines."""

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import quantiles
from typing import Literal

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from incident_agent.backends.base import LogsBackend
from incident_agent.redact import redact
from incident_agent.tools.common import ToolResult, error, iso, ok, to_ms, window_error

MAX_WINDOW = timedelta(days=7)

DESCRIPTION = """\
Search the service logs. Sources: `webhook_service` (one line per delivery attempt, successes
included, plus consume/filter/DLQ lines) and `events_service` (producer publish logs and config
API logs). Filter by tenant/site, event type, destination, HTTP status, level or message text.

Returns a summary first (counts by status and level; per destination: attempts, successes,
time_taken p50/p95/max; most common messages), then up to `limit` most recent lines."""


class SearchLogsInput(BaseModel):
    source: Literal["webhook_service", "events_service"]
    start: datetime = Field(description="ISO-8601 UTC.")
    end: datetime = Field(description="ISO-8601 UTC.")
    tenant_id: str | None = Field(default=None, description="Site / tenant ID.")
    event_type: str | None = None
    destination_id: str | None = None
    status: int | None = Field(default=None, description="HTTP status of the delivery attempt.")
    level: Literal["DEBUG", "INFO", "WARN", "ERROR"] | None = None
    text: str | None = Field(default=None, description="Case-insensitive substring of the message.")
    limit: int = Field(default=10, ge=0, le=100, description="Number of raw lines to return.")


def search_logs(
    backend: LogsBackend,
    source: str,
    start: datetime,
    end: datetime,
    tenant_id: str | None = None,
    event_type: str | None = None,
    destination_id: str | None = None,
    status: int | None = None,
    level: str | None = None,
    text: str | None = None,
    limit: int = 10,
) -> ToolResult:
    if problem := window_error(start, end, MAX_WINDOW):
        return error(problem)
    filters = {
        "tenant_id": tenant_id, "event_type": event_type, "destination_id": destination_id,
        "status": status, "level": level, "text": text,
    }
    lines = backend.search(source, to_ms(start), to_ms(end), filters)
    payload = {
        "source": source,
        "window": {"start": iso(to_ms(start)), "end": iso(to_ms(end))},
        "filters": {k: v for k, v in filters.items() if v is not None},
        "total_lines": len(lines),
        "by_status": dict(Counter(str(line["status"]) for line in lines).most_common()),
        "by_level": dict(Counter(line["level"] for line in lines).most_common()),
        "destinations": _destinations(lines),
        "top_messages": [
            {"msg": msg, "count": count}
            for msg, count in Counter(redact(line["msg"]) for line in lines).most_common(5)
        ],
        "lines": [_format(line) for line in lines[-limit:]] if limit else [],
    }
    return ok(payload, _summary(payload))


def _destinations(lines: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for line in lines:
        if line["destination_id"] and line["attempt"] is not None:
            groups[line["destination_id"]].append(line)
    rows = []
    for destination, attempts in groups.items():
        succeeded = sum(1 for a in attempts if a["status"] and 200 <= a["status"] < 300)
        taken = sorted(a["time_taken_ms"] for a in attempts if a["time_taken_ms"] is not None)
        rows.append(
            {
                "destination_id": destination,
                "attempts": len(attempts),
                "succeeded": succeeded,
                "success_rate": round(succeeded / len(attempts), 3),
                "by_status": dict(Counter(str(a["status"]) for a in attempts).most_common()),
                "time_taken_ms": _percentiles(taken),
                "first_at": iso(attempts[0]["time_ms"]),
                "last_at": iso(attempts[-1]["time_ms"]),
            }
        )
    return sorted(rows, key=lambda r: r["attempts"], reverse=True)[:10]


def _percentiles(values: list[int]) -> dict[str, int] | None:
    if not values:
        return None
    if len(values) == 1:
        return {"p50": values[0], "p95": values[0], "max": values[0]}
    cuts = quantiles(values, n=20, method="inclusive")
    return {"p50": round(cuts[9]), "p95": round(cuts[18]), "max": values[-1]}


def _format(line: dict) -> str:
    parts = [f"{iso(line['time_ms'])} {line['level']} {line['service']}"]
    for key, label in (("tenant_id", "tenant"), ("event_id", "event"), ("event_type", "type"),
                       ("destination_id", "dest"), ("request_id", "req"), ("attempt", "attempt"),
                       ("status", "status")):
        if line.get(key) is not None:
            parts.append(f"{label}={line[key]}")
    if line.get("time_taken_ms") is not None:
        parts.append(f"took={line['time_taken_ms']}ms")
    parts.append(f'msg="{redact(line["msg"])}"')
    return " ".join(parts)


def _summary(payload: dict) -> str:
    if not payload["total_lines"]:
        return "no matching log lines"
    statuses = ", ".join(f"{s}×{c}" for s, c in list(payload["by_status"].items())[:3])
    text = f"{payload['total_lines']} lines ({statuses})"
    if payload["destinations"]:
        top = payload["destinations"][0]
        p95 = (top["time_taken_ms"] or {}).get("p95")
        text += f" · {top['destination_id']} success {top['success_rate']:.0%}, p95 {p95}ms"
    return text


def make_search_logs_tool(backend: LogsBackend) -> BaseTool:
    def _run(**kwargs) -> ToolResult:
        return search_logs(backend, **kwargs)

    return StructuredTool.from_function(
        func=_run, name="search_logs", description=DESCRIPTION,
        args_schema=SearchLogsInput, response_format="content_and_artifact",
    )
