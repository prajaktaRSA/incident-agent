# incident-agent

A LangGraph agent that helps on-call engineers investigate webhook delivery failures, latency
spikes and traffic anomalies through a terminal chat. See [PLAN.md](PLAN.md) for the design.

**Status: M0.** One tool (`query_failed_events`) on mocked data, Gemini as the LLM.

## Setup

```bash
uv sync
cp .env.example .env   # then paste your Gemini API key into GOOGLE_API_KEY
uv run incident-agent models   # confirm GEMINI_MODEL is available to your key
```

## Use

```bash
uv run incident-agent chat --scenario merchant_down
```

Try: `webhooks for acme started failing about an hour and a half ago, what happened?`

Commands inside the chat: `/new`, `/scenario <name>`, `/tools`, `/trace`, `/retry`, `/help`, `/quit`.

If the model is overloaded or times out, the agent switches to `GEMINI_FALLBACK_MODEL`. If that
fails too, `/retry` resumes from the failed step without re-running tools.
Each session writes an audit log to `logs/<session-id>.jsonl`.

## Test

```bash
uv run pytest
```
