"""The investigation graph: an agent node and a tools node, looping until the agent answers."""

import json
import time
from typing import Annotated, Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, SystemMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph, add_messages
from langgraph.graph.state import CompiledStateGraph

from incident_agent.audit import AuditLog
from incident_agent.llm import TRANSIENT_LLM_ERRORS
from incident_agent.prompts import SYSTEM_PROMPT, TOOL_BUDGET_EXHAUSTED
from incident_agent.redact import redact


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    # Reset to 0 by the caller on every user turn.
    tool_calls_used: int


def build_graph(
    model: BaseChatModel,
    tools: list[BaseTool],
    *,
    max_tool_calls: int,
    fallback_model: BaseChatModel | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    audit: AuditLog | None = None,
) -> CompiledStateGraph:
    model_with_tools = _with_fallback(
        model.bind_tools(tools), fallback_model.bind_tools(tools) if fallback_model else None
    )
    answer_only_model = _with_fallback(model, fallback_model)
    tools_by_name = {t.name: t for t in tools}

    def agent(state: AgentState) -> dict[str, Any]:
        if state.get("tool_calls_used", 0) >= max_tool_calls:
            prompt = [SystemMessage(SYSTEM_PROMPT + TOOL_BUDGET_EXHAUSTED), *state["messages"]]
            return {"messages": [answer_only_model.invoke(prompt)]}
        prompt = [SystemMessage(SYSTEM_PROMPT), *state["messages"]]
        return {"messages": [model_with_tools.invoke(prompt)]}

    def run_tools(state: AgentState) -> dict[str, Any]:
        last = state["messages"][-1]
        assert isinstance(last, AIMessage)
        used = state.get("tool_calls_used", 0)
        results: list[ToolMessage] = []
        for call in last.tool_calls:
            started = time.perf_counter()
            if used >= max_tool_calls:
                message = _error_message(call, "Tool budget for this turn is exhausted.")
            elif call["name"] not in tools_by_name:
                message = _error_message(call, f"Unknown tool '{call['name']}'.")
            else:
                try:
                    message = tools_by_name[call["name"]].invoke(call)
                except Exception as exc:  # bad arguments from the model, or a backend failure
                    message = _error_message(call, redact(f"{type(exc).__name__}: {exc}"))
            used += 1
            artifact = dict(message.artifact or {})
            artifact["duration_ms"] = round((time.perf_counter() - started) * 1000)
            message.artifact = artifact
            if audit:
                audit.write(
                    "tool_call",
                    tool=call["name"],
                    args=call["args"],
                    ok=artifact.get("ok", False),
                    summary=artifact.get("summary"),
                    duration_ms=artifact["duration_ms"],
                )
            results.append(message)
        return {"messages": results, "tool_calls_used": used}

    def route(state: AgentState) -> str:
        last = state["messages"][-1]
        return "tools" if isinstance(last, AIMessage) and last.tool_calls else END

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent)
    graph.add_node("tools", run_tools)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, ["tools", END])
    graph.add_edge("tools", "agent")
    return graph.compile(checkpointer=checkpointer)


def _with_fallback(primary: Runnable, fallback: Runnable | None) -> Runnable:
    if fallback is None:
        return primary
    return primary.with_fallbacks([fallback], exceptions_to_handle=TRANSIENT_LLM_ERRORS)


def _error_message(call: dict[str, Any], error: str) -> ToolMessage:
    return ToolMessage(
        content=json.dumps({"error": error}),
        tool_call_id=call["id"],
        name=call["name"],
        artifact={"ok": False, "summary": error},
    )
