# Incident Agent — MVP Plan

An automated investigator for the webhook delivery platform. When a solutions engineer reports a
webhook problem through a Slack workflow, or a CloudWatch alarm fires, the agent collects evidence
from metrics, failed/DLQ events, deployments, service logs (Splunk or CloudWatch Logs) and runbooks, and posts an **internal
root-cause analysis (RCA)** to the Slack thread.

> **Status:** M0 done (chat prototype, see §15). This plan replaces the chat-first design with a
> trigger-driven one. **The MVP runs entirely on mocked data.**

---

## 1. Goal and scope

**In the MVP:**
- **Triggers:** a Slack workflow form submitted by a solutions engineer, or a CloudWatch alarm.
- **Intake with no LLM:** structured fields, validation, and hardcoded follow-up questions when
  something is missing.
- **Investigation:** fixed checks run in code, rules rank the possible causes, then **one LLM call
  writes the RCA**. The LLM may request at most 3 extra checks when the evidence is ambiguous.
- **Output:** an internal RCA in the Slack thread, labeled "Automated RCA — internal".

**Not in the MVP:**
- Anything sent to customers. **There is no customer reply draft.** Solutions engineers write
  customer replies themselves.
- Posting to Jira. A Jira link on the form is for reference only, because Jira is customer-facing
  and the RCA contains internal details.
- Any write or fix action, such as replaying the DLQ or rolling back.
- Real Slack, AWS, GitHub, CloudWatch Logs or Splunk connections. Everything is mocked.

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
- `site_id` and `tenant_id` are the same.
- The `webhooks` table has **no disabled column**, so a dead endpoint keeps getting attempts.
- The Redis TTL is 5s, so a stale cache can't explain an incident. If the internal config API is
  slow or failing, deliveries fail even when the merchant is healthy.
- Config-fetch failures go **straight to the DLQ** as event-level rows.
- Both tables are keyed by `eventId` (partition) + `destinationKey` (sort). **Looking up a known
  event ID is cheap;** querying by site needs an index (see §16).

---

## 3. Flow overview

```
 Triggers                   Intake (no LLM)              Investigation graph (LangGraph)               Output
┌──────────────────┐     ┌─────────────────────┐    ┌──────────────────────────────────────────┐    ┌────────────────────┐
│ Slack workflow   │──┐  │ parse → validate →  │    │ 1 select playbook     code, no LLM       │    │ Slack thread reply │
│ form (SE)        │  ├─▶│ dedupe →            │───▶│ 2 gather evidence     tools in parallel  │───▶│ "Automated RCA —   │
│ CloudWatch alarm │──┘  │ InvestigationRequest│    │ 3 triage              rules → hypotheses │    │  internal"         │
└──────────────────┘     │                     │    │ 4 write RCA           one LLM call       │    │ audit log + record │
                         │ missing field →     │    │ 5 extra checks        ≤ 3, then rewrite  │    └────────────────────┘
                         │ hardcoded question  │    └──────────────────────────────────────────┘
                         └─────────────────────┘
```

---

## 4. Intake

### 4.1 Slack workflow form: "Report webhook issue"

| Field | Type | Required | Used for |
|---|---|---|---|
| `site_id` | text | ✅ | Every check; validated against the known sites |
| `issue_type` | dropdown: `not_received` / `failing` / `delayed` | ✅ | Picks the playbook |
| `started_at` | date + time (UTC) | ✅ | Investigation window |
| `event_type` | dropdown (from the event type → topic mapping) | for `not_received` | Subscription check, topic for lag |
| `event_ids` | text (comma-separated) | optional | Exact lookup in failed/DLQ |
| `destination_id` | text | optional | Narrows to one endpoint |
| `description` | free text | optional | Given to the RCA step as context only; never parsed |
| `jira_url` | link | optional | Shown in the RCA header for reference only; nothing is posted to Jira |

The workflow posts a structured message to the channel (e.g. `#webhook-issues`). The agent takes
the form fields from that message and replies in its thread.

### 4.2 CloudWatch alarm

The alarm message (SNS JSON) already carries everything: `AlarmName`, `NewStateValue`,
`StateChangeTime`, and `Trigger` (namespace, metric, dimensions, threshold). Only transitions to
`ALARM` start an investigation. The RCA posts to `#webhook-incidents`.

### 4.3 `InvestigationRequest` (the normalized trigger)

```python
class InvestigationRequest(BaseModel):
    id: str                      # investigation ID
    source: Literal["slack_form", "cloudwatch_alarm"]
    source_ref: str              # Slack channel/ts, or alarm name + state change time
    reply_to: str                # Slack channel/thread where the RCA goes
    issue_type: Literal["not_received", "failing", "delayed", "alarm"]
    site_id: str | None          # None for service-wide alarms
    window_start: datetime
    window_end: datetime
    event_type: str | None
    event_ids: list[str]
    destination_id: str | None
    alarm: AlarmInfo | None      # metric, dimensions, threshold, state change time
    description: str | None      # free text, context only
    jira_url: str | None
```

**Window rules (code, not LLM):**
- **Slack form:** from `started_at` minus 30 minutes until now, capped at 7 days.
- **Alarm:** from the state change time minus 2 hours until the state change time plus 15 minutes.

### 4.4 Validation and hardcoded questions

Missing or invalid input gets a fixed reply in the thread, and the investigation waits:

| Problem | Reply |
|---|---|
| `site_id` missing | "Which site ID is this about?" |
| `site_id` unknown | "Site `<value>` doesn't exist. Please check the site ID." |
| `started_at` missing or in the future | "Roughly when did it start? (e.g. 2026-10-25 14:00 UTC)" |
| `not_received` with no `event_type` and no `event_ids` | "Which event type or event ID did the customer expect?" |
| `event_type` not in the mapping | "`<value>` isn't a known event type. Known types: …" |
| `event_ids` malformed | "Event IDs look like `evt_…`. Please check: `<value>`" |

When the engineer replies, the reply is parsed with the same fixed rules (site ID, time, event
type/ID patterns) and validation runs again.

### 4.5 Dedupe
- **Slack:** one investigation per form message (`channel` + `ts`). Edits or repeat submissions
  don't start a new one.
- **Alarms:** one investigation per alarm name per 30-minute window. Repeats of the same alarm add
  a "still firing" note to the existing thread.

---

## 5. Investigation graph (LangGraph)

```
 intake ─▶ select_playbook ─▶ gather ─▶ triage ─▶ write_rca ─┬─▶ publish
                               (parallel                     │
                                tool calls)                  └─▶ extra_checks ─▶ write_rca  (≤ 3 checks, once)
```

**State:**
- `request`: the `InvestigationRequest`
- `evidence`: a dict of `check_name → {ok, data, summary, duration_ms}`
- `hypotheses`: ranked causes from triage
- `rca`: the structured RCA
- `extra_checks_used`
- `messages`: only used by the LLM steps

| Node | LLM? | What it does |
|---|---|---|
| `select_playbook` | No | Maps `issue_type` (and the alarm metric) to a list of checks with concrete arguments (§6). |
| `gather` | No | Runs the checks in parallel through the **same tool wrappers** the LLM would use, so guardrails, redaction and the audit log are identical. A failed check is recorded as evidence ("logs unavailable") and doesn't stop the investigation. |
| `triage` | No | Applies the rules in §7 to the evidence. Produces ranked hypotheses, each with a score and the evidence that matched. |
| `write_rca` | **Yes** | One structured-output call (§8). Input: the request, the evidence summaries, the ranked hypotheses, matching runbook sections. |
| `extra_checks` | **Yes** | Runs only if the RCA's `needs_more_checks` is set. The LLM may request ≤ 3 tool calls from the allowlist (e.g. a baseline from yesterday, or another topic's lag), then `write_rca` runs **once more**. |
| `publish` | No | Formats the RCA as a Slack message (§9), posts it to the thread (MVP: the outbox file), writes the investigation record. |

**Time limits:** each tool times out after 20s, and the whole investigation must finish within 3
minutes. If time runs out, the RCA is published with what was gathered and the missing checks are
listed under Unknowns.

---

## 6. Playbooks (code)

| Check | `not_received` | `failing` | `delayed` | `alarm` (lag / latency / errors) |
|---|---|---|---|---|
| `get_webhook_config(site)`: endpoints, subscriptions | ✅ | ✅ | ✅ | — |
| `get_event_delivery_history(event_ids)`: every attempt from the logs | ✅ if IDs given | ✅ if IDs given | ✅ if IDs given | — |
| `lookup_events(event_ids)`: current state in failed/DLQ | ✅ if IDs given | ✅ if IDs given | — | — |
| `query_failed_events(site, window)` | ✅ | ✅ | ✅ | — |
| `search_logs(webhook_service, site, event_type?)`: attempts, status mix, `time_taken` per endpoint | ✅ | ✅ | ✅ | ✅ service-wide errors |
| `get_cloudwatch_metric`: Kafka time + offset lag on the event's topic | ✅ | — | ✅ | ✅ alarm metric, plus all topics |
| `get_cloudwatch_metric`: delivery failures / latency (service-wide) | — | ✅ | ✅ | ✅ |
| `get_cloudwatch_metric`: config API errors / latency | ✅ | ✅ | ✅ | ✅ |
| `get_recent_deployments(events-service, webhook-service, window − 6h)` | ✅ | ✅ | ✅ | ✅ |
| `search_runbooks(query built from issue type + top hypothesis)` | after triage | after triage | after triage | after triage |

The cross-site view for alarms comes from **CloudWatch metrics and the service logs**, not the DynamoDB
tables, because the tables can't be queried efficiently by time across all sites (§16).

**The service logs are the main source for what happened to a delivery.** The same logs are in
Splunk and CloudWatch Logs, so either can be used (§12). They record every attempt: retries, `request_id`, `time_taken`, status code, searchable by `event_id`
and `site_id`. DynamoDB gives the *current* state (still retrying, or in the DLQ); the logs give the
*history*. The logs are also the only per-site latency source, because the CloudWatch delivery metrics
are service-wide.

---

## 7. Triage rules (code)

Each rule checks the evidence and, if it matches, adds a hypothesis with a score and the evidence
keys that support it. A rule only scores if its required evidence is present; a missing check
lowers the score and is listed as an unknown.

| Category | Matches when |
|---|---|
| `NO_ENDPOINT` | The site has no webhook endpoints. |
| `NOT_SUBSCRIBED` | `not_received`, and no endpoint is subscribed to `event_type`. |
| `DELIVERED_OK` | The event's last logged attempt returned 2xx. The merchant received it; the RCA gives the `request_id`, time and status so the engineer can point the merchant at it. |
| `STILL_RETRYING` | The event's attempts are all failures, fewer than 7, and it's in the failed table. Delivery is pending; the RCA lists each attempt. |
| `NOT_CONSUMED` | The event ID has no delivery logs at all. webhook-service never picked it up: check the producer (`events_service` logs) and consumer lag on its topic. |
| `MERCHANT_ENDPOINT_DOWN` | 5xx or timeouts on one site's endpoint(s), while service-wide failures are normal. |
| `MERCHANT_REJECTING` | Consistent 401/403/404/410 from an endpoint (bad URL or auth on the merchant side). |
| `MERCHANT_RATE_LIMITING` | 429s dominate, or `webhook-ratelimit-cg` lag is rising. |
| `CONFIG_FETCH_FAILURE` | DLQ `CONFIG_FETCH_ERROR` rows, or config API errors/latency above normal. |
| `BAD_DEPLOY` | A deploy finished ≤ 30 min before failures/latency started, and the effect is service-wide. |
| `CONSUMER_LAG` | Time lag above warning, with flat failures. Deliveries are delayed, not failing. |
| `TRAFFIC_BURST` | Offset lag high but time lag low. Consumers are keeping up and it will drain. |
| `UNDETERMINED` | Nothing scores above the threshold. |

Rules live in `triage.py` as small, independently tested functions.

---

## 8. RCA step (the LLM)

**Output schema (structured output, validated with Pydantic):**

```python
class EvidenceItem(BaseModel):
    claim: str            # "wg_3532 returned 503 for 417 deliveries since 14:02Z"
    source: str           # check name, e.g. "query_failed_events"

class RCA(BaseModel):
    title: str                                  # one line
    category: Category                          # one of the §7 categories
    summary: str                                # 2–3 sentences: what, scope, when
    root_cause: str
    confidence: Literal["high", "medium", "low"]
    evidence: list[EvidenceItem]
    timeline: list[str]                         # "14:00Z deploy … 14:02Z first 503 …"
    impact: str                                 # sites/endpoints/event types/counts
    next_steps: list[str]                       # internal actions
    unknowns: list[str]
    runbooks: list[str]                         # links from search_runbooks
    needs_more_checks: list[str]                # empty unless the evidence is ambiguous
```

**Rules in the prompt:**
- Choose the category from the triage hypotheses, or `UNDETERMINED`.
- Every claim must cite a check that produced it. Quote numbers exactly.
- No claim may go beyond the evidence. Anything not checked goes under `unknowns`.
- The audience is internal engineers, so internal detail is fine.

**After the LLM call, code checks the result:**
- The `category` must be one of the triage hypotheses or `UNDETERMINED`.
- Every evidence `source` must be a check that actually ran.

If either check fails, the RCA is regenerated once. If it fails again, the RCA is published as
`UNDETERMINED` with the raw triage output.

---

## 9. Output

### Slack thread message (internal)

```
🔎 Automated RCA — internal · acme · not_received · confidence: high
Merchant endpoint wg_3532 returning 503 since 14:02 UTC

Summary      …
Root cause   MERCHANT_ENDPOINT_DOWN — …
Evidence     • wg_3532: 417× 503, 32× 504 since 14:02Z (query_failed_events)
             • service-wide failure rate normal (delivery_failures metric)
             • no deploys in window (get_recent_deployments)
Timeline     • 14:02Z first 503 · 14:40Z first DLQ entry (retries exhausted)
Impact       acme · wg_3532 · payment.succeeded, invoice.paid, … · 37 events in DLQ
Next steps   • …
Unknowns     • …
Runbook      merchant-endpoint-5xx.md
Jira (ref)   SUP-4821
— 7 checks · 9.2s · gemini-3.6-flash · investigation 3f9c21
```

**MVP:** the message is written to `outbox/<investigation-id>.md` and printed to the terminal.
Nothing is posted anywhere.

### Investigation record
`records/<investigation-id>.json` stores the request, every check's summary and data, the
hypotheses, the RCA, the model used and timings. It's used for evals, debugging and future
"similar incidents" search. The audit log (`logs/<id>.jsonl`) records every tool call.

---

## 10. LLM

- **Provider:** Google Gemini through `langchain-google-genai`, using the user's API key.
- **Models:** `gemini-3.5-flash` (`GEMINI_MODEL`), falling back to `gemini-3.1-flash-lite`
  (`GEMINI_FALLBACK_MODEL`) when the primary is overloaded, rate-limited or times out. Model IDs
  are pinned. In M0 the `gemini-flash-latest` alias was overloaded (350s calls, 503s), while
  `gemini-3.5-flash` answered in 5–12s.
- **The provider can be swapped** through `LLM_PROVIDER`; only Gemini is implemented.
- **The LLM is used only in `write_rca` and `extra_checks`.** Intake, playbooks, data gathering,
  triage and publishing are plain code.
- **Data rule:** the Gemini API is acceptable **with mock data only**. Before real data, switch to
  a provider that meets compliance requirements (e.g. Claude through Amazon Bedrock in
  `us-east-1`).

---

## 11. Tools

These rules apply to every tool:
- **Read-only.**
- **Inputs are typed** (Pydantic) and validated.
- **Times are ISO-8601 UTC**, and each tool has a maximum window.
- **Outputs are compact summaries,** each with a size cap.
- **Errors are returned as data,** not raised.
- **Secrets are stripped** from all outputs.
- **Every call is written to the audit log.**

The same tools serve the playbooks, the LLM's extra checks and the debug chat.

| # | Tool | Inputs | Returns |
|---|---|---|---|
| 1 | `get_webhook_config` | `site_id` | Endpoints: `destinationId`, URL host only, subscribed event types, retry policy. Secrets removed. |
| 2 | `lookup_events` *(new)* | `event_ids` (≤ 20), `site_id?` | For each ID: found in failed / DLQ / neither, `destinationKey`, status, `retryAttempts`, `failureMessage`, times. Exact key lookups. |
| 3 | `query_failed_events` *(M0)* | `site_id, start, end, source` | Counts by status (with destinations and event types), by destination, by event type; retry distribution; first/last seen; samples; config-fetch failures counted separately. Never returns `eventContent`. |
| 4 | `get_cloudwatch_metric` | `metric` (allowlisted key), `dimensions, start, end, period, stat` | Min/max/avg/p99, detected spikes, a downsampled series, and the alarms on that metric (thresholds and state changes in the window). |
| 5 | `get_recent_deployments` | `services, since` | Deploy runs on `main`: time, commit, PR title/author/link, outcome; ECS rollout state. |
| 6 | `search_logs` | `source, start, end, tenant_id?, event_type?, destination_id?, status?, level?, text?, limit≤100` | A summary first: attempts by status, `time_taken` p50/p95/max per destination, retry counts, and errors by message. Then up to `limit` matching lines (secrets stripped). Structured inputs only; the backend builds the query (Splunk SPL or CloudWatch Logs Insights). Sources come from a fixed list. |
| 7 | `get_event_delivery_history` *(new)* | `event_ids` (≤ 20), `site_id?` | For each event, every attempt in order: time, attempt number, destination, `request_id`, status, `time_taken`, message. Final outcome: delivered / retrying / DLQ / no logs. Backed by the logs backend. |
| 8 | `search_runbooks` | `query` | Top 3 runbook sections (`docs/**/*.md` split by heading, ranked with BM25): repo, path, heading, text, link. |

---

## 12. Mock environment

All values are made up for the MVP and get replaced with real values later.

### AWS
- Region: `us-east-1`. ECS cluster: `platform-stg`. Services: `events-service`, `webhook-service`.

### MSK
- Cluster: `events-msk-stg`
- Topics and the event type → topic mapping (`config/topic_mapping.yaml`):

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

### DynamoDB tables
- `webhook-failed-events` and `webhook-dlq-events`. Key: `eventId` + `destinationKey`
  (`<destinationId>:<DESTINATION_TYPE>`).
- The mock has a `tenant_id` + time index; production doesn't yet (§16).
- Timestamps are epoch milliseconds. Max retries: 7.

Failed table item:
```json
{ "eventId":"gf_tirtdjrgs948w", "destinationKey":"wg_3532:WEBHOOK", "tenant_id":"acme",
  "destinationId":"wg_3532", "destinationType":"WEBHOOK", "failedAt":1716135675647,
  "retryAttempts":3, "failureStatusCode":503, "failureMessage":"Service Unavailable",
  "eventContent":"{...serialized event...}", "created_at":1716135675647, "updatedAt":1716136964765 }
```

DLQ items:
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
- `GET /internal/sites/{site_id}/webhooks` returns endpoints with their `event_filters`.
- Auth: Basic auth with the agent's own pre-shared key, plus a caller-name header
  (`incident-agent`). The real client fetches the key from Secrets Manager, keeps it in memory, and
  on a 401 fetches it again once and retries.

### GitHub
- Repos: `example-org/events-service`, `example-org/webhook-service`
- Deploy workflow: `.github/workflows/deploy-ecs.yml` ("Deploy to ECS"), on merge to `main`.

### Logs (same data in Splunk and CloudWatch Logs)
The services' logs go to both Splunk and CloudWatch Logs with the same content, so log tools sit
behind one `LogsBackend` interface. `LOGS_BACKEND=mock|cloudwatch|splunk` picks the source; the
MVP uses `mock`. `config/logs.yaml` maps each logical source to where it lives in each backend:

| Logical source | Splunk | CloudWatch Logs *(assumed names)* |
|---|---|---|
| `webhook_service` | index `webhook_service` | log group `/ecs/webhook-service` |
| `events_service` | index `events_service` | log group `/ecs/events-service` |

- Contents: `webhook_service` has one line per delivery attempt (successes included) plus
  consume/filter/DLQ lines; `events_service` has producer publish logs and config API logs.
- Fields: `_time, level, service, tenant_id, event_id, event_type, destination_id, request_id,
  attempt, status, time_taken_ms, msg, trace_id`.
- Example lines for one event:
  ```
  14:02:11Z INFO  webhook-service tenant_id=acme event_id=evt_8k2lq9 event_type=invoice.paid destination_id=wg_3532 request_id=req_71f2 attempt=1 status=503 time_taken_ms=412  msg="delivery failed, scheduling retry"
  14:03:15Z INFO  webhook-service tenant_id=acme event_id=evt_8k2lq9 event_type=invoice.paid destination_id=wg_3532 request_id=req_9a04 attempt=2 status=504 time_taken_ms=10000 msg="delivery failed, scheduling retry"
  …
  14:40:02Z WARN  webhook-service tenant_id=acme event_id=evt_8k2lq9 event_type=invoice.paid destination_id=wg_3532 request_id=req_c310 attempt=7 status=503 time_taken_ms=388  msg="max retries exceeded, moved to DLQ"
  ```

### Runbooks (mock `docs/`, mixed formats)
`merchant-endpoint-5xx.md`, `consumer-lag.md`, `webhook-config-api-errors.md`,
`rollback-a-bad-deploy.md`, `merchant-rate-limiting.md`, `event-not-received.md`.

### Triggers and scenarios (they double as evals)

Each scenario is a data fixture plus a trigger fixture. Times are relative to "now" (e.g.
`"-95m"`), so the scenarios always look current.

| Scenario | Trigger | Setup | Expected category |
|---|---|---|---|
| `merchant_down` | Slack form: acme, `failing` | wg_3532 503s/504s since T; others healthy; no deploy | `MERCHANT_ENDPOINT_DOWN` |
| `not_subscribed` | Slack form: acme, `not_received`, `invoice.paid` | No endpoint subscribed to `invoice.paid`; no failures | `NOT_SUBSCRIBED` |
| `delivered_ok` | Slack form: acme, `not_received`, event ID | Logs: attempt 1 → 503, attempt 2 → 200 | `DELIVERED_OK` |
| `still_retrying` | Slack form: acme, `not_received`, event ID | Logs: 3 failed attempts; event in failed table, `retryAttempts` 3 | `STILL_RETRYING` |
| `not_consumed` | Slack form: acme, `not_received`, event ID | No `webhook_service` logs for the event; producer log shows it published; lag on its topic | `NOT_CONSUMED` |
| `bad_deploy` | Alarm: `webhook.delivery_failures` | webhook-service deploy at T; failures across sites; time lag rising | `BAD_DEPLOY` |
| `config_api_auth` | Slack form: acme, `not_received` | events-service deploy at T; DLQ `CONFIG_FETCH_ERROR` 401s for many sites | `CONFIG_FETCH_FAILURE` |
| `traffic_burst` | Alarm: `kafka.max_offset_lag` on `payment-events` | Offset lag spike, low time lag, no failures | `TRAFFIC_BURST` |
| `rate_limited` | Slack form: acme, `delayed` | 429s; `webhook-ratelimit-cg` lag | `MERCHANT_RATE_LIMITING` |
| `missing_fields` | Slack form with no `site_id` | — | Hardcoded question; no investigation |

Slack form trigger fixture:
```json
{ "source": "slack_form", "channel": "C0WEBHOOKS", "ts": "1729861234.000100",
  "reported_by": "se.jane",
  "fields": { "site_id": "acme", "issue_type": "not_received", "started_at": "-95m",
              "event_type": "invoice.paid", "event_ids": [], "destination_id": null,
              "description": "Customer says invoice webhooks stopped since this morning",
              "jira_url": "https://example.atlassian.net/browse/SUP-4821" } }
```

Alarm trigger fixture (SNS-style):
```json
{ "source": "cloudwatch_alarm",
  "AlarmName": "webhook-delivery-cg-time-lag-critical", "NewStateValue": "ALARM",
  "StateChangeTime": "-5m",
  "Trigger": { "Namespace": "AWS/Kafka", "MetricName": "EstimatedMaxTimeLag", "Threshold": 120,
               "Dimensions": [ {"name": "Cluster Name", "value": "events-msk-stg"},
                               {"name": "Consumer Group", "value": "webhook-delivery-cg"},
                               {"name": "Topic", "value": "payment-events"} ] } }
```

---

## 13. Interfaces

- **`incident-agent investigate --trigger <file>`** is the main MVP entry point. It runs the whole
  flow on a trigger fixture, prints the RCA and writes the outbox message and record.
- **`incident-agent eval`** runs every scenario and checks the category and key evidence.
- **`incident-agent chat`** (from M0) is kept for debugging and ad-hoc questions.
- **Post-MVP:** a Slack app that listens to `#webhook-issues` for workflow messages and to thread
  replies (answers to the hardcoded questions), plus CloudWatch alarm → SNS → SQS → worker.

---

## 14. Project layout

Code that decides (plain code), code that calls the LLM, code that fetches data and code that
receives triggers live apart, so most of the system is tested without an LLM.

```
incident-agent/
  pyproject.toml  .env.example
  config/                    # committed, non-secret: metrics.yaml topic_mapping.yaml logs.yaml sites.yaml
  src/incident_agent/
    settings.py              # env vars and secrets (.env, git-ignored)
    config.py                # loaders for config/
    llm.py                   # model factory, fallback
    redact.py  audit.py
    intake/                  # triggers → InvestigationRequest; fixed questions (no LLM)
      models.py  slack_form.py  alarm.py  questions.py
    investigation/           # the trigger-driven workflow
      graph.py  models.py  playbooks.py  gather.py  triage.py
      rca.py (schema + output checks)  prompts.py  publish.py
    chat/                    # the debugging chat workflow: graph.py  prompts.py
    tools/                   # what playbooks and the LLM call: input schema, guardrails, summaries
      runner.py              #   shared execution: timing, errors, audit
      <one module per tool>
    backends/                # data access only, behind interfaces
      base.py                #   Protocols
      mock/                  #   scenario-driven fake data and trigger fixtures
      aws/                   #   post-MVP: DynamoDB, CloudWatch metrics + alarms, ECS, Secrets Manager
      logs/                  #   post-MVP: cloudwatch.py (Logs Insights), splunk.py
      github/                #   post-MVP: deploy workflow runs, PRs
      config_api/            #   post-MVP: internal webhook-config API client
    apps/                    # entry points only: parse input, call a workflow
      cli/                   #   __init__.py (Typer app) investigate.py chat.py list_models.py (eval.py in M3)
      slack/                 #   post-MVP: Slack app (form messages, thread replies)
      worker/                #   post-MVP: queue consumer for alarms
  fixtures/
    scenarios/<name>.yaml    triggers/<name>.json    runbooks/
  evals/                     # agent quality, not code correctness
    expectations.yaml        #   expected outcome per scenario
    reports/                 #   per-run results (git-ignored)
  tests/                     # code correctness, scripted LLM
  outbox/  records/  logs/   # generated, git-ignored
```

---

## 15. Milestones

| # | Deliverable | Done when |
|---|---|---|
| **M0 ✅** | Scaffold, Gemini with fallback, chat graph, `query_failed_events`, `merchant_down` data, terminal chat | Done: 29 tests pass; live chat answered correctly |
| **M1 ✅** | Trigger pipeline: `InvestigationRequest`, Slack-form and alarm parsing, validation and hardcoded questions, playbook selection, parallel gather, triage (failure rules), structured RCA, outbox and record. `investigate` command. | Done: 77 tests pass; live Gemini run on `merchant_down` gave `MERCHANT_ENDPOINT_DOWN` in ~12s; `missing_fields` asks its 3 questions; the alarm trigger degrades to `UNDETERMINED` until M2's tools exist |
| **M2 ✅** | Remaining mocked tools: config, `lookup_events`, metrics and alarms, deployments, logs, runbooks. All triage rules. Extra-checks loop. | Done: 117 tests pass, including all 9 scenarios end to end (triage reaches the expected cause). Live Gemini run: 8 of 9 RCAs correct; the 9th (`not_subscribed`) hit Gemini outages/quota and published the triage fallback (top hypothesis NOT_SUBSCRIBED) |
| **M3** | `eval` command (expected category and key evidence per scenario), RCA output checks, dedupe, time limits, unit tests for every rule | All scenarios reach the expected category; tests pass |
| **M4** *(post-MVP)* | Slack app in a test workspace; alarm subscription; real backends in staging | A real form submission gets an RCA in its thread |
| **M5** *(post-MVP)* | Compliant LLM, production rollout | On-call uses it |

---

## 16. Before using real data (post-MVP prerequisites)

1. **DynamoDB:** a `tenant_id` + time index on both tables, owned by webhook-service. Until then,
   lookups by site need a bounded scan, which is acceptable only in staging. Event-ID lookups work
   without it.
2. **IAM role `incident-agent-readonly`**, assumed through AWS SSO or as an ECS task role:
   - `dynamodb:Query` / `GetItem` on the two tables and their indexes
   - `cloudwatch:GetMetricData`, `DescribeAlarmsForMetric`, `DescribeAlarmHistory`
   - `secretsmanager:GetSecretValue` on the agent's own key only
   - `ecs:DescribeServices`
3. **Network:** access to the internal config API (VPN or SSM port forwarding for staging; in-VPC
   when deployed).
4. **Agent identity:** its own pre-shared key and caller name for the internal API.
5. **GitHub:** a fine-grained read-only token (Actions, Contents, Pull requests) on the service repos.
6. **Logs, for whichever backend is chosen:**
   - CloudWatch Logs: `logs:StartQuery`, `logs:GetQueryResults` and `logs:StopQuery` on the two
     log groups. This uses the same IAM role, so no extra credential is needed.
   - Splunk: an API token with a read-only role limited to the two indexes.
7. **Slack app:** bot scopes to read `#webhook-issues` messages and thread replies and to post in
   `#webhook-issues` and `#webhook-incidents` only.
8. **LLM:** a provider approved for production data.
9. **Metric names:** replace the made-up names in `config/metrics.yaml` with the real ones. To find
   them:
   1. `aws cloudwatch list-metrics --region us-east-1 --query 'Metrics[].Namespace' --output text | tr '\t' '\n' | sort -u`
   2. `aws cloudwatch list-metrics --region us-east-1 --namespace <ns>`
   3. `aws cloudwatch describe-alarms-for-metric ...`
   4. Search the service repos for where metrics are emitted, and the IaC for alarm definitions.
   5. `aws cloudwatch list-dashboards`

   Alarm thresholds are read at runtime, so only metric names and dimensions need to be right.

---

## 17. Safety
- Every check is read-only. The agent never replays, rolls back or changes configuration.
- **RCAs are internal only.** They go to Slack threads, never to Jira or customers.
- Event payloads (`eventContent`) never reach the LLM; only the event type is read from them.
  Secrets are stripped from every tool output and log line.
- Every tool call and every published RCA is logged (audit log and investigation record).

---

## 18. Decisions
- **Intake:** a Slack workflow form for solutions engineers, plus CloudWatch alarms. **No LLM in
  intake.** Missing details get hardcoded questions.
- **Jira:** an optional reference link on the form. Nothing is posted to Jira.
- **Output:** an internal RCA in the Slack thread only. No customer reply draft for now.
- **LLM:** writes the RCA, and may request ≤ 3 extra checks. Model: Gemini 3.5 Flash, with
  3.1 Flash-Lite as fallback; mock data only.
- **`chat`** stays as a debugging tool.
- **Service logs** have per-attempt delivery records (retries, `request_id`, `time_taken`,
  status), searchable by `event_id` and `site_id`. They're the main source for a delivery's history
  and for per-site latency. **The same logs are in Splunk and CloudWatch Logs**; the tools work
  with either through `LOGS_BACKEND`.
- Region `us-east-1`. Logs are in the MVP (mocked). `tenant_id` = `site_id`. The event type field in
  `eventContent` is `type`. Retries and rate limiting each have their own topic and consumer group.

## 19. Assumptions (to confirm)
- The `SumOffsetLag` thresholds and all `WebhookService` / `EventsService` metric names are made up.
- Log lines carry `event_type`, so "not received" reports with only an event type (no
  event ID) can be searched by site + event type.
- The producer logs each publish in `events_service` with the `event_id`, which `NOT_CONSUMED` uses.
- The CloudWatch log group names (`/ecs/webhook-service`, `/ecs/events-service`) are made up.
- **Recommended real backend: CloudWatch Logs Insights.** It uses the same IAM role as the other AWS
  checks, so there's no extra credential. Check that log retention covers at least 7 days, and keep
  query windows small, since Insights bills per GB scanned. Splunk remains a config switch away.
- Slack channels: `#webhook-issues` (form submissions) and `#webhook-incidents` (alarm RCAs).
