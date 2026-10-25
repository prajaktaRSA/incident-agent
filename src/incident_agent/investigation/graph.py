"""The investigation graph: playbook → gather → triage → RCA → publish.

Only the RCA node calls an LLM; every other step is plain code.
"""

import time
from typing import Any, TypedDict

from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from incident_agent.audit import AuditLog
from incident_agent.config import TopicMapping
from incident_agent.intake.models import InvestigationRequest
from incident_agent.investigation.gather import gather_evidence
from incident_agent.investigation.models import Check, Evidence, Hypothesis
from incident_agent.investigation.playbooks import select_playbook
from incident_agent.investigation.publish import format_rca_message, write_outbox, write_record
from incident_agent.investigation.rca import RcaOutcome, RcaWriter, write_rca
from incident_agent.investigation.triage import triage
from incident_agent.settings import Settings


class InvestigationState(TypedDict, total=False):
    request: InvestigationRequest
    started_at: float  # time.perf_counter() when the investigation began
    checks: list[Check]
    evidence: list[Evidence]
    hypotheses: list[Hypothesis]
    outcome: RcaOutcome
    message: str
    outbox_path: str
    record_path: str


def build_investigation_graph(
    *,
    tools: list[BaseTool],
    rca_writer: RcaWriter,
    topics: TopicMapping,
    settings: Settings,
    audit: AuditLog | None = None,
) -> CompiledStateGraph:
    def plan(state: InvestigationState) -> dict[str, Any]:
        checks = select_playbook(state["request"], topics)
        if audit:
            audit.write("playbook", checks=[c.model_dump() for c in checks])
        return {"checks": checks}

    def gather(state: InvestigationState) -> dict[str, Any]:
        evidence = gather_evidence(
            state["checks"], tools, timeout_seconds=settings.check_timeout_seconds, audit=audit
        )
        return {"evidence": evidence}

    def rank(state: InvestigationState) -> dict[str, Any]:
        hypotheses = triage(state["evidence"], state["request"])
        if audit:
            audit.write("triage", hypotheses=[h.model_dump(mode="json") for h in hypotheses])
        return {"hypotheses": hypotheses}

    def rca(state: InvestigationState) -> dict[str, Any]:
        outcome = write_rca(rca_writer, state["request"], state["evidence"], state["hypotheses"])
        if audit:
            audit.write(
                "rca",
                model=outcome.model,
                attempts=outcome.attempts,
                deterministic=outcome.deterministic,
                problems=outcome.problems,
                category=outcome.rca.category,
            )
        return {"outcome": outcome}

    def publish(state: InvestigationState) -> dict[str, Any]:
        request = state["request"]
        duration = time.perf_counter() - state["started_at"]
        message = format_rca_message(request, state["outcome"], state["evidence"], duration)
        outbox = write_outbox(settings.outbox_dir, request.id, request.reply_to, message)
        record = write_record(
            settings.records_dir,
            request,
            state["evidence"],
            state["hypotheses"],
            state["outcome"],
            duration,
            outbox,
        )
        if audit:
            audit.write("published", reply_to=request.reply_to, outbox=str(outbox), record=str(record))
        return {"message": message, "outbox_path": str(outbox), "record_path": str(record)}

    graph = StateGraph(InvestigationState)
    graph.add_node("select_playbook", plan)
    graph.add_node("gather", gather)
    graph.add_node("triage", rank)
    graph.add_node("write_rca", rca)
    graph.add_node("publish", publish)
    graph.add_edge(START, "select_playbook")
    graph.add_edge("select_playbook", "gather")
    graph.add_edge("gather", "triage")
    graph.add_edge("triage", "write_rca")
    graph.add_edge("write_rca", "publish")
    graph.add_edge("publish", END)
    return graph.compile()
