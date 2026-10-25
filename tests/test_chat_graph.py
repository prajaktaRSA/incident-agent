import json
from datetime import UTC, datetime, timedelta
from typing import Any

from langchain_core.exceptions import ModelAPIError
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from incident_agent.audit import AuditLog
from incident_agent.chat.graph import build_graph
from incident_agent.tools import build_mock_tools


class ScriptedModel(BaseChatModel):
    """Replays scripted replies in order; an Exception in the script is raised instead."""

    script: list[Any]

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        reply = self.script.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return ChatResult(generations=[ChatGeneration(message=reply)])

    def bind_tools(self, tools, **kwargs):
        return self


def _call(call_id: str, minutes: int = 120) -> dict:
    end = datetime.now(UTC)
    return {
        "id": call_id,
        "name": "query_failed_events",
        "args": {
            "site_id": "acme",
            "start": (end - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }


def _turn(text: str) -> dict:
    return {"messages": [HumanMessage(text)], "tool_calls_used": 0}


def test_runs_tool_then_answers(merchant_down, tmp_path):
    model = ScriptedModel(
        script=[
            AIMessage(content="", tool_calls=[_call("c1")]),
            AIMessage(content="**What happened** acme wg_3532 is returning 503s."),
        ]
    )
    audit = AuditLog(tmp_path, "s1")
    graph = build_graph(model, build_mock_tools(merchant_down), max_tool_calls=15, audit=audit)

    state = graph.invoke(_turn("what happened to acme?"))

    tool_messages = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].artifact["ok"]
    assert "duration_ms" in tool_messages[0].artifact
    assert json.loads(tool_messages[0].content)["totals"] == {"failed": 412, "dlq": 37}
    assert state["messages"][-1].text.startswith("**What happened**")
    assert state["tool_calls_used"] == 1

    records = [json.loads(line) for line in audit.path.read_text().splitlines()]
    assert records[0]["kind"] == "tool_call"
    assert records[0]["tool"] == "query_failed_events"
    assert records[0]["ok"] is True


def test_bad_arguments_go_back_to_the_model(merchant_down):
    bad_call = {"id": "c1", "name": "query_failed_events", "args": {"site_id": "acme"}}
    model = ScriptedModel(
        script=[AIMessage(content="", tool_calls=[bad_call]), AIMessage(content="done")]
    )
    graph = build_graph(model, build_mock_tools(merchant_down), max_tool_calls=15)

    state = graph.invoke(_turn("q"))

    tool_message = next(m for m in state["messages"] if isinstance(m, ToolMessage))
    assert not tool_message.artifact["ok"]
    assert "error" in json.loads(tool_message.content)
    assert state["messages"][-1].text == "done"


def test_unknown_tool_is_reported(merchant_down):
    call = {"id": "c1", "name": "drop_tables", "args": {}}
    model = ScriptedModel(script=[AIMessage(content="", tool_calls=[call]), AIMessage(content="ok")])
    graph = build_graph(model, build_mock_tools(merchant_down), max_tool_calls=15)

    state = graph.invoke(_turn("q"))

    tool_message = next(m for m in state["messages"] if isinstance(m, ToolMessage))
    assert "Unknown tool" in tool_message.artifact["summary"]


def test_tool_budget_is_enforced(merchant_down):
    model = ScriptedModel(
        script=[
            AIMessage(content="", tool_calls=[_call("c1"), _call("c2")]),
            AIMessage(content="final answer with what I have"),
        ]
    )
    graph = build_graph(model, build_mock_tools(merchant_down), max_tool_calls=1)

    state = graph.invoke(_turn("q"))

    results = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert [m.artifact["ok"] for m in results] == [True, False]
    assert "budget" in results[1].artifact["summary"]
    assert state["messages"][-1].text == "final answer with what I have"


def test_overloaded_primary_falls_back(merchant_down):
    # The case seen live: the tool call works, then the primary model is overloaded (504/503).
    primary = ScriptedModel(
        script=[AIMessage(content="", tool_calls=[_call("c1")]), ModelAPIError("504 DEADLINE_EXCEEDED")]
    )
    fallback = ScriptedModel(script=[AIMessage(content="answer from fallback")])
    graph = build_graph(
        primary, build_mock_tools(merchant_down), max_tool_calls=15, fallback_model=fallback
    )

    state = graph.invoke(_turn("q"))

    assert state["messages"][-1].text == "answer from fallback"
    assert sum(isinstance(m, ToolMessage) for m in state["messages"]) == 1


def test_non_transient_errors_do_not_fall_back(merchant_down):
    primary = ScriptedModel(script=[ValueError("bad request")])
    fallback = ScriptedModel(script=[AIMessage(content="should not be used")])
    graph = build_graph(
        primary, build_mock_tools(merchant_down), max_tool_calls=15, fallback_model=fallback
    )

    try:
        graph.invoke(_turn("q"))
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
    assert fallback.script  # untouched


def test_retry_resumes_without_rerunning_tools(merchant_down, tmp_path):
    model = ScriptedModel(
        script=[
            AIMessage(content="", tool_calls=[_call("c1")]),
            ModelAPIError("503 UNAVAILABLE"),
            AIMessage(content="answer after retry"),
        ]
    )
    audit = AuditLog(tmp_path, "s1")
    graph = build_graph(
        model,
        build_mock_tools(merchant_down),
        max_tool_calls=15,
        checkpointer=InMemorySaver(),
        audit=audit,
    )
    config = {"configurable": {"thread_id": "t1"}}

    try:
        graph.invoke(_turn("q"), config)
    except ModelAPIError:
        pass
    assert graph.get_state(config).next == ("agent",)

    state = graph.invoke(None, config)

    assert state["messages"][-1].text == "answer after retry"
    tool_calls_logged = [
        line for line in audit.path.read_text().splitlines() if '"tool_call"' in line
    ]
    assert len(tool_calls_logged) == 1
