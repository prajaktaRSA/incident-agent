"""Command line: `investigate` (trigger-driven RCA), `chat` (debugging) and `models`."""

import typer

from incident_agent.apps.cli.chat import chat
from incident_agent.apps.cli.investigate import investigate
from incident_agent.apps.cli.list_models import list_models

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Incident investigation agent.")
app.command()(investigate)
app.command()(chat)
app.command(name="models")(list_models)

__all__ = ["app"]
