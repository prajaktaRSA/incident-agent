"""`investigate`: run the trigger-driven pipeline on one trigger fixture."""

import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from rich.markdown import Markdown
from rich.panel import Panel

from incident_agent.apps.cli.console import console
from incident_agent.audit import AuditLog
from incident_agent.backends.mock import Scenario, load_scenario
from incident_agent.backends.mock.triggers import load_trigger
from incident_agent.config import load_topic_mapping
from incident_agent.intake import intake
from incident_agent.investigation.graph import build_investigation_graph
from incident_agent.investigation.models import Evidence
from incident_agent.investigation.publish import format_questions_message, write_outbox
from incident_agent.investigation.rca import LlmRcaWriter
from incident_agent.llm import MissingApiKeyError, build_chat_models
from incident_agent.settings import get_settings
from incident_agent.tools import build_mock_tools


def investigate(
    trigger: Path = typer.Option(
        ..., "--trigger", "-t", exists=True, dir_okay=False, help="Trigger JSON, e.g. fixtures/triggers/merchant_down.json."
    ),
    scenario: str = typer.Option(
        None, help="Mock data scenario (default: the trigger file's name)."
    ),
) -> None:
    """Investigate one trigger and write the internal RCA to outbox/."""
    settings = get_settings()
    now = datetime.now(UTC)
    result = intake(load_trigger(trigger, now), settings, now)

    if result.ignored:
        console.print(f"[dim]Ignored: {result.ignored}[/]")
        return
    if result.questions:
        text = format_questions_message(result)
        path = write_outbox(settings.outbox_dir, f"questions-{uuid.uuid4().hex[:8]}", result.reply_to, text)
        console.print(Panel(Markdown(text), title="Needs more information", border_style="yellow"))
        console.print(f"[dim]outbox: {path}[/]")
        return

    request = result.request
    assert request is not None
    scenario_name = scenario or trigger.stem
    try:
        data = load_scenario(settings.fixtures_dir, scenario_name, now_ms=int(now.timestamp() * 1000))
    except FileNotFoundError:
        console.print(f"[yellow]No mock data for scenario '{scenario_name}'; data sources will be empty.[/]")
        data = Scenario(name=scenario_name, description="")
    try:
        models = build_chat_models(settings)
    except MissingApiKeyError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from exc

    audit = AuditLog(settings.logs_dir, request.id)
    audit.write("trigger", request=request.model_dump(mode="json"))
    graph = build_investigation_graph(
        tools=build_mock_tools(data),
        rca_writer=LlmRcaWriter(models),
        topics=load_topic_mapping(settings.config_dir),
        settings=settings,
        audit=audit,
    )
    subject = request.site_id or (request.alarm.name if request.alarm else "service")
    console.print(
        f"[dim]investigation {request.id} · {request.source} · {subject} · {request.issue_type} · "
        f"window {request.window_start:%H:%M}–{request.window_end:%H:%M} UTC · scenario {scenario_name}[/]"
    )

    state: dict[str, Any] = {}
    with console.status("[dim]selecting checks…[/]", spinner="dots") as status:
        updates = graph.stream({"request": request, "started_at": time.perf_counter()}, stream_mode="updates")
        for update in updates:
            for node, payload in update.items():
                state.update(payload)
                if node == "select_playbook":
                    status.update(f"[dim]running {len(payload['checks'])} checks…[/]")
                elif node == "gather":
                    for evidence in payload["evidence"]:
                        console.print(_evidence_line(evidence))
                    status.update("[dim]ranking causes…[/]")
                elif node == "triage":
                    for h in payload["hypotheses"]:
                        console.print(f"  [magenta]◆[/] {h.category} [dim]score {h.score:.2f}[/]")
                    status.update("[dim]writing RCA…[/]")
                elif node == "write_rca":
                    status.update("[dim]publishing…[/]")

    outcome = state["outcome"]
    console.print(
        Panel(
            Markdown(state["message"]),
            title="Automated RCA — internal",
            subtitle=f"[dim]→ {request.reply_to}[/]",
            border_style="yellow" if outcome.deterministic else "cyan",
        )
    )
    console.print(f"[dim]outbox: {state['outbox_path']}\nrecord: {state['record_path']}[/]")


def _evidence_line(evidence: Evidence) -> str:
    marks = {"ok": "[green]✓[/]", "error": "[red]✗[/]", "timeout": "[red]⏱[/]", "skipped": "[dim]–[/]"}
    duration = f"{evidence.duration_ms}ms " if evidence.duration_ms is not None else ""
    style = "dim" if evidence.status == "skipped" else ""
    text = f"  [cyan]▸[/] {evidence.check} [dim]{duration}[/]{marks[evidence.status]} {evidence.summary}"
    return f"[{style}]{text}[/]" if style else text
