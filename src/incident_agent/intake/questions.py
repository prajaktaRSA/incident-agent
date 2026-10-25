"""Fixed follow-up questions for missing or invalid form fields. No LLM involved."""

from incident_agent.intake.models import Question


def site_missing() -> Question:
    return Question(field="site_id", text="Which site ID is this about?")


def site_unknown(value: str) -> Question:
    return Question(field="site_id", text=f"Site `{value}` doesn't exist. Please check the site ID.")


def issue_type_invalid() -> Question:
    return Question(
        field="issue_type",
        text="What kind of issue is it: *not received*, *failing* or *delayed*?",
    )


def started_at_missing() -> Question:
    return Question(
        field="started_at", text="Roughly when did it start? (e.g. 2026-10-25 14:00 UTC)"
    )


def started_at_invalid(value: str) -> Question:
    return Question(
        field="started_at",
        text=f"I couldn't read the start time `{value}`. Please use a format like 2026-10-25 14:00 UTC.",
    )


def started_at_in_future() -> Question:
    return Question(
        field="started_at",
        text="The start time is in the future. Roughly when did it start? (e.g. 2026-10-25 14:00 UTC)",
    )


def event_info_missing() -> Question:
    return Question(
        field="event_type",
        text="Which event type or event ID did the customer expect?",
    )


def event_type_unknown(value: str, known: set[str]) -> Question:
    return Question(
        field="event_type",
        text=f"`{value}` isn't a known event type. Known types: {', '.join(sorted(known))}.",
    )


def event_ids_malformed(values: list[str]) -> Question:
    return Question(
        field="event_ids",
        text=f"Event IDs look like `evt_8k2lq9`. Please check: {', '.join(f'`{v}`' for v in values)}",
    )
