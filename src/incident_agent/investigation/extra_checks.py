"""The optional extra-checks round: when the first RCA draft finds the evidence ambiguous, the LLM
may call up to 3 tools (same tools and guardrails as the playbooks), then the RCA is rewritten once.
"""

from typing import Any, Protocol

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool

from incident_agent.intake.models import InvestigationRequest
from incident_agent.investigation.models import Check, Evidence, Hypothesis
from incident_agent.investigation.prompts import EXTRA_CHECKS_PROMPT
from incident_agent.investigation.rca import RCA, build_bundle
from incident_agent.llm import ChatModels, with_fallback

MAX_EXTRA_CHECKS = 3


class ExtraCheckPlanner(Protocol):
    def __call__(self, messages: list[BaseMessage]) -> list[dict[str, Any]]:
        """Tool calls ({"name", "args", "id"}) the model wants to make."""
        ...


class LlmExtraCheckPlanner:
    def __init__(self, models: ChatModels, tools: list[BaseTool]) -> None:
        self._runnable = with_fallback(
            models.primary.bind_tools(tools),
            models.fallback.bind_tools(tools) if models.fallback else None,
        )

    def __call__(self, messages: list[BaseMessage]) -> list[dict[str, Any]]:
        return list(self._runnable.invoke(messages).tool_calls)


def plan_extra_checks(
    planner: ExtraCheckPlanner,
    request: InvestigationRequest,
    evidence: list[Evidence],
    hypotheses: list[Hypothesis],
    draft: RCA,
) -> list[Check]:
    messages = [
        SystemMessage(EXTRA_CHECKS_PROMPT),
        HumanMessage(
            build_bundle(request, evidence, hypotheses)
            + "\n\nThe draft RCA asked to check:\n- "
            + "\n- ".join(draft.needs_more_checks)
        ),
    ]
    calls = planner(messages)[:MAX_EXTRA_CHECKS]
    return [
        Check(
            name=f"extra_{n}_{call['name']}",
            tool=call["name"],
            args=call.get("args", {}),
            purpose=f"Extra check requested by the RCA step: {draft.needs_more_checks[min(n - 1, len(draft.needs_more_checks) - 1)]}",
        )
        for n, call in enumerate(calls, start=1)
    ]
