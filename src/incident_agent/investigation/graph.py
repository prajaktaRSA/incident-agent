"""The investigation graph.

    select_playbook → gather → triage → runbooks → write_rca ─┬─▶ publish
                                                              └─▶ extra_checks ─▶ write_rca ─▶ publish
                                                                  (≤ 3 LLM-chosen checks, one round)

Only write_rca and extra_checks call an LLM; every other step is plain code.
"""

import time
from typing import Any, TypedDict

from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from incident_agent.audit import AuditLog
from incident_agent.config import MetricsConfig, TopicMapping
from incident_agent.intake.models import InvestigationRequest
from incident_agent.investigation.extra_checks import ExtraCheckPlanner, plan_extra_checks
from incident_agent.investigation.gather import gather_evidence
from incident_agent.investigation.models import Check, Evidence, Hypothesis
from incident_agent.investigation.playbooks import runbook_check, select_playbook
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
    extra_checks: list[Check]
    extra_round_done: bool
    message: str
    outbox_path: str
    record_path: str


def build_investigation_graph(
    *,
    tools: list[BaseTool],
    rca_writer: RcaWriter,
    topics: TopicMapping,
    metrics: MetricsConfig,
    settings: Settings,
    extra_check_planner: ExtraCheckPlanner | None = None,
    audit: AuditLog | None = None,
) -> CompiledStateGraph:
    def gather(checks: list[Check]) -> list[Evidence]:
        return gather_evidence(checks, tools, timeout_seconds=settings.check_timeout_seconds, audit=audit)

    def rank(evidence: list[Evidence], request: InvestigationRequest) -> list[Hypothesis]:
        hypotheses = triage(evidence, request)
        if audit:
            audit.write("triage", hypotheses=[h.model_dump(mode="json") for h in hypotheses])
        return hypotheses

    def plan_node(state: InvestigationState) -> dict[str, Any]:
        checks = select_playbook(state["request"], topics, metrics)
        if audit:
            audit.write("playbook", checks=[c.model_dump() for c in checks])
        return {"checks": checks}

    def gather_node(state: InvestigationState) -> dict[str, Any]:
        return {"evidence": gather(state["checks"])}

    def triage_node(state: InvestigationState) -> dict[str, Any]:
        return {"hypotheses": rank(state["evidence"], state["request"])}

    def runbooks_node(state: InvestigationState) -> dict[str, Any]:
        check = runbook_check(state["hypotheses"][0].category, state["request"])
        return {"evidence": state["evidence"] + gather([check])}

    def rca_node(state: InvestigationState) -> dict[str, Any]:
        allowed = extra_check_planner is not None and not state.get("extra_round_done", False)
        outcome = write_rca(
            rca_writer, state["request"], state["evidence"], state["hypotheses"], extra_checks_allowed=allowed
        )
        if audit:
            audit.write(
                "rca",
                model=outcome.model,
                attempts=outcome.attempts,
                deterministic=outcome.deterministic,
                problems=outcome.problems,
                category=outcome.rca.category,
                needs_more_checks=outcome.rca.needs_more_checks,
            )
        return {"outcome": outcome}

    def after_rca(state: InvestigationState) -> str:
        outcome = state["outcome"]
        wants_more = bool(outcome.rca.needs_more_checks) and not outcome.deterministic
        if extra_check_planner and wants_more and not state.get("extra_round_done", False):
            return "extra_checks"
        return "publish"

    def extra_checks_node(state: InvestigationState) -> dict[str, Any]:
        assert extra_check_planner is not None
        request = state["request"]
        try:
            checks = plan_extra_checks(
                extra_check_planner, request, state["evidence"], state["hypotheses"], state["outcome"].rca
            )
        except Exception as exc:  # the first draft stands if planning fails
            if audit:
                audit.write("extra_checks_failed", error=f"{type(exc).__name__}: {exc}")
            checks = []
        if audit:
            audit.write("extra_checks", checks=[c.model_dump() for c in checks])
        if not checks:
            return {"extra_checks": [], "extra_round_done": True}
        evidence = state["evidence"] + gather(checks)
        return {
            "extra_checks": checks,
            "extra_round_done": True,
            "evidence": evidence,
            "hypotheses": rank(evidence, request),
        }

    def after_extra_checks(state: InvestigationState) -> str:
        return "write_rca" if state.get("extra_checks") else "publish"

    def publish_node(state: InvestigationState) -> dict[str, Any]:
        request = state["request"]
        duration = time.perf_counter() - state["started_at"]
        message = format_rca_message(
            request, state["outcome"], state["evidence"], duration, extra_checks=len(state.get("extra_checks", []))
        )
        outbox = write_outbox(settings.outbox_dir, request.id, request.reply_to, message)
        record = write_record(
            settings.records_dir, request, state["evidence"], state["hypotheses"], state["outcome"], duration, outbox
        )
        if audit:
            audit.write("published", reply_to=request.reply_to, outbox=str(outbox), record=str(record))
        return {"message": message, "outbox_path": str(outbox), "record_path": str(record)}

    graph = StateGraph(InvestigationState)
    graph.add_node("select_playbook", plan_node)
    graph.add_node("gather", gather_node)
    graph.add_node("triage", triage_node)
    graph.add_node("runbooks", runbooks_node)
    graph.add_node("write_rca", rca_node)
    graph.add_node("extra_checks", extra_checks_node)
    graph.add_node("publish", publish_node)
    graph.add_edge(START, "select_playbook")
    graph.add_edge("select_playbook", "gather")
    graph.add_edge("gather", "triage")
    graph.add_edge("triage", "runbooks")
    graph.add_edge("runbooks", "write_rca")
    graph.add_conditional_edges("write_rca", after_rca, ["extra_checks", "publish"])
    graph.add_conditional_edges("extra_checks", after_extra_checks, ["write_rca", "publish"])
    graph.add_edge("publish", END)
    return graph.compile()
