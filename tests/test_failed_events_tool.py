import json
from datetime import UTC, datetime, timedelta

import pytest

from conftest import MINUTE_MS
from incident_agent.backends.mock import MockFailedEventsBackend, Scenario
from incident_agent.tools.failed_events import make_query_failed_events_tool, query_failed_events


def _window(minutes: int) -> tuple[datetime, datetime]:
    end = datetime.now(UTC)
    return end - timedelta(minutes=minutes), end


def _run(scenario: Scenario, minutes: int = 120, source: str = "both", site_id: str = "acme"):
    content, artifact = query_failed_events(
        MockFailedEventsBackend(scenario), site_id, *_window(minutes), source
    )
    return json.loads(content), artifact


def test_groups_merchant_outage_by_status(merchant_down):
    result, artifact = _run(merchant_down)
    assert artifact["ok"]
    assert result["totals"] == {"failed": 412, "dlq": 37}
    top = result["by_status"][0]
    assert (top["status"], top["count"], top["failed"], top["dlq"]) == (503, 417, 380, 37)
    assert top["destinations"] == {"wg_3532": 417}
    assert top["sample_messages"] == ["Service Unavailable"]
    assert len(top["sample_event_ids"]) == 3
    timeouts = result["by_status"][1]
    assert timeouts["status"] == 504
    assert set(timeouts["event_types"]) <= {"payment.succeeded", "invoice.paid"}
    assert sum(timeouts["event_types"].values()) == 32
    assert result["by_destination"][0]["destination_id"] == "wg_3532"
    assert result["dlq_reasons"] == {"MAX_RETRIES_EXCEEDED": 37}
    assert result["config_fetch_failures"]["count"] == 0
    assert "412 failed · 37 DLQ · top status 503 (417)" == artifact["summary"]


def test_event_types_come_from_event_content(merchant_down):
    result, _ = _run(merchant_down)
    types = {row["type"] for row in result["by_event_type"]}
    assert types <= {"payment.succeeded", "subscription.renewed", "invoice.paid", "payment.refunded"}


def test_never_returns_event_content(merchant_down):
    content, _ = query_failed_events(
        MockFailedEventsBackend(merchant_down), "acme", *_window(400), "both"
    )
    assert "eventContent" not in content
    assert "customer_email" not in content
    assert "jane.doe" not in content


def test_redacts_secrets_in_failure_messages(merchant_down):
    result, _ = _run(merchant_down, minutes=400)
    bad_request = next(row for row in result["by_status"] if row["status"] == 400)
    assert "sk_live" not in json.dumps(bad_request)
    assert "signature=[REDACTED]" in bad_request["sample_messages"][0]


def test_source_filter(merchant_down):
    failed_only, _ = _run(merchant_down, source="failed")
    assert failed_only["totals"] == {"failed": 412, "dlq": 0}
    dlq_only, _ = _run(merchant_down, source="dlq")
    assert dlq_only["totals"] == {"failed": 0, "dlq": 37}


def test_config_fetch_failures_are_reported_separately(now_ms):
    config_rows = [
        {
            "eventId": f"evt_cfg{i}",
            "destinationKey": "NONE:CONFIG_FETCH",
            "tenant_id": "acme",
            "destinationId": None,
            "destinationType": "CONFIG_FETCH",
            "failureType": "CONFIG_FETCH_ERROR",
            "dlqReason": "CONFIG_UNAVAILABLE",
            "retryAttempts": 0,
            "failureStatusCode": 401,
            "failureMessage": "webhook-config API returned 401 Unauthorized (Authorization: Basic aW5jaWRlbnQ6c2VjcmV0)",
            "eventContent": '{"type": "invoice.paid"}',
            "created_at": now_ms - (i + 1) * MINUTE_MS,
            "updatedAt": now_ms - (i + 1) * MINUTE_MS,
        }
        for i in range(4)
    ]
    scenario = Scenario(name="t", description="", dlq_events=config_rows)
    result, artifact = _run(scenario)
    assert result["by_status"] == []
    cfg = result["config_fetch_failures"]
    assert cfg["count"] == 4
    assert cfg["by_status"] == {"401": 4}  # JSON object keys are strings
    assert "aW5jaWRlbnQ6c2VjcmV0" not in json.dumps(cfg)
    assert "4 config-fetch" in artifact["summary"]


def test_no_failures(merchant_down):
    result, artifact = _run(merchant_down, site_id="initech")
    assert result["totals"] == {"failed": 0, "dlq": 0}
    assert artifact["summary"] == "no failures in window"


@pytest.mark.parametrize(
    ("start_delta", "end_delta", "message"),
    [
        (timedelta(0), timedelta(hours=-1), "start must be before end"),
        (timedelta(days=-8), timedelta(0), "longer than 7 days"),
        (timedelta(hours=-1), timedelta(hours=2), "in the future"),
    ],
)
def test_rejects_bad_windows(merchant_down, start_delta, end_delta, message):
    now = datetime.now(UTC)
    content, artifact = query_failed_events(
        MockFailedEventsBackend(merchant_down), "acme", now + start_delta, now + end_delta
    )
    assert not artifact["ok"]
    assert message in json.loads(content)["error"]


def test_tool_accepts_iso_strings_from_the_model(merchant_down):
    tool = make_query_failed_events_tool(MockFailedEventsBackend(merchant_down))
    start, end = _window(120)
    message = tool.invoke(
        {
            "type": "tool_call",
            "id": "call_1",
            "name": "query_failed_events",
            "args": {
                "site_id": "acme",
                "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        }
    )
    assert message.artifact["ok"]
    assert json.loads(message.content)["totals"]["dlq"] == 37
