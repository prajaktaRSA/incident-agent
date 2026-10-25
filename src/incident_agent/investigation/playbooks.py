"""Which checks run for which trigger. Plain code: the LLM doesn't choose the first steps.

Playbooks list every check from the plan, including tools that don't exist yet; the gather step
records those as skipped, and they show up as unknowns in the RCA.
"""

from datetime import datetime, timedelta

from incident_agent.config import TopicMapping
from incident_agent.intake.models import InvestigationRequest
from incident_agent.investigation.models import Check

DEPLOY_LOOKBACK = timedelta(hours=6)
SERVICES = ["events-service", "webhook-service"]


def select_playbook(request: InvestigationRequest, topics: TopicMapping) -> list[Check]:
    window = {"start": _iso(request.window_start), "end": _iso(request.window_end)}
    site = request.site_id
    checks: list[Check] = []

    if request.issue_type in ("not_received", "failing", "delayed"):
        assert site is not None
        checks.append(
            Check(
                name="webhook_config",
                tool="get_webhook_config",
                args={"site_id": site},
                purpose="Site's webhook endpoints and their event subscriptions",
            )
        )
        if request.event_ids:
            checks.append(
                Check(
                    name="event_history",
                    tool="get_event_delivery_history",
                    args={"event_ids": request.event_ids, "site_id": site},
                    purpose="Every delivery attempt for the reported events (logs)",
                )
            )
            if request.issue_type != "delayed":
                checks.append(
                    Check(
                        name="event_state",
                        tool="lookup_events",
                        args={"event_ids": request.event_ids, "site_id": site},
                        purpose="Current state of the reported events in the failed/DLQ tables",
                    )
                )
        checks.append(
            Check(
                name="failed_events",
                tool="query_failed_events",
                args={"site_id": site, **window, "source": "both"},
                purpose="Failed and DLQ deliveries for the site in the window",
            )
        )
        checks.append(
            Check(
                name="site_logs",
                tool="search_logs",
                args={
                    "source": "webhook_service",
                    **window,
                    "tenant_id": site,
                    "event_type": request.event_type,
                    "destination_id": request.destination_id,
                },
                purpose="Delivery attempts for the site: status mix and time taken per endpoint",
            )
        )
        if request.issue_type in ("not_received", "delayed"):
            checks.extend(_lag_checks(request, topics, window))
        if request.issue_type in ("failing", "delayed"):
            checks.extend(_service_metric_checks(window))
        checks.extend(_config_api_checks(window))

    elif request.issue_type == "alarm":
        assert request.alarm is not None
        checks.append(
            Check(
                name="alarm_metric",
                tool="get_cloudwatch_metric",
                args={
                    "namespace": request.alarm.namespace,
                    "metric_name": request.alarm.metric,
                    "dimensions": request.alarm.dimensions,
                    **window,
                },
                purpose=f"The alarm's metric ({request.alarm.metric}) around the alarm time",
            )
        )
        checks.extend(_service_metric_checks(window))
        checks.extend(_config_api_checks(window))
        checks.append(
            Check(
                name="service_errors",
                tool="search_logs",
                args={"source": "webhook_service", **window, "level": "ERROR"},
                purpose="Service-wide delivery errors in the logs",
            )
        )

    checks.append(
        Check(
            name="deployments",
            tool="get_recent_deployments",
            args={"services": SERVICES, "since": _iso(request.window_start - DEPLOY_LOOKBACK)},
            purpose="Deploys of events-service and webhook-service before and during the window",
        )
    )
    return checks


def _lag_checks(request: InvestigationRequest, topics: TopicMapping, window: dict) -> list[Check]:
    topic = topics.topic_for(request.event_type) if request.event_type else None
    targets = [topic] if topic else [t for t in topics.topics if t.event_types]
    return [
        Check(
            name=f"lag_{t.name}",
            tool="get_cloudwatch_metric",
            args={
                "metric": "kafka.time_lag",
                "dimensions": {
                    "Cluster Name": topics.cluster,
                    "Consumer Group": t.consumer_group,
                    "Topic": t.name,
                },
                **window,
            },
            purpose=f"Consumer time lag on {t.name}",
        )
        for t in targets
    ]


def _service_metric_checks(window: dict) -> list[Check]:
    return [
        Check(
            name="delivery_failures",
            tool="get_cloudwatch_metric",
            args={"metric": "webhook.delivery_failures", **window},
            purpose="Service-wide delivery failure rate (is it only this site?)",
        ),
        Check(
            name="delivery_latency",
            tool="get_cloudwatch_metric",
            args={"metric": "webhook.delivery_latency", **window},
            purpose="Service-wide delivery latency",
        ),
    ]


def _config_api_checks(window: dict) -> list[Check]:
    return [
        Check(
            name="config_api_errors",
            tool="get_cloudwatch_metric",
            args={"metric": "config_api.errors", **window},
            purpose="Internal webhook-config API error rate",
        )
    ]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
