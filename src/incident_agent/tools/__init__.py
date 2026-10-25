from langchain_core.tools import BaseTool

from incident_agent.backends.mock import MockFailedEventsBackend, Scenario
from incident_agent.tools.failed_events import make_query_failed_events_tool


def build_mock_tools(scenario: Scenario) -> list[BaseTool]:
    return [make_query_failed_events_tool(MockFailedEventsBackend(scenario))]


__all__ = ["build_mock_tools"]
