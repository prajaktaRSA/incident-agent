"""Backend interfaces. Tools depend on these; mock and (later) real implementations provide them."""

from typing import Any, Literal, Protocol

FailedEventsTable = Literal["failed", "dlq"]


class FailedEventsBackend(Protocol):
    def query_by_tenant(
        self, table: FailedEventsTable, tenant_id: str, start_ms: int, end_ms: int
    ) -> list[dict[str, Any]]:
        """Return raw items for one tenant in [start_ms, end_ms).

        Mirrors a Query on the `tenant_id` index: `failedAt` for the failed table,
        `created_at` for the DLQ table.
        """
        ...
