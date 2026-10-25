"""The M2 tools against scenario data."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from conftest import FIXTURES_DIR
from incident_agent.backends.mock import (
    FileRunbooksBackend,
    MockDeploymentsBackend,
    MockFailedEventsBackend,
    MockLogsBackend,
    MockMetricsBackend,
    MockWebhookConfigBackend,
    load_scenario,
)
from incident_agent.config import load_metrics_config
from incident_agent.tools.deployments import get_recent_deployments
from incident_agent.tools.events import get_event_delivery_history, lookup_events
from incident_agent.tools.logs import make_search_logs_tool, search_logs
from incident_agent.tools.metrics import get_cloudwatch_metric
from incident_agent.tools.runbooks import RunbookIndex, search_runbooks
from incident_agent.tools.webhook_config import get_webhook_config

SITES = frozenset({"acme", "globex", "initech", "umbrella"})


def _scenario(name: str):
    return load_scenario(FIXTURES_DIR, name)


def _window(minutes: int = 120) -> tuple[datetime, datetime]:
    end = datetime.now(UTC)
    return end - timedelta(minutes=minutes), end


def _parse(result):
    content, artifact = result
    return json.loads(content), artifact


# --- get_webhook_config ---


def test_config_returns_hosts_and_subscriptions_without_secrets():
    data, artifact = _parse(get_webhook_config(MockWebhookConfigBackend(_scenario("merchant_down"), SITES), "acme"))
    assert artifact["ok"]
    endpoints = {e["destination_id"]: e for e in data["endpoints"]}
    assert endpoints["wg_3532"]["url_host"] == "hooks.acme.com"
    assert "invoice.paid" in endpoints["wg_3532"]["event_types"]
    assert "acme_live" not in json.dumps(data)  # token in the URL query never leaves the tool


def test_config_unknown_site_and_site_without_endpoints():
    backend = MockWebhookConfigBackend(_scenario("merchant_down"), SITES)
    _, missing = _parse(get_webhook_config(backend, "nobody"))
    assert not missing["ok"] and "404" in missing["summary"]
    data, empty = _parse(get_webhook_config(backend, "umbrella"))
    assert data["endpoints"] == [] and empty["summary"] == "no webhook endpoints configured"


# --- lookup_events / get_event_delivery_history ---


def test_lookup_finds_retrying_event_and_hides_payload():
    data, artifact = _parse(lookup_events(MockFailedEventsBackend(_scenario("still_retrying")), ["evt_rtry4x9q2m", "evt_unknown1"]))
    assert artifact["summary"] == "evt_rtry4x9q2m: retrying · evt_unknown1: not_found"
    row = data["events"][0]["rows"][0]
    assert (row["retry_attempts"], row["status"], row["event_type"]) == (3, 503, "payment.succeeded")
    assert "eventContent" not in json.dumps(data) and "jane.doe" not in json.dumps(data)


@pytest.mark.parametrize(
    ("scenario", "event_id", "outcome", "attempts"),
    [
        ("delivered_ok", "evt_dlvrd8k2q7", "delivered", 2),
        ("still_retrying", "evt_rtry4x9q2m", "retrying", 3),
        ("not_consumed", "evt_nc0nsm3d7x", "no_logs", 0),
    ],
)
def test_event_history_outcomes(scenario, event_id, outcome, attempts):
    data, _ = _parse(get_event_delivery_history(MockLogsBackend(_scenario(scenario)), [event_id], "acme"))
    event = data["events"][0]
    assert (event["outcome"], event["attempt_count"]) == (outcome, attempts)
    assert event["published_at"] is not None


def test_delivered_event_includes_request_id():
    data, artifact = _parse(get_event_delivery_history(MockLogsBackend(_scenario("delivered_ok")), ["evt_dlvrd8k2q7"]))
    delivered = data["events"][0]["delivered"]
    assert delivered["status"] == 200 and delivered["attempt"] == 2
    assert delivered["request_id"].startswith("req_")
    assert delivered["request_id"] in artifact["summary"]


# --- search_logs ---


def test_logs_summarize_attempts_per_destination():
    data, artifact = _parse(search_logs(MockLogsBackend(_scenario("merchant_down")), "webhook_service", *_window(), tenant_id="acme"))
    destinations = {d["destination_id"]: d for d in data["destinations"]}
    assert destinations["wg_3532"]["success_rate"] < 0.2
    assert destinations["wg_7710"]["success_rate"] == 1.0
    assert destinations["wg_3532"]["time_taken_ms"]["max"] == 10000
    assert len(data["lines"]) == 10
    assert artifact["ok"]


def test_logs_filters_and_limit():
    data, _ = _parse(search_logs(MockLogsBackend(_scenario("merchant_down")), "webhook_service", *_window(),
                                 tenant_id="acme", status=504, limit=3))
    assert set(data["by_status"]) == {"504"}
    assert len(data["lines"]) == 3
    assert "took=10000ms" in data["lines"][0]


def test_logs_reject_unknown_source_through_the_tool():
    tool = make_search_logs_tool(MockLogsBackend(_scenario("merchant_down")))
    start, end = _window()
    with pytest.raises(Exception):
        tool.invoke({"source": "nginx", "start": start.isoformat(), "end": end.isoformat()})


# --- get_cloudwatch_metric ---


def _metric(scenario: str, metric: str, dimensions=None, minutes: int = 120):
    backend = MockMetricsBackend(_scenario(scenario))
    return _parse(get_cloudwatch_metric(backend, load_metrics_config(), metric, *_window(minutes), dimensions))


def test_metric_spike_status_and_alarm():
    data, artifact = _metric("bad_deploy", "webhook.delivery_failures")
    assert data["status"] == "critical"
    assert data["baseline"] < 30
    assert len(data["spikes"]) == 1
    assert len(data["points"]) <= 30
    critical = next(a for a in data["alarms"] if a["level"] == "critical")
    assert critical["state_changes"][0]["state"] == "ALARM"
    assert "critical" in artifact["summary"]


def test_metric_normal_when_healthy():
    data, _ = _metric("merchant_down", "webhook.delivery_failures")
    assert data["status"] == "normal" and data["spikes"] == []


def test_metric_dimensions_select_the_series():
    dims = {"Cluster Name": "events-msk-stg", "Consumer Group": "webhook-delivery-cg"}
    lagging, _ = _metric("not_consumed", "kafka.time_lag", dims | {"Topic": "payment-events"})
    healthy, _ = _metric("not_consumed", "kafka.time_lag", dims | {"Topic": "invoice-events"})
    assert lagging["status"] == "critical" and healthy["status"] == "normal"


@pytest.mark.parametrize(
    ("metric", "dimensions", "message"),
    [("kafka.lag", None, "not in the allowlist"), ("kafka.time_lag", {"Partition": "1"}, "Unknown dimension")],
)
def test_metric_rejects_bad_input(metric, dimensions, message):
    data, artifact = _metric("merchant_down", metric, dimensions)
    assert not artifact["ok"] and message in data["error"]


# --- get_recent_deployments ---


def test_deployments_in_window_with_pr():
    since = datetime.now(UTC) - timedelta(hours=8)
    data, artifact = _parse(get_recent_deployments(MockDeploymentsBackend(_scenario("bad_deploy")), ["webhook-service"], since))
    (deploy,) = data["deployments"]
    assert deploy["pr"]["number"] == 482
    assert deploy["pr"]["url"] == "https://github.com/example-org/webhook-service/pull/482"
    assert deploy["ecs_rollout"]["state"] == "COMPLETED"
    assert "PR #482" in artifact["summary"]


def test_no_recent_deployments():
    since = datetime.now(UTC) - timedelta(hours=8)
    data, artifact = _parse(get_recent_deployments(MockDeploymentsBackend(_scenario("merchant_down")),
                                                   ["events-service", "webhook-service"], since))
    assert data["deployments"] == [] and artifact["summary"].startswith("no deploys since")


# --- search_runbooks ---


@pytest.fixture(scope="module")
def runbooks() -> RunbookIndex:
    return RunbookIndex(FileRunbooksBackend(FIXTURES_DIR / "runbooks"))


@pytest.mark.parametrize(
    ("query", "path"),
    [
        ("merchant rate limiting 429 too many requests", "docs/merchant-rate-limiting.md"),
        ("webhook config api errors 401 config fetch failure", "docs/webhook-config-api-errors.md"),
        ("bad deploy rollback webhook service", "docs/rollback-a-bad-deploy.md"),
    ],
)
def test_runbook_search_finds_the_right_document(runbooks, query, path):
    data, _ = _parse(search_runbooks(runbooks, query))
    assert data["results"][0]["path"] == path
    assert data["results"][0]["url"].startswith("https://github.com/example-org/")


def test_runbooks_split_both_heading_styles(runbooks):
    lag = [s for s in runbooks.sections if s["path"] == "docs/consumer-lag.md"]
    assert lag[0]["heading"] == "Consumer lag on webhook-delivery-cg"  # "===" underline style
    assert "Offset lag high but time lag low" in [s["heading"] for s in lag]
