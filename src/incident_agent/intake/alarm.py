"""Turns a CloudWatch alarm notification (SNS message) into an InvestigationRequest."""

from datetime import datetime, timedelta
from typing import Any

from incident_agent.intake.models import AlarmInfo, InvestigationRequest, IntakeResult

LOOKBACK_BEFORE_ALARM = timedelta(hours=2)
LOOKAHEAD_AFTER_ALARM = timedelta(minutes=15)


def parse_alarm(trigger: dict[str, Any], *, incidents_channel: str, now: datetime) -> IntakeResult:
    if trigger.get("NewStateValue") != "ALARM":
        return IntakeResult(
            ignored=f"Alarm {trigger.get('AlarmName')} moved to {trigger.get('NewStateValue')}; "
            "only transitions to ALARM are investigated."
        )

    spec = trigger["Trigger"]
    changed_at = datetime.fromisoformat(trigger["StateChangeTime"])
    dimensions = {d["name"]: d["value"] for d in spec.get("Dimensions", [])}
    alarm = AlarmInfo(
        name=trigger["AlarmName"],
        namespace=spec["Namespace"],
        metric=spec["MetricName"],
        dimensions=dimensions,
        threshold=spec.get("Threshold"),
        state_change_time=changed_at,
    )
    request = InvestigationRequest(
        source="cloudwatch_alarm",
        source_ref=f"{alarm.name}@{changed_at.isoformat()}",
        reply_to=incidents_channel,
        issue_type="alarm",
        site_id=dimensions.get("TenantId"),
        window_start=changed_at - LOOKBACK_BEFORE_ALARM,
        window_end=min(changed_at + LOOKAHEAD_AFTER_ALARM, now),
        alarm=alarm,
    )
    return IntakeResult(request=request, reply_to=incidents_channel)
