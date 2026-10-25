from datetime import UTC, datetime, timedelta

from incident_agent.config import load_metrics_config, load_topic_mapping
from incident_agent.intake.models import AlarmInfo, InvestigationRequest
from incident_agent.investigation.playbooks import select_playbook

NOW = datetime(2026, 10, 25, 15, 0, tzinfo=UTC)
TOPICS = load_topic_mapping()
METRICS = load_metrics_config()


def _request(issue_type: str, **kw) -> InvestigationRequest:
    return InvestigationRequest(
        source="slack_form", source_ref="C1/1", reply_to="C1/1", issue_type=issue_type,
        site_id="acme", window_start=NOW - timedelta(hours=2), window_end=NOW, **kw,
    )


def _names(checks) -> list[str]:
    return [c.name for c in checks]


def test_failing_checks_failures_logs_service_metrics_and_deploys():
    checks = select_playbook(_request("failing"), TOPICS, METRICS)
    names = _names(checks)
    assert names[:3] == ["webhook_config", "failed_events", "site_logs"]
    assert {"delivery_failures", "delivery_latency", "config_api_errors", "deployments"} <= set(names)
    assert not any(n.startswith("lag_") or n.startswith("event_") for n in names)
    failed = next(c for c in checks if c.name == "failed_events")
    assert failed.args == {"site_id": "acme", "start": "2026-10-25T13:00:00Z",
                           "end": "2026-10-25T15:00:00Z", "source": "both"}


def test_not_received_with_ids_looks_up_events_and_the_event_topic_lag():
    checks = select_playbook(
        _request("not_received", event_type="invoice.paid", event_ids=["evt_8k2lq9"]), TOPICS, METRICS
    )
    names = _names(checks)
    assert {"event_history", "event_state", "lag_invoice-events"} <= set(names)
    assert not any(n.startswith("lag_payment") for n in names)
    lag = next(c for c in checks if c.name == "lag_invoice-events")
    assert lag.args["dimensions"]["Consumer Group"] == "webhook-delivery-cg"


def test_not_received_without_event_type_checks_lag_on_every_domain_topic():
    names = _names(select_playbook(_request("not_received", event_ids=["evt_8k2lq9"]), TOPICS, METRICS))
    lag = sorted(n for n in names if n.startswith("lag_"))
    assert lag == ["lag_customer-events", "lag_invoice-events", "lag_payment-events", "lag_subscription-events"]


def test_alarm_checks_are_service_wide():
    request = InvestigationRequest(
        source="cloudwatch_alarm", source_ref="a", reply_to="#inc", issue_type="alarm", site_id=None,
        window_start=NOW - timedelta(hours=2), window_end=NOW,
        alarm=AlarmInfo(name="a", namespace="AWS/Kafka", metric="EstimatedMaxTimeLag",
                        dimensions={}, threshold=120, state_change_time=NOW),
    )
    names = _names(select_playbook(request, TOPICS, METRICS))
    assert names[0] == "alarm_metric"
    assert "service_errors" in names and "deployments" in names
    assert "failed_events" not in names


def test_deploy_lookback_starts_six_hours_before_the_window():
    checks = select_playbook(_request("failing"), TOPICS, METRICS)
    deploys = next(c for c in checks if c.name == "deployments")
    assert deploys.args["since"] == "2026-10-25T07:00:00Z"
