"""The RCA step: the only place the investigation pipeline calls an LLM.

The model gets the gathered evidence and the triage hypotheses and returns a structured RCA.
Code then checks it (category must come from triage, every cited source must be a check that ran)
and regenerates once if needed. If the LLM is unavailable or keeps failing the checks, a
deterministic RCA built from triage is published instead, so an investigation always ends with
something for the engineer.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from incident_agent.intake.models import InvestigationRequest
from incident_agent.investigation.prompts import RCA_SYSTEM_PROMPT
from incident_agent.investigation.models import Category, Evidence, Hypothesis
from incident_agent.llm import ChatModels, with_fallback

MAX_DATA_CHARS_PER_CHECK = 8000


class EvidenceItem(BaseModel):
    claim: str = Field(description="A factual statement with exact numbers and times.")
    source: str = Field(description="Name of the check that produced it, e.g. 'failed_events'.")


class RCA(BaseModel):
    title: str = Field(description="One line naming the cause and scope.")
    category: Category
    summary: str = Field(description="2-3 sentences: what happened, scope, when.")
    root_cause: str
    confidence: Literal["high", "medium", "low"]
    evidence: list[EvidenceItem]
    timeline: list[str] = Field(description="Chronological, each entry starting with a UTC time.")
    impact: str = Field(description="Affected sites, endpoints, event types and counts.")
    next_steps: list[str] = Field(description="Concrete internal actions for engineers.")
    unknowns: list[str] = Field(description="What couldn't be checked and what would settle it.")
    runbooks: list[str] = Field(default_factory=list, description="Runbook links from the evidence only.")
    needs_more_checks: list[str] = Field(
        default_factory=list,
        description="Only if the evidence is ambiguous and extra checks are allowed: up to 3 specific "
        "things to check next. Otherwise empty.",
    )


class RcaParseError(RuntimeError):
    pass


@dataclass
class RcaDraft:
    rca: RCA
    model: str


class RcaWriter(Protocol):
    def __call__(self, messages: list[BaseMessage]) -> RcaDraft: ...


class LlmRcaWriter:
    """Structured-output call on the primary model, with the fallback model for transient errors."""

    def __init__(self, models: ChatModels) -> None:
        def structured(model):
            return model.with_structured_output(RCA, include_raw=True)

        self._runnable = with_fallback(
            structured(models.primary), structured(models.fallback) if models.fallback else None
        )

    def __call__(self, messages: list[BaseMessage]) -> RcaDraft:
        out = self._runnable.invoke(messages)
        if out.get("parsing_error") or out.get("parsed") is None:
            raise RcaParseError(f"Model output didn't match the RCA schema: {out.get('parsing_error')}")
        raw = out["raw"]
        return RcaDraft(rca=out["parsed"], model=raw.response_metadata.get("model_name", "unknown"))


@dataclass
class RcaOutcome:
    rca: RCA
    model: str | None  # None when the deterministic fallback was used
    attempts: int
    problems: list[str] = field(default_factory=list)  # check failures and errors along the way
    deterministic: bool = False


def write_rca(
    writer: RcaWriter,
    request: InvestigationRequest,
    evidence: list[Evidence],
    hypotheses: list[Hypothesis],
    *,
    extra_checks_allowed: bool = False,
    max_attempts: int = 2,
) -> RcaOutcome:
    messages: list[BaseMessage] = [
        SystemMessage(RCA_SYSTEM_PROMPT),
        HumanMessage(build_bundle(request, evidence, hypotheses, extra_checks_allowed=extra_checks_allowed)),
    ]
    problems: list[str] = []
    attempts = 0
    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        try:
            draft = writer(messages)
        except Exception as exc:  # LLM unavailable (after fallback) or unusable output
            problems.append(f"attempt {attempt}: {type(exc).__name__}: {exc}"[:500])
            break
        issues = check_rca(draft.rca, evidence, hypotheses)
        if not issues:
            return RcaOutcome(rca=draft.rca, model=draft.model, attempts=attempt, problems=problems)
        problems.extend(f"attempt {attempt}: {issue}" for issue in issues)
        messages += [
            AIMessage(draft.rca.model_dump_json()),
            HumanMessage(
                "Your RCA failed these checks. Return a corrected RCA.\n- " + "\n- ".join(issues)
            ),
        ]
    return RcaOutcome(
        rca=deterministic_rca(request, evidence, hypotheses, problems),
        model=None,
        attempts=attempts,
        problems=problems,
        deterministic=True,
    )


def check_rca(rca: RCA, evidence: list[Evidence], hypotheses: list[Hypothesis]) -> list[str]:
    issues = []
    allowed = {h.category for h in hypotheses} | {Category.UNDETERMINED}
    if rca.category not in allowed:
        issues.append(
            f"category {rca.category} is not one of the triage hypotheses "
            f"({', '.join(sorted(allowed))})"
        )
    ran = {e.check for e in evidence if e.status == "ok"}
    bad_sources = sorted({item.source for item in rca.evidence} - ran)
    if bad_sources:
        issues.append(
            f"evidence cites checks that didn't run successfully: {', '.join(bad_sources)} "
            f"(valid sources: {', '.join(sorted(ran)) or 'none'})"
        )
    if not rca.evidence and rca.category != Category.UNDETERMINED:
        issues.append("a category other than UNDETERMINED needs at least one evidence item")
    return issues


def build_bundle(
    request: InvestigationRequest,
    evidence: list[Evidence],
    hypotheses: list[Hypothesis],
    *,
    extra_checks_allowed: bool = False,
) -> str:
    bundle: dict[str, Any] = {
        "investigation": {
            "id": request.id,
            "source": request.source,
            "issue_type": request.issue_type,
            "site_id": request.site_id,
            "window": {"start": request.window_start.isoformat(), "end": request.window_end.isoformat()},
            "window_capped_to_7_days": request.window_capped,
            "event_type": request.event_type,
            "event_ids": request.event_ids,
            "destination_id": request.destination_id,
            "alarm": request.alarm.model_dump(mode="json") if request.alarm else None,
            "reporter_description": request.description,
        },
        "checks": [
            {"check": e.check, "purpose": e.purpose, "status": e.status, "summary": e.summary,
             "data": _truncate(e.data)}
            for e in evidence
            if e.status == "ok"
        ],
        "checks_not_run": [
            {"check": e.check, "purpose": e.purpose, "status": e.status, "reason": e.summary}
            for e in evidence
            if e.status != "ok"
        ],
        "triage_hypotheses": [h.model_dump(mode="json") for h in hypotheses],
        "extra_checks_allowed": extra_checks_allowed,
    }
    return json.dumps(bundle, indent=1, default=str)


def deterministic_rca(
    request: InvestigationRequest,
    evidence: list[Evidence],
    hypotheses: list[Hypothesis],
    problems: list[str],
) -> RCA:
    top = hypotheses[0] if hypotheses else None
    ran = [e for e in evidence if e.status == "ok"]
    lead = (
        f"Top triage hypothesis: {top.category} (score {top.score:.2f}): " + "; ".join(top.reasons)
        if top and top.category != Category.UNDETERMINED
        else "Triage found no matching cause."
    )
    return RCA(
        title=f"Needs manual review: {request.issue_type} for {request.site_id or 'service'}",
        category=Category.UNDETERMINED,
        summary="The automated write-up couldn't be produced or validated, so this is the raw "
        "triage result. Review the evidence below.",
        root_cause=lead,
        confidence="low",
        evidence=[{"claim": e.summary, "source": e.check} for e in ran],
        timeline=[],
        impact="See evidence.",
        next_steps=["Review the triage hypotheses and evidence manually."],
        unknowns=[f"{e.check}: {e.summary}" for e in evidence if e.status != "ok"]
        + [f"RCA generation: {p}" for p in problems],
    )


def _truncate(data: dict | None) -> Any:
    if data is None:
        return None
    text = json.dumps(data, separators=(",", ":"), default=str)
    if len(text) <= MAX_DATA_CHARS_PER_CHECK:
        return data
    return {"truncated": True, "json_prefix": text[:MAX_DATA_CHARS_PER_CHECK]}
