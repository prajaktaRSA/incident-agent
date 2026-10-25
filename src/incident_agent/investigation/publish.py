"""Formats what gets posted to Slack and writes it to the outbox, plus the investigation record.

In the MVP nothing is sent anywhere: the message lands in outbox/ and the full record in records/.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from incident_agent.intake.models import IntakeResult, InvestigationRequest
from incident_agent.investigation.models import Evidence, Hypothesis
from incident_agent.investigation.rca import RcaOutcome

LABEL = "Automated RCA — internal"


def format_rca_message(
    request: InvestigationRequest,
    outcome: RcaOutcome,
    evidence: list[Evidence],
    duration_s: float,
    *,
    extra_checks: int = 0,
) -> str:
    rca = outcome.rca
    subject = request.site_id or (request.alarm.name if request.alarm else "service")
    status = "needs manual review" if outcome.deterministic else f"confidence: {rca.confidence}"
    lines = [
        f"🔎 **{LABEL}** · {subject} · {request.issue_type} · {status}",
        "",
        f"**{rca.title}**",
        "",
        f"**Summary** {rca.summary}",
        "",
        f"**Root cause** `{rca.category}` — {rca.root_cause}",
        "",
        "**Evidence**",
        *([f"- {item.claim} _({item.source})_" for item in rca.evidence] or ["- none: no check returned data"]),
    ]
    if rca.timeline:
        lines += ["", "**Timeline**", *[f"- {entry}" for entry in rca.timeline]]
    lines += ["", f"**Impact** {rca.impact}", "", "**Next steps**", *[f"- {s}" for s in rca.next_steps]]
    if rca.unknowns:
        lines += ["", "**Unknowns**", *[f"- {u}" for u in rca.unknowns]]
    if rca.needs_more_checks:
        lines += ["", "**Suggested follow-up checks**", *[f"- {c}" for c in rca.needs_more_checks]]
    if rca.runbooks:
        lines += ["", "**Runbooks** " + " · ".join(rca.runbooks)]
    if request.jira_url:
        lines += ["", f"**Jira (reference only)** {request.jira_url}"]

    ran = sum(e.status == "ok" for e in evidence)
    not_run = len(evidence) - ran
    model = outcome.model or "no model (deterministic fallback)"
    extra = f" (incl. {extra_checks} extra)" if extra_checks else ""
    lines += [
        "",
        f"_{ran} checks run{extra} · {not_run} failed or unavailable · {duration_s:.1f}s · {model} · "
        f"investigation {request.id}_",
    ]
    return "\n".join(lines) + "\n"


def format_questions_message(result: IntakeResult) -> str:
    mention = f"@{result.reported_by} " if result.reported_by else ""
    lines = [
        f"🔎 **{LABEL}** · needs more information",
        "",
        f"{mention}Before I can investigate, please reply in this thread:",
        *[f"- {q.text}" for q in result.questions],
    ]
    return "\n".join(lines) + "\n"


def write_outbox(outbox_dir: Path, name: str, reply_to: str | None, text: str) -> Path:
    outbox_dir.mkdir(parents=True, exist_ok=True)
    path = outbox_dir / f"{name}.md"
    path.write_text(f"<!-- reply_to: {reply_to} -->\n{text}")
    return path


def write_record(
    records_dir: Path,
    request: InvestigationRequest,
    evidence: list[Evidence],
    hypotheses: list[Hypothesis],
    outcome: RcaOutcome,
    duration_s: float,
    outbox_path: Path,
) -> Path:
    records_dir.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "id": request.id,
        "created_at": datetime.now(UTC).isoformat(),
        "duration_s": round(duration_s, 2),
        "request": request.model_dump(mode="json"),
        "evidence": [e.model_dump(mode="json") for e in evidence],
        "hypotheses": [h.model_dump(mode="json") for h in hypotheses],
        "rca": outcome.rca.model_dump(mode="json"),
        "rca_meta": {
            "model": outcome.model,
            "attempts": outcome.attempts,
            "deterministic": outcome.deterministic,
            "problems": outcome.problems,
        },
        "outbox": str(outbox_path),
    }
    path = records_dir / f"{request.id}.json"
    path.write_text(json.dumps(record, indent=2, default=str))
    return path
