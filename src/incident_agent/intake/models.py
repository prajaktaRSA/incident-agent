"""The normalized trigger every investigation starts from."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

IssueType = Literal["not_received", "failing", "delayed", "alarm"]


class AlarmInfo(BaseModel):
    name: str
    namespace: str
    metric: str
    dimensions: dict[str, str]
    threshold: float | None
    state_change_time: datetime


class InvestigationRequest(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    source: Literal["slack_form", "cloudwatch_alarm"]
    source_ref: str  # Slack channel/ts, or alarm name + state change time (the dedupe key)
    reply_to: str  # Slack channel/thread where the RCA goes
    reported_by: str | None = None
    issue_type: IssueType
    site_id: str | None  # None for service-wide alarms
    window_start: datetime
    window_end: datetime
    window_capped: bool = False  # started_at was older than the maximum window
    event_type: str | None = None
    event_ids: list[str] = Field(default_factory=list)
    destination_id: str | None = None
    alarm: AlarmInfo | None = None
    description: str | None = None  # free text from the reporter: context only, never parsed
    jira_url: str | None = None


class Question(BaseModel):
    field: str
    text: str


class IntakeResult(BaseModel):
    """Exactly one of: a request to investigate, questions to ask, or a reason to ignore."""

    request: InvestigationRequest | None = None
    questions: list[Question] = Field(default_factory=list)
    reply_to: str | None = None
    reported_by: str | None = None
    ignored: str | None = None
