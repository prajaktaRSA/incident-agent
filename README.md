# incident-agent

An automated investigator for webhook delivery problems. A trigger (a solutions engineer's Slack
workflow form, or a CloudWatch alarm) starts an investigation: code picks and runs the checks,
rules rank the likely causes, and one LLM call writes an internal RCA for the Slack thread.
See [PLAN.md](PLAN.md) for the design.

**Status: M2.** The full trigger pipeline runs on mocked data with all eight tools, twelve
triage rules, runbook search and an optional extra-checks round. Nine scenarios in
`fixtures/scenarios/` (layered on `_base.yaml`), with matching triggers in `fixtures/triggers/`.

## Setup

```bash
uv sync
cp .env.example .env   # then paste your Gemini API key into GOOGLE_API_KEY
uv run incident-agent models   # confirm GEMINI_MODEL is available to your key
```

## Investigate a trigger

```bash
uv run incident-agent investigate --trigger fixtures/triggers/merchant_down.json
```

Trigger fixtures live in `fixtures/triggers/` (times like `"-95m"` are relative to now). The mock
data comes from `fixtures/scenarios/<name>.yaml`, matched by file name or `--scenario`.

The RCA that would be posted to Slack is written to `outbox/<id>.md`, the full investigation
(request, evidence, hypotheses, RCA) to `records/<id>.json`, and every step to `logs/<id>.jsonl`.
If the form is missing details, the fixed follow-up questions go to the outbox instead.

## Debug chat

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
