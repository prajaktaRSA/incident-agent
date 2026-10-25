"""Backend interfaces. Tools depend on these; mock and (later) real implementations provide them.

Backends only fetch data. Validation, summarizing and redaction happen in the tools.
"""

from typing import Any, Literal, Protocol

from incident_agent.config import MetricDef

FailedEventsTable = Literal["failed", "dlq"]


class FailedEventsBackend(Protocol):
    def query_by_tenant(
        self, table: FailedEventsTable, tenant_id: str, start_ms: int, end_ms: int
    ) -> list[dict[str, Any]]:
        """Raw items for one tenant in [start_ms, end_ms).

        Mirrors a Query on the `tenant_id` index: `failedAt` for the failed table,
        `created_at` for the DLQ table.
        """
        ...

    def get_by_event_id(self, table: FailedEventsTable, event_id: str) -> list[dict[str, Any]]:
        """All items for one event (one per destination). A Query on the partition key."""
        ...


class WebhookConfigBackend(Protocol):
    def get_site_webhooks(self, site_id: str) -> list[dict[str, Any]] | None:
        """The site's endpoints with their subscribed event types; None if the site doesn't exist.

        Real: GET /internal/sites/{site_id}/webhooks on the internal config API.
        """
        ...


class LogsBackend(Protocol):
    def search(
        self, source: str, start_ms: int, end_ms: int, filters: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Log lines from a logical source (config/logs.yaml) in [start_ms, end_ms), oldest first.

        `filters` keys: tenant_id, event_ids, event_type, destination_id, status, level, text.
        Real: a Splunk search or a CloudWatch Logs Insights query built from the filters.
        """
        ...


class MetricsBackend(Protocol):
    def get_series(
        self, metric: MetricDef, dimensions: dict[str, str], start_ms: int, end_ms: int, period_s: int
    ) -> list[tuple[int, float]]:
        """(timestamp_ms, value) points. Real: CloudWatch GetMetricData."""
        ...

    def get_alarms(
        self, metric: MetricDef, dimensions: dict[str, str], start_ms: int, end_ms: int
    ) -> list[dict[str, Any]]:
        """Alarms on the metric: name, level, threshold, and state changes in the window.

        Real: DescribeAlarmsForMetric + DescribeAlarmHistory.
        """
        ...


class DeploymentsBackend(Protocol):
    def list_deployments(self, services: list[str], since_ms: int, until_ms: int) -> list[dict[str, Any]]:
        """Deploy workflow runs on main for the services, newest first.

        Real: GitHub Actions runs for deploy-ecs.yml, joined with ECS DescribeServices.
        """
        ...


class RunbooksBackend(Protocol):
    def documents(self) -> list[dict[str, str]]:
        """Runbook markdown files: repo, path, text, url."""
        ...
