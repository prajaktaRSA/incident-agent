"""Which checks run for which trigger. Plain code: the LLM doesn't choose the first steps."""

from datetime import datetime, timedelta

from incident_agent.config import MetricsConfig, Topic, TopicMapping
from incident_agent.intake.models import InvestigationRequest
from incident_agent.investigation.models import Category, Check

DEPLOY_LOOKBACK = timedelta(hours=6)
SERVICES = ["events-service", "webhook-service"]
INTERNAL_TOPICS = ("webhook-retry-events", "webhook-ratelimited-events")

# Keywords for the runbook search that runs after triage, per top hypothesis.
RUNBOOK_QUERIES: dict[Category, str] = {
    Category.NO_ENDPOINT: "webhook endpoint not configured site setup",
    Category.NOT_SUBSCRIBED: "event not received subscription event filters event type",
    Category.DELIVERED_OK: "event not received delivered merchant request id confirm",
    Category.STILL_RETRYING: "retry schedule retries pending event not received",
    Category.NOT_CONSUMED: "event not received consumer lag not consumed kafka",
    Category.MERCHANT_ENDPOINT_DOWN: "merchant endpoint 5xx 503 timeout outage retries dlq replay",
    Category.MERCHANT_REJECTING: "merchant endpoint 401 403 404 410 rejected authentication url",
    Category.MERCHANT_RATE_LIMITING: "merchant rate limiting 429 too many requests",
    Category.CONFIG_FETCH_FAILURE: "webhook config api errors 401 config fetch failure",
    Category.BAD_DEPLOY: "bad deploy rollback webhook service",
    Category.CONSUMER_LAG: "consumer lag kafka time lag",
    Category.TRAFFIC_BURST: "consumer lag traffic burst offset lag",
    Category.UNDETERMINED: "webhook delivery troubleshooting",
}


def select_playbook(
    request: InvestigationRequest, topics: TopicMapping, metrics: MetricsConfig
) -> list[Check]:
    window = {"start": _iso(request.window_start), "end": _iso(request.window_end)}
    checks: list[Check] = []

    if request.issue_type in ("not_received", "failing", "delayed"):
        checks += _site_checks(request, topics, window)
    elif request.issue_type == "alarm":
        checks += _alarm_checks(request, topics, metrics, window)

    checks.append(
        Check(
            name="deployments",
            tool="get_recent_deployments",
            args={"services": SERVICES, "since": _iso(request.window_start - DEPLOY_LOOKBACK)},
            purpose="Deploys of events-service and webhook-service before and during the window",
        )
    )
    return _dedupe(checks)


def runbook_check(top: Category, request: InvestigationRequest) -> Check:
    return Check(
        name="runbooks",
        tool="search_runbooks",
        args={"query": RUNBOOK_QUERIES[top], "top_k": 3},
        purpose=f"Runbook sections for {top}",
    )


def _site_checks(request: InvestigationRequest, topics: TopicMapping, window: dict) -> list[Check]:
    site = request.site_id
    assert site is not None
    checks = [
        Check(
            name="webhook_config",
            tool="get_webhook_config",
            args={"site_id": site},
            purpose="Site's webhook endpoints and their event subscriptions",
        )
    ]
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
    checks += [
        Check(
            name="failed_events",
            tool="query_failed_events",
            args={"site_id": site, **window, "source": "both"},
            purpose="Failed and DLQ deliveries for the site in the window",
        ),
        Check(
            name="site_logs",
            tool="search_logs",
            args=_drop_none(
                {
                    "source": "webhook_service",
                    **window,
                    "tenant_id": site,
                    "event_type": request.event_type,
                    "destination_id": request.destination_id,
                }
            ),
            purpose="Delivery attempts for the site: status mix and time taken per endpoint",
        ),
    ]
    if request.issue_type in ("not_received", "delayed"):
        domain = topics.topic_for(request.event_type) if request.event_type else None
        targets = [domain] if domain else [t for t in topics.topics if t.event_types]
        if request.issue_type == "delayed":
            targets += [t for t in topics.topics if t.name in INTERNAL_TOPICS]
        checks += [_lag_check(t, topics.cluster, window) for t in targets]
    if request.issue_type in ("failing", "delayed"):
        checks += _service_metric_checks(window)
    checks += _config_checks(window)
    return checks


