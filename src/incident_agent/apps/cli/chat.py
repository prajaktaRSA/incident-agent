"""`chat`: interactive debugging chat over the same tools and mock data."""

import uuid
from datetime import UTC, datetime
from typing import Any

import typer
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from rich.markdown import Markdown
from rich.panel import Panel

from incident_agent.apps.cli.console import console
from incident_agent.audit import AuditLog
from incident_agent.backends.mock import available_scenarios, load_scenario
from incident_agent.chat.graph import build_graph
from incident_agent.llm import TRANSIENT_LLM_ERRORS, MissingApiKeyError, build_chat_models
from incident_agent.settings import Settings, get_settings
from incident_agent.tools import build_mock_tools

HELP = """\
[bold]/new[/]              start a fresh session
[bold]/scenario <name>[/]  switch mock scenario ({scenarios})
[bold]/tools[/]            list available tools
[bold]/trace[/]            tool calls made in this session
[bold]/retry[/]            retry the step that failed (keeps tool results already fetched)
[bold]/quit[/]             exit"""


class ChatSession:
    def __init__(self, settings: Settings, scenario_name: str, verbose: bool) -> None:
        self.settings = settings
        self.verbose = verbose
        self.models = build_chat_models(settings)
        self.start(scenario_name)

    def start(self, scenario_name: str) -> None:
        self.scenario = load_scenario(self.settings.fixtures_dir, scenario_name)
        self.tools = build_mock_tools(self.scenario)
        self.session_id = uuid.uuid4().hex[:8]
        self.audit = AuditLog(self.settings.logs_dir, self.session_id)
        self.audit.write("session_start", scenario=self.scenario.name, model=self.settings.gemini_model)
        self.graph = build_graph(
            self.models.primary,
            self.tools,
            max_tool_calls=self.settings.max_tool_calls_per_turn,
            fallback_model=self.models.fallback,
            checkpointer=InMemorySaver(),
            audit=self.audit,
        )
        self.config = {"configurable": {"thread_id": self.session_id}}
        self.trace: list[str] = []
        fallback = self.settings.gemini_fallback_model or "none"
        console.print(
            f"[dim]incident-agent · {self.settings.gemini_model} (fallback: {fallback}) · "
            f"scenario: {self.scenario.name} · session {self.session_id}[/]"
        )

    def ask(self, text: str) -> None:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.audit.write("user_message", text=text)
        turn = {"messages": [HumanMessage(f"[current time: {now}]\n{text}")], "tool_calls_used": 0}
        self._run(turn)

    def retry(self) -> None:
        """Resume the graph at the step that failed; tool results already fetched are kept."""
        if not self.graph.get_state(self.config).next:
            console.print("[dim]Nothing to retry.[/]")
            return
        self.audit.write("retry")
        self._run(None)

    def _run(self, turn: dict[str, Any] | None) -> None:
        pending_calls: dict[str, dict[str, Any]] = {}
        answer: AIMessage | None = None

        with console.status("[dim]thinking…[/]", spinner="dots") as status:
            for update in self.graph.stream(turn, self.config, stream_mode="updates"):
                for node, payload in update.items():
                    for message in payload.get("messages", []):
                        if isinstance(message, AIMessage):
                            if message.tool_calls:
                                pending_calls |= {c["id"]: c for c in message.tool_calls}
                                status.update("[dim]running tools…[/]")
                            else:
                                answer = message
                        elif isinstance(message, ToolMessage):
                            self._print_tool_result(message, pending_calls.get(message.tool_call_id))
                            status.update("[dim]thinking…[/]")

        if answer is None or not answer.text.strip():
            console.print("[yellow]The model returned no answer.[/]")
            return
        model_name = answer.response_metadata.get("model_name") or "unknown model"
        self.audit.write("answer", model=model_name, text=answer.text)
        console.print(
            Panel(
                Markdown(answer.text),
                title="Findings",
                subtitle=f"[dim]{model_name}[/]",
                border_style="cyan",
            )
        )

    def _print_tool_result(self, message: ToolMessage, call: dict[str, Any] | None) -> None:
        artifact = message.artifact or {}
        args = ", ".join(f"{k}={v}" for k, v in (call or {}).get("args", {}).items())
        mark = "[green]✓[/]" if artifact.get("ok") else "[red]✗[/]"
        line = (
            f"  [cyan]▸[/] {message.name}([dim]{args}[/]) "
            f"[dim]{artifact.get('duration_ms', '?')}ms[/] {mark} {artifact.get('summary', '')}"
        )
        self.trace.append(line)
        console.print(line)
        if self.verbose:
            console.print_json(message.content if isinstance(message.content, str) else "{}")


def chat(
    scenario: str = typer.Option(None, help="Mock scenario to load (default: MOCK_SCENARIO)."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Print full tool outputs."),
) -> None:
    """Start an interactive debugging chat."""
    settings = get_settings()
    try:
        session = ChatSession(settings, scenario or settings.mock_scenario, verbose)
    except (MissingApiKeyError, FileNotFoundError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc
    console.print("[dim]Type a question, or /help for commands.[/]")

    while True:
        try:
            text = console.input("[bold green]you ›[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if not text:
            continue
        if text.startswith("/"):
            command, _, arg = text.partition(" ")
            if command in ("/quit", "/exit"):
                break
            elif command == "/help":
                scenarios = ", ".join(available_scenarios(settings.fixtures_dir))
                console.print(HELP.format(scenarios=scenarios))
            elif command == "/new":
                session.start(session.scenario.name)
            elif command == "/scenario":
                try:
                    session.start(arg.strip())
                except FileNotFoundError as exc:
                    console.print(f"[red]{exc}[/]")
            elif command == "/tools":
                for tool in session.tools:
                    console.print(f"  [cyan]{tool.name}[/] — {tool.description.splitlines()[0]}")
            elif command == "/trace":
                for line in session.trace or ["  [dim]no tool calls yet[/]"]:
                    console.print(line)
            elif command == "/retry":
                _guarded(session.retry)
            else:
                console.print(f"[yellow]Unknown command {command}. Try /help.[/]")
            continue
        _guarded(lambda: session.ask(text))


def _guarded(action) -> None:
    """Run one turn; LLM and API errors are reported without ending the session."""
    try:
        action()
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted. Type /retry to continue from where it stopped.[/]")
    except TRANSIENT_LLM_ERRORS as exc:
        console.print(
            "[yellow]The model didn't respond (overloaded, rate-limited or timed out), and the "
            "fallback failed too. Type /retry to continue from where it stopped, or try again "
            f"later.[/]\n[dim]{type(exc).__name__}: {exc}[/]"
        )
    except Exception as exc:
        console.print(f"[red]Error: {type(exc).__name__}: {exc}[/]")
