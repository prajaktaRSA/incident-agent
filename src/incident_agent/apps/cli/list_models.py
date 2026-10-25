"""`models`: list the Gemini models an API key can use."""

import typer

from incident_agent.apps.cli.console import console
from incident_agent.settings import get_settings


def list_models() -> None:
    """List Gemini models available to your API key (to pick GEMINI_MODEL)."""
    settings = get_settings()
    if settings.google_api_key is None:
        console.print("[red]GOOGLE_API_KEY is not set. Add it to .env first.[/]")
        raise typer.Exit(1)
    from google import genai

    client = genai.Client(api_key=settings.google_api_key.get_secret_value())
    for model in client.models.list():
        if "generateContent" in (model.supported_actions or []) and "flash" in (model.name or ""):
            marker = ""
            if model.name.endswith(f"/{settings.gemini_model}"):
                marker = " [green]← configured[/]"
            elif settings.gemini_fallback_model and model.name.endswith(
                f"/{settings.gemini_fallback_model}"
            ):
                marker = " [green]← fallback[/]"
            console.print(f"  {model.name}  [dim]{model.display_name}[/]{marker}")
