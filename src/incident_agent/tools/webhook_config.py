"""`get_webhook_config`: a site's endpoints and the event types each is subscribed to."""

from urllib.parse import urlparse

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from incident_agent.backends.base import WebhookConfigBackend
from incident_agent.tools.common import ToolResult, error, ok

MAX_RETRY_ATTEMPTS = 7

DESCRIPTION = """\
A site's webhook endpoints from the internal config API: destination ID, URL host, and the event
types each endpoint is subscribed to (from event_filters). Secrets and URL paths/queries are
removed. Use it to check whether an endpoint exists and is subscribed to an event type."""


class GetWebhookConfigInput(BaseModel):
    site_id: str = Field(description="Site / tenant ID, e.g. 'acme'.")


def get_webhook_config(backend: WebhookConfigBackend, site_id: str) -> ToolResult:
    endpoints = backend.get_site_webhooks(site_id)
    if endpoints is None:
        return error(f"Site '{site_id}' not found (config API returned 404).")
    payload = {
        "site_id": site_id,
        "endpoint_count": len(endpoints),
        "endpoints": [
            {
                "destination_id": e["destination_id"],
                # Host only: paths and query strings can carry tokens.
                "url_host": urlparse(e["url"]).hostname,
                "event_types": sorted(e.get("event_types", [])),
            }
            for e in endpoints
        ],
        "retry_policy": {"max_attempts": MAX_RETRY_ATTEMPTS},
    }
    if not endpoints:
        return ok(payload, "no webhook endpoints configured")
    summary = " · ".join(
        f"{e['destination_id']} ({len(e['event_types'])} event types)" for e in payload["endpoints"]
    )
    return ok(payload, f"{len(endpoints)} endpoint(s): {summary}")


def make_get_webhook_config_tool(backend: WebhookConfigBackend) -> BaseTool:
    def _run(site_id: str) -> ToolResult:
        return get_webhook_config(backend, site_id)

    return StructuredTool.from_function(
        func=_run,
        name="get_webhook_config",
        description=DESCRIPTION,
        args_schema=GetWebhookConfigInput,
        response_format="content_and_artifact",
    )
