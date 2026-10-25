"""Trigger intake: structured parsing and validation only, no LLM."""

from datetime import datetime
from typing import Any

from incident_agent.config import load_known_sites, load_topic_mapping
from incident_agent.intake.alarm import parse_alarm
from incident_agent.intake.models import InvestigationRequest, IntakeResult, Question
from incident_agent.intake.slack_form import parse_slack_form
from incident_agent.settings import Settings


def intake(trigger: dict[str, Any], settings: Settings, now: datetime) -> IntakeResult:
    source = trigger.get("source")
    if source == "slack_form":
        return parse_slack_form(
            trigger,
            known_sites=load_known_sites(settings.config_dir),
            known_event_types=load_topic_mapping(settings.config_dir).event_types,
            now=now,
        )
    if source == "cloudwatch_alarm":
        return parse_alarm(trigger, incidents_channel=settings.incidents_channel, now=now)
    return IntakeResult(ignored=f"Unsupported trigger source: {source!r}")


__all__ = ["InvestigationRequest", "IntakeResult", "Question", "intake"]
