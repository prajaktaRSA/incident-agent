from langchain_core.tools import BaseTool

from incident_agent.backends.mock import (
    FileRunbooksBackend,
    MockDeploymentsBackend,
    MockFailedEventsBackend,
    MockLogsBackend,
    MockMetricsBackend,
    MockWebhookConfigBackend,
    Scenario,
)
from incident_agent.config import load_known_sites, load_metrics_config
from incident_agent.settings import get_settings
from incident_agent.tools.deployments import make_get_deployments_tool
from incident_agent.tools.events import make_event_history_tool, make_lookup_events_tool
from incident_agent.tools.failed_events import make_query_failed_events_tool
from incident_agent.tools.logs import make_search_logs_tool
from incident_agent.tools.metrics import make_get_metric_tool
from incident_agent.tools.runbooks import make_search_runbooks_tool
from incident_agent.tools.webhook_config import make_get_webhook_config_tool


def build_mock_tools(scenario: Scenario) -> list[BaseTool]:
    settings = get_settings()
    failed = MockFailedEventsBackend(scenario)
    logs = MockLogsBackend(scenario)
    return [
        make_get_webhook_config_tool(MockWebhookConfigBackend(scenario, load_known_sites(settings.config_dir))),
        make_lookup_events_tool(failed),
        make_event_history_tool(logs),
        make_query_failed_events_tool(failed),
        make_search_logs_tool(logs),
        make_get_metric_tool(MockMetricsBackend(scenario), load_metrics_config(settings.config_dir)),
        make_get_deployments_tool(MockDeploymentsBackend(scenario)),
        make_search_runbooks_tool(FileRunbooksBackend(settings.fixtures_dir / "runbooks")),
    ]


__all__ = ["build_mock_tools"]
