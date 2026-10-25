"""In-memory stand-in for the `webhook-failed-events` and `webhook-dlq-events` tables."""

from typing import Any

from incident_agent.backends.base import FailedEventsTable
from incident_agent.backends.mock.scenario import Scenario

_TIME_FIELD: dict[FailedEventsTable, str] = {"failed": "failedAt", "dlq": "created_at"}


class MockFailedEventsBackend:
    def __init__(self, scenario: Scenario) -> None:
        self._tables: dict[FailedEventsTable, list[dict[str, Any]]] = {
            "failed": scenario.failed_events,
            "dlq": scenario.dlq_events,
        }

    def query_by_tenant(
        self, table: FailedEventsTable, tenant_id: str, start_ms: int, end_ms: int
    ) -> list[dict[str, Any]]:
        time_field = _TIME_FIELD[table]
        return [
            item
            for item in self._tables[table]
            if item["tenant_id"] == tenant_id and start_ms <= item[time_field] < end_ms
        ]