def _alarm_checks(
    request: InvestigationRequest, topics: TopicMapping, metrics: MetricsConfig, window: dict
) -> list[Check]:
    alarm = request.alarm
    assert alarm is not None
    definition = metrics.find(alarm.namespace, alarm.metric)
    key = definition.key if definition else f"{alarm.namespace}/{alarm.metric}"
    checks = [
        Check(
            name="alarm_metric",
            tool="get_cloudwatch_metric",
            args={"metric": key, "dimensions": alarm.dimensions, **window},
            purpose=f"The alarm's metric ({alarm.metric}) around the alarm time",
        )
    ]
    if key.startswith("kafka."):
        # Offset lag vs time lag tells a burst (consumers keeping up) from stuck consumers.
        for companion, name in (("kafka.time_lag", "kafka_time_lag"), ("kafka.max_offset_lag", "kafka_offset_lag")):
            if companion != key:
                checks.append(
                    Check(
                        name=name,
                        tool="get_cloudwatch_metric",
                        args={"metric": companion, "dimensions": alarm.dimensions, **window},
                        purpose=f"{companion} for the same consumer group and topic",
                    )
                )
    else:
        checks += [_lag_check(t, topics.cluster, window) for t in topics.topics if t.event_types]
    checks += [
        Check(
            name="delivery_attempts",
            tool="get_cloudwatch_metric",
            args={"metric": "webhook.delivery_attempts", **window},
            purpose="Delivery volume (traffic)",
        ),
        *_service_metric_checks(window),
        *_config_checks(window),
        Check(
            name="service_errors",
            tool="search_logs",
            args={"source": "webhook_service", **window, "level": "ERROR"},
            purpose="Service-wide delivery errors in the logs",
        ),
    ]
    return checks


def _lag_check(topic: Topic, cluster: str, window: dict) -> Check:
    return Check(
        name=f"lag_{topic.name}",
        tool="get_cloudwatch_metric",
        args={
            "metric": "kafka.time_lag",
            "dimensions": {
                "Cluster Name": cluster,
                "Consumer Group": topic.consumer_group,
                "Topic": topic.name,
            },
            **window,
        },
        purpose=f"Consumer time lag on {topic.name} ({topic.consumer_group})",
    )


def _service_metric_checks(window: dict) -> list[Check]:
    return [
        Check(
            name="delivery_failures",
            tool="get_cloudwatch_metric",
            args={"metric": "webhook.delivery_failures", **window},
            purpose="Service-wide delivery failures (is it only this site?)",
        ),
        Check(
            name="delivery_latency",
            tool="get_cloudwatch_metric",
            args={"metric": "webhook.delivery_latency", **window},
            purpose="Service-wide delivery latency",
        ),
    ]


def _config_checks(window: dict) -> list[Check]:
    return [
        Check(
            name="config_api_errors",
            tool="get_cloudwatch_metric",
            args={"metric": "config_api.errors", **window},
            purpose="Internal webhook-config API 5xx errors",
        ),
        Check(
            name="config_fetch_errors",
            tool="get_cloudwatch_metric",
            args={"metric": "webhook.config_fetch_errors", **window},
            purpose="webhook-service failures loading webhook config (any status, e.g. 401)",
        ),
    ]


def _dedupe(checks: list[Check]) -> list[Check]:
    seen, unique = set(), []
    for check in checks:
        key = (check.tool, repr(sorted(check.args.items())))
        if check.name not in seen and key not in seen:
            seen |= {check.name, key}
            unique.append(check)
    return unique


def _drop_none(args: dict) -> dict:
    return {k: v for k, v in args.items() if v is not None}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
