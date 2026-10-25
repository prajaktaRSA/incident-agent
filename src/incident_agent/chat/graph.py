"""The chat graph: an agent node and a tools node, looping until the agent answers."""

from typing import Annotated, Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph, add_messages
from langgraph.graph.state import CompiledStateGraph

from incident_agent.audit import AuditLog
from incident_agent.chat.prompts import SYSTEM_PROMPT, TOOL_BUDGET_EXHAUSTED
from incident_agent.llm import with_fallback
from incident_agent.tools.runner import run_tool_call


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
    model_with_tools = with_fallback(
        model.bind_tools(tools), fallback_model.bind_tools(tools) if fallback_model else None
    )
    answer_only_model = with_fallback(model, fallback_model)
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
            refuse = "Tool budget for this turn is exhausted." if used >= max_tool_calls else None
            results.append(run_tool_call(tools_by_name, call, audit, refuse=refuse))
            used += 1
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
