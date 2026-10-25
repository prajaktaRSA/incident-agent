"""Data passed between the investigation steps."""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Category(StrEnum):
    NO_ENDPOINT = "NO_ENDPOINT"
    NOT_SUBSCRIBED = "NOT_SUBSCRIBED"
    DELIVERED_OK = "DELIVERED_OK"
    STILL_RETRYING = "STILL_RETRYING"
    NOT_CONSUMED = "NOT_CONSUMED"
    MERCHANT_ENDPOINT_DOWN = "MERCHANT_ENDPOINT_DOWN"
    MERCHANT_REJECTING = "MERCHANT_REJECTING"
    MERCHANT_RATE_LIMITING = "MERCHANT_RATE_LIMITING"
    CONFIG_FETCH_FAILURE = "CONFIG_FETCH_FAILURE"
    BAD_DEPLOY = "BAD_DEPLOY"
    CONSUMER_LAG = "CONSUMER_LAG"
    TRAFFIC_BURST = "TRAFFIC_BURST"
    UNDETERMINED = "UNDETERMINED"


class Check(BaseModel):
    """One evidence-gathering step chosen by a playbook."""

    name: str  # unique within an investigation; what the RCA cites as a source
    tool: str
    args: dict[str, Any]
    purpose: str  # plain-language description, shown when the check couldn't run


class Evidence(BaseModel):
    check: str
    tool: str
    args: dict[str, Any]
    purpose: str
    status: Literal["ok", "error", "skipped", "timeout"]
    summary: str
    data: dict[str, Any] | None = None
    duration_ms: int | None = None


class Hypothesis(BaseModel):
    category: Category
    score: float  # 0..1
    reasons: list[str]
    supporting_checks: list[str]
    # Checks that would confirm or rule this out but didn't run successfully.
    missing_checks: list[str] = Field(default_factory=list)
