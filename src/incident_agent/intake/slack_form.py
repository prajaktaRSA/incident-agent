"""Turns a "Report webhook issue" Slack workflow submission into an InvestigationRequest.

Every field arrives structured from the form; this module only validates them with fixed rules
and asks fixed questions when something is missing. The free-text description is passed through
as context and never parsed.
"""

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from incident_agent.intake import questions
from incident_agent.intake.models import InvestigationRequest, IntakeResult, Question

ISSUE_TYPES = {"not_received", "failing", "delayed"}
EVENT_ID_PATTERN = re.compile(r"^[a-z]{2,4}_[A-Za-z0-9]{6,}$")
LOOKBACK_BEFORE_START = timedelta(minutes=30)
MAX_WINDOW = timedelta(days=7)
FUTURE_TOLERANCE = timedelta(minutes=5)


def parse_slack_form(
    trigger: dict[str, Any],
    *,
    known_sites: frozenset[str],
    known_event_types: set[str],
    now: datetime,
) -> IntakeResult:
    fields = trigger.get("fields") or {}
    reply_to = f"{trigger['channel']}/{trigger['ts']}"
    reported_by = trigger.get("reported_by")
    asks: list[Question] = []

    site_id = _text(fields.get("site_id"))
    if not site_id:
        asks.append(questions.site_missing())
    elif site_id not in known_sites:
        asks.append(questions.site_unknown(site_id))

    issue_type = _text(fields.get("issue_type"))
    if issue_type not in ISSUE_TYPES:
        asks.append(questions.issue_type_invalid())

    started_at = None
    raw_started = _text(fields.get("started_at"))
    if not raw_started:
        asks.append(questions.started_at_missing())
    else:
        started_at = _parse_time(raw_started)
        if started_at is None:
            asks.append(questions.started_at_invalid(raw_started))
        elif started_at > now + FUTURE_TOLERANCE:
            asks.append(questions.started_at_in_future())

    event_type = _text(fields.get("event_type"))
    if event_type and event_type not in known_event_types:
        asks.append(questions.event_type_unknown(event_type, known_event_types))

    event_ids = _list(fields.get("event_ids"))
    malformed = [e for e in event_ids if not EVENT_ID_PATTERN.match(e)]
    if malformed:
        asks.append(questions.event_ids_malformed(malformed))

    if issue_type == "not_received" and not event_type and not event_ids:
        asks.append(questions.event_info_missing())

    if asks:
        return IntakeResult(questions=asks, reply_to=reply_to, reported_by=reported_by)

    assert site_id and issue_type and started_at
    window_start = started_at - LOOKBACK_BEFORE_START
    capped = now - window_start > MAX_WINDOW
    request = InvestigationRequest(
        source="slack_form",
        source_ref=reply_to,
        reply_to=reply_to,
        reported_by=reported_by,
        issue_type=issue_type,  # type: ignore[arg-type]
        site_id=site_id,
        window_start=now - MAX_WINDOW if capped else window_start,
        window_end=now,
        window_capped=capped,
        event_type=event_type,
        event_ids=event_ids,
        destination_id=_text(fields.get("destination_id")),
        description=_text(fields.get("description")),
        jira_url=_text(fields.get("jira_url")),
    )
    return IntakeResult(request=request, reply_to=reply_to, reported_by=reported_by)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _list(value: Any) -> list[str]:
    if not value:
        return []
    items = value if isinstance(value, list) else str(value).split(",")
    return [s for s in (str(i).strip() for i in items) if s]


def _parse_time(value: str) -> datetime | None:
    """Accepts ISO-8601 and the form's "YYYY-MM-DD HH:MM" (UTC when no zone is given)."""
    cleaned = value.strip().removesuffix(" UTC").replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
