"""Chat model factory. The provider is a config switch so it can change without code changes."""

from dataclasses import dataclass

import httpx
from langchain_core.exceptions import (
    ModelAPIError,
    ModelConnectionError,
    ModelRateLimitError,
    ModelTimeoutError,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable

from incident_agent.settings import Settings

# Failures worth switching to the fallback model for: provider overload (5xx), rate limits,
# timeouts and dropped connections. Bad requests and auth errors are not retried.
TRANSIENT_LLM_ERRORS: tuple[type[Exception], ...] = (
    ModelAPIError,
    ModelRateLimitError,
    ModelTimeoutError,
    ModelConnectionError,
    httpx.TimeoutException,
)


class MissingApiKeyError(RuntimeError):
    pass


@dataclass
class ChatModels:
    primary: BaseChatModel
    fallback: BaseChatModel | None


def build_chat_models(settings: Settings) -> ChatModels:
    if settings.llm_provider == "gemini":
        if settings.google_api_key is None:
            raise MissingApiKeyError(
                "GOOGLE_API_KEY is not set. Copy .env.example to .env and paste your Gemini API key."
            )
        fallback = settings.gemini_fallback_model
        return ChatModels(
            primary=_gemini(settings, settings.gemini_model),
            fallback=_gemini(settings, fallback) if fallback else None,
        )
    raise ValueError(f"Unsupported LLM provider: {settings.llm_provider}")


def with_fallback(primary: Runnable, fallback: Runnable | None) -> Runnable:
    """Use `fallback` when `primary` fails with a transient error."""
    if fallback is None:
        return primary
    return primary.with_fallbacks([fallback], exceptions_to_handle=TRANSIENT_LLM_ERRORS)


def _gemini(settings: Settings, model: str) -> BaseChatModel:
    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(
        model=model,
        api_key=settings.google_api_key,
        temperature=0,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )
