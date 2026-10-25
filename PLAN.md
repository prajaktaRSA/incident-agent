# Incident Agent — MVP Plan

A Python AI agent, built with LangGraph, that helps an on-call engineer investigate
webhook delivery failures, latency spikes and traffic anomalies by querying production
metrics, failed/DLQ events, deployments, runbooks and Splunk logs through an
interactive chat.

> **Status:** plan under review. No code yet. **The MVP runs entirely on mocked data.**

---

## 1. Goal and scope

**The MVP is** a terminal chat. The engineer asks something like:

> "Webhooks for site `acme` started failing around 14:00 — what happened?"

The agent investigates with read-only tools and answers with:

- **What happened**: the symptom, its scope (one site, one endpoint, or everyone) and the time window
- **Evidence**: concrete numbers from each tool it called
- **Likely cause** and **confidence** (high / medium / low)
- **Next steps**, taken from the matching runbook

Follow-up questions keep their context ("only the 503s", "compare with yesterday").

**Not in the MVP:** write or fix actions, triggering from alerts, a Slack bot, a web UI,
multiple cooperating agents, or real backends. Every backend is mocked.

---

## 2. The system being investigated

```
 events-service (example-org/events-service)            webhook-service (example-org/webhook-service)
┌──────────────────────────────────┐                  ┌──────────────────────────────────────────┐
│ MySQL                            │  MSK (Kafka)     │ webhook-delivery-cg                      │
│  webhooks      (per site_id,     │ ───────────────▶ │  consume event                           │
│                 1 row / endpoint)│  domain topics   │  → Redis webhook_config:{site_id} (TTL 5s)│
│  event_filters (endpoint →       │                  │      miss → internal API → cache JSON    │
│                 subscribed types)│ ◀── internal ─── │  → filter by subscribed event types      │
│                                  │    config API    │  → POST to merchant endpoint             │
│ Producer: event type → topic     │   (Basic auth +  │  → on failure:                           │
│ (explicit mapping in code)       │    caller header)│      webhook-failed-events (retry ≤ 7)   │
└──────────────────────────────────┘                  │      webhook-retry-events → retry-cg     │
                                                      │      429 → webhook-ratelimited-events    │
                                                      │      retries exhausted → webhook-dlq-events│
                                                      │      config fetch failure → DLQ directly │
                                                      └──────────────────────────────────────────┘
 Deploys: GitHub Actions (.github/workflows/deploy-ecs.yml) → ECS cluster platform-stg, on merge to main
```

Key facts:

- The `webhooks` table has **no disabled column**. A dead endpoint keeps getting attempts, so the
  agent has to infer a dead endpoint from the failure pattern.
- The Redis TTL is **5 seconds**, so a stale cache can't explain an incident and the agent doesn't
  read Redis. The short TTL means the internal config API is called very often. **If that API is slow
  or failing, deliveries fail even though the merchant's endpoint is healthy.**
- Config-fetch failures go **straight to the DLQ** as event-level rows.

---

## 3. Agent design (LangGraph)

```
            ┌──────────────┐
 user ────▶ │  agent (LLM) │ ◀───────────────┐
            └──────┬───────┘                 │
                   │ tool calls?             │ tool results
           yes ┌───┴───┐ no                  │
               ▼       ▼                     │
        ┌───────────┐  ┌──────────────┐      │
        │ tools node│  │ final answer │      │
        └─────┬─────┘  └──────────────┘      │
              └──────────────────────────────┘
```

- A small **custom `StateGraph`** with an agent node, a tools node and a conditional edge. It's custom
  rather than prebuilt so we have places to enforce limits and write the audit log.
- **State:** `messages`, `site_id`, `window_start`, `window_end`, `evidence[]`, `tool_calls_used`.
- **Checkpointer:** `MemorySaver` (in-memory) so follow-ups work within a session.
- **Limits:** at most 15 tool calls per user turn, a per-tool timeout, and a cap on the size of each
  tool's output.
- **Current time:** the current UTC time is added to each user turn so relative times like
  "since 14:00" can be resolved.
- **System prompt (`prompts.py`)** describes how to investigate:
  1. Pin down the site, the time window and the symptom: failures or latency.
  2. Check the config: endpoints, and whether the endpoint is subscribed to the event type.
  3. Look at failed and DLQ events grouped by status. Is it one endpoint or all of them? Is it a
     config-fetch failure?
  4. Look at metrics: consumer lag, delivery latency and errors, config API health.
  5. Line the timing up with recent deployments.
  6. Search Splunk logs for the "why".
  7. Find the matching runbook.
  8. Answer in the report format. Never claim a root cause without evidence; say what is unknown.
- **How to read the signals** (part of the prompt):

  | Pattern | Likely meaning |
  |---|---|
  | One endpoint, 5xx or timeouts, others healthy | Merchant-side outage |
  | Many tenants failing right after a webhook-service deploy | Regression in the deploy |
  | DLQ `CONFIG_FETCH_ERROR` (401/5xx) across tenants | Internal config API or auth problem |
  | High offset lag, low time lag | Traffic burst; consumers keeping up |
  | Time lag rising steadily | Consumers slow or stuck (slow endpoints, config API latency, deploy) |
  | Lag on all topics | webhook-service-wide problem |
  | Lag on one topic | Burst or problem specific to that topic |
  | 429s plus `webhook-ratelimit-cg` lag | Merchant is rate-limiting us |
  | "Didn't receive event X", no failures | Endpoint not subscribed to X (check `event_filters`) |

### Terminal chat (the MVP interface)

`typer` handles commands and `rich` handles rendering. Start it with:

```bash
uv run incident-agent chat --scenario merchant_down
```

```
incident-agent · gemini-flash-latest · scenario: merchant_down · session 3f9c
you › webhooks for acme started failing around 14:00, what happened?
  ▸ get_webhook_config(site_id=acme)                          120ms ✓
  ▸ query_failed_events(acme, 13:30–now, both)                 85ms ✓  412 failed · 37 DLQ
  ▸ get_cloudwatch_metric(kafka.time_lag, webhook-delivery-cg)  60ms ✓  no alarm
  ▸ get_recent_deployments(since 12:00)                        40ms ✓  none
  ▸ search_splunk_logs(webhook_service, acme, level=ERROR)      90ms ✓  50 lines
  ▸ search_runbooks("merchant endpoint 503")                    15ms ✓
╭─ Findings ───────────────────────────────────────────────────────────────╮
│ What happened · Evidence · Likely cause (confidence) · Next steps        │
╰──────────────────────────────────────────────────────────────────────────╯
you › only the 503s from the last 30 minutes
```

- Tool calls appear live, one line each, with their inputs, timing and a one-line summary.
  `--verbose` shows the full tool output.
- The final answer is a rich panel using the report format from §1.
- Slash commands: `/new` (fresh session), `/scenario <name>` (switch mock data), `/tools` (list
  tools), `/trace` (tool calls so far), `/quit`.
- Every session writes `logs/<session-id>.jsonl` with each message and tool call (the audit log).

---

## 4. LLM

- **Provider:** Google Gemini, using the user's API key, through `langchain-google-genai`
  (`ChatGoogleGenerativeAI`).
- **Model:** Gemini Flash. The model ID is a config value (`GEMINI_MODEL`, defaulting to
  `gemini-flash-latest`). **M0 checks the exact ID** by listing the models available to the key.
- **The provider can be swapped:** `LLM_PROVIDER=gemini|groq|ollama|anthropic|bedrock` maps to the
  matching LangChain chat model. Gemini is the only one implemented in the MVP; the others are a
  one-line change later.
- **Data rule:** the Gemini API is acceptable **with mock data only**. Before any real production
  data is used, switch to a provider that meets compliance requirements, e.g. Claude through Amazon
  Bedrock in `us-east-1`, which keeps data in the AWS account.
- **Secrets:** `GOOGLE_API_KEY` lives in `.env`, which is git-ignored, and is never committed or logged.

---

## 5. Tools

These rules apply to every tool:

- **Read-only.**
- **Inputs are typed** (Pydantic) and validated.
- **Time is ISO-8601 UTC**, and every tool has a maximum time window.
- **Outputs are compact summaries, never raw dumps**, each with a size cap.
- **Errors come back as structured tool results** rather than exceptions.
- **Secrets are stripped** from all outputs.
- **Every tool call is written to the audit log.**

Each tool calls a backend **interface**: `MockBackend` now, `RealBackend` later.

| # | Tool | Inputs | Returns |
|---|---|---|---|
| 1 | `get_webhook_config` | `site_id` | Endpoints: `destinationId`, URL host only, subscribed event types (from `event_filters`), retry policy. Signing secrets are removed. |
| 2 | `query_failed_events` | `site_id, start, end, source=failed\|dlq\|both` | Counts by `failureStatusCode`, by `destinationId` and by event type; `retryAttempts` distribution; first and last failure; 3–5 sample `failureMessage`s per status; DLQ `CONFIG_FETCH_ERROR` counted separately. **`eventContent` is never returned**; only its `type` field is read. |
| 3 | `get_cloudwatch_metric` | `metric` (allowlisted key), `dimensions, start, end, period, stat` | Min/max/avg/p99, detected spikes, a downsampled series, **plus the alarms on that metric (thresholds) and any state changes in the window.** |
| 4 | `get_recent_deployments` | `service?, since` | GitHub Actions deploy runs on `main`: time, commit, PR title/author/link, outcome. Optionally the ECS rollout state (completed, failed, rolled back). |
| 5 | `search_runbooks` | `query` | Top 3 sections (`docs/**/*.md` split by heading, ranked with BM25): repo, path, heading, section text, link. |
| 6 | `search_splunk_logs` | `index, start, end, tenant_id?, level?, text?, limit≤100` | Matching log lines (secrets stripped) plus a field summary (counts by level, status, destination). **The model passes structured inputs and the tool builds the Splunk query itself.** Indexes come from a fixed list, the window is at most 24h, and results are capped. |

---

## 6. Mock environment

All values below are made up for the MVP and get replaced with real values later.

### AWS
- Region: `us-east-1`. ECS cluster: `platform-stg`. Services: `events-service`, `webhook-service`.

### MSK
- Cluster: `events-msk-stg`
- Topics and the producer's event type → topic mapping (stored in `config/topic_mapping.yaml`):

  | Topic | Event types |
  |---|---|
  | `subscription-events` | `subscription.created`, `.updated`, `.renewed`, `.cancelled` |
  | `payment-events` | `payment.succeeded`, `.failed`, `.refunded` |
  | `invoice-events` | `invoice.created`, `.paid`, `.overdue` |
  | `customer-events` | `customer.created`, `.updated`, `.deleted` |
  | `webhook-retry-events` | failed deliveries waiting to be retried |
  | `webhook-ratelimited-events` | deliveries held back by rate limiting |

- Consumer groups: `webhook-delivery-cg` (the 4 domain topics), `webhook-retry-cg`,
  `webhook-ratelimit-cg`.

### CloudWatch metric allowlist (`config/metrics.yaml`)

| Key | Namespace / metric | Dimensions | Alarms (warn / crit) |
|---|---|---|---|
| `kafka.max_offset_lag` | `AWS/Kafka` `MaxOffsetLag` | Cluster Name, Consumer Group, Topic | 500 / 2000 messages |
| `kafka.sum_offset_lag` | `AWS/Kafka` `SumOffsetLag` | Cluster Name, Consumer Group, Topic | 2000 / 10000 *(assumed)* |
| `kafka.time_lag` | `AWS/Kafka` `EstimatedMaxTimeLag` | Cluster Name, Consumer Group, Topic | 30 / 120 seconds |
| `webhook.delivery_attempts` | `WebhookService` `DeliveryAttempts` | Environment | — |
| `webhook.delivery_failures` | `WebhookService` `DeliveryFailures` | Environment, StatusClass (4xx/5xx/timeout) | *(assumed)* |
| `webhook.delivery_latency` | `WebhookService` `DeliveryLatencyMs` (p50/p99) | Environment | *(assumed)* |
| `webhook.config_fetch_errors` | `WebhookService` `ConfigFetchErrors` | Environment, StatusCode | *(assumed)* |
| `config_api.errors` | `EventsService` `WebhookConfigApi5xx` | Environment | *(assumed)* |
| `config_api.latency` | `EventsService` `WebhookConfigApiLatencyMs` (p99) | Environment | *(assumed)* |
| `ecs.cpu` / `ecs.memory` | `AWS/ECS` `CPUUtilization` / `MemoryUtilization` | ClusterName, ServiceName | — |

Alarm thresholds are read from CloudWatch alarms (mocked here), not hard-coded in the agent.

### DynamoDB tables
- `webhook-failed-events` and `webhook-dlq-events`
- Key: `eventId` (partition) + `destinationKey` (sort), where `destinationKey` is
  `<destinationId>:<DESTINATION_TYPE>`
- Mock index: `tenant_id-failedAt-index` (failed table) and `tenant_id-created_at-index` (DLQ table).
  **In production these indexes don't exist yet** (see §9).
- Timestamps are epoch milliseconds. Max retries: **7**.

Failed table item:
```json
{ "eventId":"gf_tirtdjrgs948w", "destinationKey":"wg_3532:WEBHOOK", "tenant_id":"acme",
  "destinationId":"wg_3532", "destinationType":"WEBHOOK", "failedAt":1716135675647,
  "retryAttempts":3, "failureStatusCode":503, "failureMessage":"Service Unavailable",
  "eventContent":"{...serialized event...}", "created_at":1716135675647, "updatedAt":1716136964765 }
```

DLQ items (one per failure kind):
```json
{ "eventId":"evt_8k2lq9", "destinationKey":"wg_3532:WEBHOOK", "tenant_id":"acme",
  "destinationId":"wg_3532", "destinationType":"WEBHOOK",
  "failureType":"DELIVERY_ERROR", "dlqReason":"MAX_RETRIES_EXCEEDED",
  "retryAttempts":7, "failureStatusCode":503, "failureMessage":"Service Unavailable",
  "eventContent":"{...}", "created_at":1716135675647, "updatedAt":1716136964765 }

{ "eventId":"evt_9p1zx4", "destinationKey":"NONE:CONFIG_FETCH", "tenant_id":"acme",
  "destinationId":null, "destinationType":"CONFIG_FETCH",
  "failureType":"CONFIG_FETCH_ERROR", "dlqReason":"CONFIG_UNAVAILABLE",
  "retryAttempts":0, "failureStatusCode":401,
  "failureMessage":"webhook-config API returned 401 Unauthorized",
  "eventContent":"{...}", "created_at":1716135675647, "updatedAt":1716135675647 }
```

### Internal webhook-config API (mocked)
- `GET /internal/sites/{site_id}/webhooks` returns endpoints along with their `event_filters`.
- Auth: Basic auth with the agent's own pre-shared key, plus a caller-name header
  (`incident-agent`). The real client will fetch the key from Secrets Manager, keep it in memory,
  and on a 401 fetch it again once and retry.

### GitHub
- Repos: `example-org/events-service`, `example-org/webhook-service`
- Deploy workflow: `.github/workflows/deploy-ecs.yml` ("Deploy to ECS"), triggered on merge to `main`

### Splunk
- Indexes: `webhook_service` (delivery logs), `events_service` (producer and config API logs)
- Log fields: `_time, level, service, tenant_id, eventId, destinationId, status, latency_ms, msg, trace_id`

### Runbooks (mock `docs/` content, deliberately in mixed formats)
- `webhook-service/docs/merchant-endpoint-5xx.md`
- `webhook-service/docs/consumer-lag.md`
- `events-service/docs/webhook-config-api-errors.md`
- `webhook-service/docs/rollback-a-bad-deploy.md`
- `webhook-service/docs/merchant-rate-limiting.md`

### Mock scenarios (these double as evals)
Each scenario is a fixture set, picked with `MOCK_SCENARIO=<name>`. Its timestamps are relative to
"now", so time-window queries always hit data.

| Scenario | Setup | Expected conclusion |
|---|---|---|
| `merchant_down` | `acme` / `wg_3532`: 503s since T; other tenants healthy; no deploy | Merchant-side outage; contact merchant |
| `bad_deploy` | `webhook-service` deployed at T; timeouts and 5xx across many tenants; time lag rising | Regression from that deploy (PR linked); roll back |
| `config_api_auth` | `events-service` deployed (key rotation) at T; DLQ `CONFIG_FETCH_ERROR` 401s across tenants | Config API auth broken; fix key or caller config |
| `traffic_burst` | `payment-events` offset lag spike, time lag low, no failures | Burst that is draining; no action |
| `rate_limited` | `acme` returning 429s; `webhook-ratelimit-cg` lag | Merchant rate-limiting; follow the runbook |
| `not_subscribed` | "acme didn't get `invoice.paid`"; no failures | Endpoint isn't subscribed to `invoice.paid` |

---

## 7. Project layout

```
incident-agent/
  pyproject.toml              # uv, Python 3.12
  .env.example                # GOOGLE_API_KEY=, LLM_PROVIDER=gemini, GEMINI_MODEL=, MOCK_SCENARIO=
  config/
    metrics.yaml  topic_mapping.yaml  splunk.yaml
  src/incident_agent/
    settings.py               # pydantic-settings
    llm.py                    # provider factory
    state.py  graph.py  prompts.py
    tools/                    # one module per tool (schema + formatting + guardrails)
    backends/
      base.py                 # interfaces
      mock/                   # in-memory backends + scenario loader
    audit.py                  # JSONL audit log of every tool call
    cli.py                    # typer + rich chat REPL
  fixtures/scenarios/<name>/  # config, failed/dlq items, metrics, deploys, logs
  fixtures/runbooks/          # mock docs/ markdown
  tests/                      # unit tests for tools, guardrails, graph routing
  evals/run_evals.py          # runs every scenario, checks conclusions
```

Main dependencies: `langgraph`, `langchain-core`, `langchain-google-genai`, `pydantic`,
`pydantic-settings`, `typer`, `rich`, `rank-bm25`, `pyyaml`, `pytest`.

---

## 8. Milestones

| # | Deliverable | Done when |
|---|---|---|
| **M0** | Scaffold with uv, settings, Gemini connection (model ID checked), graph, CLI, `query_failed_events` + mock backend, `merchant_down` scenario | You can chat about `acme` and get failures grouped by status |
| **M1** | All 6 tools with mock backends, the full system prompt, audit log, limits | `merchant_down` and `bad_deploy` are diagnosed correctly end to end |
| **M2** | All 6 scenarios, eval runner, unit tests | Every scenario reaches the expected conclusion; tests pass |
| **M3** *(post-MVP)* | Real backends (boto3, GitHub API, Splunk REST, internal API client), staging | Investigates a real staging incident |
| **M4** *(post-MVP)* | Compliant LLM (e.g. Bedrock), LangSmith tracing, Slack interface | Ready for on-call use |

---

## 9. Before using real data (post-MVP prerequisites)

1. **DynamoDB indexes** on `tenant_id` + time for both tables. Owners: webhook-service. Until they
   exist, only a bounded `Scan` is possible, and only in staging.
2. **IAM role `incident-agent-readonly`** in staging, assumed through AWS SSO:
   - `dynamodb:Query` / `GetItem` on the two tables and their indexes
   - `cloudwatch:GetMetricData`, `DescribeAlarmsForMetric`, `DescribeAlarmHistory`
   - `secretsmanager:GetSecretValue` on the agent's own key only
   - `ecs:DescribeServices`
3. **Network:** the agent runs on a laptop over VPN. If the internal API is only reachable inside the
   VPC, use SSM Session Manager port forwarding.
4. **Agent identity:** its own pre-shared key and caller name for the internal API.
5. **GitHub:** a fine-grained read-only token (Actions, Contents, Pull requests) on the two repos.
6. **Splunk:** an API token with a read-only role limited to the allowed indexes.
7. **LLM:** a provider approved for production data.

---

## 10. Assumptions (to confirm)

- The `SumOffsetLag` thresholds and every `WebhookService` / `EventsService` metric name are made up.
  That's fine for the mocked MVP. Before M3, replace them with real values in `config/metrics.yaml`
  (a config change, not a code change). How to find the real ones:
  1. **Namespaces:** `aws cloudwatch list-metrics --region us-east-1 --query 'Metrics[].Namespace' --output text | tr '\t' '\n' | sort -u`
  2. **Metrics in a namespace:** `aws cloudwatch list-metrics --region us-east-1 --namespace <ns>`
  3. **Alarm thresholds:** `aws cloudwatch describe-alarms-for-metric --region us-east-1 --namespace AWS/Kafka --metric-name SumOffsetLag --dimensions ...`
  4. **Code and infrastructure:** search the service repos for where metrics are emitted
     (`putMetricData`, EMF, Micrometer/StatsD) and the IaC for alarms (`aws_cloudwatch_metric_alarm`,
     CDK `Alarm`).
  5. **Dashboards:** `aws cloudwatch list-dashboards` shows what on-call already watches.
  6. If the service metrics live outside CloudWatch (Datadog, Prometheus), add a separate tool for them.

  In real mode, alarm thresholds are read from CloudWatch at runtime, so only metric names and
  dimensions need to be correct in config.

## 11. Decisions

- Interface: **terminal chat** (LangGraph Studio is optional for debugging).
- LLM: **Gemini Flash**, with every backend **mocked** for the MVP.
- Splunk: **in the MVP**.
- Region: **`us-east-1`**.
- `tenant_id` **is** `site_id` (same value; the tables use `tenant_id`).
- The event type field inside `eventContent` is **`type`**.
- Retries and rate-limited deliveries each have **their own Kafka topic and consumer group**.
