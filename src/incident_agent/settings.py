"""Runtime settings, read from environment variables and `.env`."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    llm_provider: Literal["gemini"] = "gemini"
    # Pinned rather than `gemini-flash-latest`: the alias tracks the newest model, which is
    # often overloaded (calls of several minutes and 503 "high demand" errors).
    gemini_model: str = "gemini-3.5-flash"
    # Used when the primary model is overloaded, rate-limited or times out. Empty disables it.
    gemini_fallback_model: str = "gemini-3.1-flash-lite"
    llm_timeout_seconds: float = 60
    # Total attempts per call, including the first.
    llm_max_retries: int = 2
    # The Gemini SDK also reads GOOGLE_API_KEY / GEMINI_API_KEY itself; we read it here so a
    # missing key fails fast with a clear message instead of deep inside the first request.
    google_api_key: SecretStr | None = Field(default=None, alias="GOOGLE_API_KEY")

    mock_scenario: str = "merchant_down"
    config_dir: Path = PROJECT_ROOT / "config"
    fixtures_dir: Path = PROJECT_ROOT / "fixtures"
    logs_dir: Path = PROJECT_ROOT / "logs"
    outbox_dir: Path = PROJECT_ROOT / "outbox"
    records_dir: Path = PROJECT_ROOT / "records"

    max_tool_calls_per_turn: int = 15
    check_timeout_seconds: float = 20
    # Channel for RCAs triggered by alarms (form-triggered RCAs reply in the form's thread).
    incidents_channel: str = "#webhook-incidents"


@lru_cache
def get_settings() -> Settings:
    return Settings()
