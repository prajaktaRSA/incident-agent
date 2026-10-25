from datetime import UTC, datetime, timedelta

import pytest

from conftest import FIXTURES_DIR
from incident_agent.backends.mock.triggers import load_trigger
from incident_agent.intake import intake
from incident_agent.intake.alarm import parse_alarm
from incident_agent.intake.slack_form import parse_slack_form
from incident_agent.settings import Settings

NOW = datetime(2026, 10, 25, 15, 0, tzinfo=UTC)
SITES = frozenset({"acme", "globex"})
EVENT_TYPES = {"invoice.paid", "payment.succeeded"}


def _form(**fields) -> dict:
    base = {"site_id": "acme", "issue_type": "failing", "started_at": "2026-10-25T13:30:00Z"}
    return {"source": "slack_form", "channel": "C1", "ts": "1.2", "reported_by": "se.jane",
            "fields": base | fields}


def _parse(trigger: dict):
    return parse_slack_form(trigger, known_sites=SITES, known_event_types=EVENT_TYPES, now=NOW)


def _question_fields(result) -> list[str]:
    return [q.field for q in result.questions]


def test_valid_form_becomes_a_request():
    result = _parse(_form(event_ids="evt_8k2lq9, evt_9p1zx4", jira_url="https://jira/SUP-1"))
    request = result.request
    assert result.questions == []
    assert request.site_id == "acme"
    assert request.issue_type == "failing"
    assert request.reply_to == "C1/1.2"
    assert request.window_start == datetime(2026, 10, 25, 13, 0, tzinfo=UTC)  # 30 min before start
    assert request.window_end == NOW
    assert request.event_ids == ["evt_8k2lq9", "evt_9p1zx4"]
    assert request.jira_url == "https://jira/SUP-1"


@pytest.mark.parametrize("value", ["2026-10-25 13:30", "2026-10-25 13:30 UTC", "2026-10-25T13:30:00+00:00"])
def test_accepts_form_time_formats(value):
    assert _parse(_form(started_at=value)).request.window_start.hour == 13


def test_old_start_is_capped_to_seven_days():
    request = _parse(_form(started_at="2026-10-01T00:00:00Z")).request
    assert request.window_capped
    assert NOW - request.window_start == timedelta(days=7)


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"site_id": ""}, ["site_id"]),
        ({"site_id": "acm3"}, ["site_id"]),
        ({"issue_type": "broken"}, ["issue_type"]),
        ({"started_at": None}, ["started_at"]),
        ({"started_at": "yesterday-ish"}, ["started_at"]),
        ({"started_at": "2026-10-26T09:00:00Z"}, ["started_at"]),
        ({"issue_type": "not_received"}, ["event_type"]),
        ({"event_type": "invoice.payed"}, ["event_type"]),
        ({"event_ids": ["evt_8k2lq9", "8k2lq9"]}, ["event_ids"]),
    ],
)
def test_problems_become_hardcoded_questions(fields, expected):
    result = _parse(_form(**fields))
    assert result.request is None
    assert _question_fields(result) == expected
    assert result.reply_to == "C1/1.2"


def test_all_problems_are_asked_at_once():
    result = _parse(_form(site_id="", started_at="", issue_type="not_received"))
    assert _question_fields(result) == ["site_id", "started_at", "event_type"]


def test_unknown_site_question_quotes_the_value():
    result = _parse(_form(site_id="acm3"))
    assert "`acm3`" in result.questions[0].text


def test_not_received_with_event_type_is_enough():
    assert _parse(_form(issue_type="not_received", event_type="invoice.paid")).request is not None


def _alarm(state: str = "ALARM") -> dict:
    return {
        "source": "cloudwatch_alarm",
        "AlarmName": "lag-critical",
        "NewStateValue": state,
        "StateChangeTime": "2026-10-25T14:55:00+00:00",
        "Trigger": {
            "Namespace": "AWS/Kafka",
            "MetricName": "EstimatedMaxTimeLag",
            "Threshold": 120,
            "Dimensions": [{"name": "Topic", "value": "payment-events"}],
        },
    }


def test_alarm_becomes_a_service_wide_request():
    request = parse_alarm(_alarm(), incidents_channel="#inc", now=NOW).request
    assert request.issue_type == "alarm"
    assert request.site_id is None
    assert request.reply_to == "#inc"
    assert request.alarm.dimensions == {"Topic": "payment-events"}
    assert request.window_start == datetime(2026, 10, 25, 12, 55, tzinfo=UTC)
    assert request.window_end == NOW  # alarm time + 15 min would be in the future
    assert request.source_ref.startswith("lag-critical@")


def test_alarm_recovery_is_ignored():
    result = parse_alarm(_alarm("OK"), incidents_channel="#inc", now=NOW)
    assert result.request is None
    assert "ALARM" in result.ignored


def test_fixture_triggers_resolve_relative_times():
    now = datetime.now(UTC)
    trigger = load_trigger(FIXTURES_DIR / "triggers" / "merchant_down.json", now)
    started = datetime.fromisoformat(trigger["fields"]["started_at"])
    assert abs((now - started) - timedelta(minutes=95)) < timedelta(seconds=1)


def test_intake_routes_by_source():
    settings = Settings(_env_file=None)
    now = datetime.now(UTC)
    missing = intake(load_trigger(FIXTURES_DIR / "triggers" / "missing_fields.json", now), settings, now)
    assert _question_fields(missing) == ["site_id", "started_at", "event_type"]
    alarm = intake(load_trigger(FIXTURES_DIR / "triggers" / "traffic_burst.json", now), settings, now)
    assert alarm.request.issue_type == "alarm"
    assert "Unsupported" in intake({"source": "email"}, settings, now).ignored
