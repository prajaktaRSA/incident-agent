"""Rules that turn evidence into ranked hypotheses. Plain code, each rule tested on its own.

Rules read evidence by check name for site- and event-level data, and by metric key for metrics
(so they work whatever the playbook named the check). A rule only adds a hypothesis when its
required evidence is present; checks that would confirm or rule it out but didn't run are listed
as missing, and the RCA reports them as unknowns.
"""

from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta

from incident_agent.intake.models import InvestigationRequest
from incident_agent.investigation.models import Category, Evidence, Hypothesis

MIN_FAILURES = 20  # below this, failures are treated as background noise
DOMINANT_SHARE = 0.6
CONCENTRATED_SHARE = 0.8
MIN_SCORE = 0.5
REJECT_STATUSES = {401, 403, 404, 410}
DEPLOY_ONSET_WINDOW = timedelta(minutes=30)
EVENT_LEVEL_SCORE = 0.92  # a specific event's own history beats aggregate patterns

Rule = Callable[[dict[str, Evidence], InvestigationRequest], Hypothesis | None]


def triage(evidence: list[Evidence], request: InvestigationRequest) -> list[Hypothesis]:
    by_check = {e.check: e for e in evidence}
    hypotheses = [h for rule in RULES if (h := rule(by_check, request)) and h.score >= MIN_SCORE]
    if not hypotheses:
        return [_undetermined(by_check)]
    return sorted(hypotheses, key=lambda h: h.score, reverse=True)


# --- configuration and event-level rules -------------------------------------------------------


def no_endpoint(ev: dict[str, Evidence], request: InvestigationRequest) -> Hypothesis | None:
    config = _data(ev, "webhook_config")
    if config is None or config["endpoints"]:
        return None
    return Hypothesis(
        category=Category.NO_ENDPOINT,
        score=0.95,
        reasons=[f"site {config['site_id']} has no webhook endpoints configured"],
        supporting_checks=["webhook_config"],
    )


def not_subscribed(ev: dict[str, Evidence], request: InvestigationRequest) -> Hypothesis | None:
    config = _data(ev, "webhook_config")
    if request.issue_type != "not_received" or not request.event_type or not config or not config["endpoints"]:
        return None
    endpoints = [
        e for e in config["endpoints"]
        if not request.destination_id or e["destination_id"] == request.destination_id
    ]
    if any(request.event_type in e["event_types"] for e in endpoints):
        return None
    subscribed = "; ".join(f"{e['destination_id']}: {', '.join(e['event_types'])}" for e in endpoints)
    reasons = [
        f"no endpoint of {request.site_id} is subscribed to {request.event_type}",
        f"current subscriptions — {subscribed}",
    ]
    supporting = ["webhook_config"]
    logs = _data(ev, "site_logs")
    filtered = [m for m in (logs or {}).get("top_messages", []) if "filtered" in m["msg"]]
    if filtered:
        reasons.append(f"logs show {filtered[0]['count']} line(s): \"{filtered[0]['msg']}\"")
        supporting.append("site_logs")
    return Hypothesis(
        category=Category.NOT_SUBSCRIBED, score=0.9, reasons=reasons, supporting_checks=supporting
    )


def delivered_ok(ev: dict[str, Evidence], request: InvestigationRequest) -> Hypothesis | None:
    events = _events(ev)
    delivered = [e for e in events if e["outcome"] == "delivered"]
    if not delivered:
        return None
    reasons = [
        f"{e['event_id']} was delivered to {e['delivered']['destination_id']} at {e['delivered']['at']} "
        f"on attempt {e['delivered']['attempt']} (status {e['delivered']['status']}, "
        f"request_id {e['delivered']['request_id']})"
        for e in delivered
    ]
    share = len(delivered) / len(events)
    return Hypothesis(
        category=Category.DELIVERED_OK,
        score=EVENT_LEVEL_SCORE if share == 1 else 0.7,
        reasons=reasons + ([f"{len(events) - len(delivered)} other event(s) were not delivered"] if share < 1 else []),
        supporting_checks=["event_history"],
        missing_checks=_not_ok(ev, ["event_state"]),
    )


def still_retrying(ev: dict[str, Evidence], request: InvestigationRequest) -> Hypothesis | None:
    retrying = [e for e in _events(ev) if e["outcome"] == "retrying"]
    state = {e["event_id"]: e for e in (_data(ev, "event_state") or {}).get("events", [])}
    retrying_ids = {e["event_id"] for e in retrying} | {i for i, e in state.items() if e["state"] == "retrying"}
    if not retrying_ids:
        return None
    reasons = []
    for event in retrying:
        statuses = ", ".join(str(a["status"]) for a in event["attempts"])
        reasons.append(
            f"{event['event_id']}: {event['attempt_count']} failed attempt(s) so far ({statuses}); "
            f"last at {event['attempts'][-1]['at'] if event['attempts'] else 'unknown'}"
        )
    for event_id in retrying_ids:
        rows = state.get(event_id, {}).get("rows", [])
        if rows:
            reasons.append(f"{event_id} is in the failed table with {rows[0]['retry_attempts']} of 7 attempts used")
    return Hypothesis(
        category=Category.STILL_RETRYING,
        score=EVENT_LEVEL_SCORE,
        reasons=reasons,
        supporting_checks=[c for c in ("event_history", "event_state") if _ok(ev, c)],
    )


def not_consumed(ev: dict[str, Evidence], request: InvestigationRequest) -> Hypothesis | None:
    missing = [e for e in _events(ev) if e["outcome"] == "no_logs"]
    if not missing:
        return None
    reasons, score = [], 0.75
    for event in missing:
        if event["published_at"]:
            reasons.append(f"{event['event_id']} was published at {event['published_at']} but webhook-service has no log of it")
            score = 0.85
        else:
            reasons.append(f"{event['event_id']}: no delivery logs and no publish log either (may never have been produced)")
    supporting = ["event_history"]
    lagging = [m for m in _metrics(ev, "kafka.time_lag") if m["data"]["status"] != "normal"]
    for metric in lagging:
        topic = metric["data"]["dimensions"].get("Topic")
        reasons.append(f"consumer time lag on {topic} reached {metric['data']['max']:g}s ({metric['data']['status']})")
        supporting.append(metric["check"])
        score = EVENT_LEVEL_SCORE
    return Hypothesis(
        category=Category.NOT_CONSUMED,
        score=score,
        reasons=reasons,
        supporting_checks=supporting,
        missing_checks=[] if _metrics(ev, "kafka.time_lag") else ["kafka time lag"],
    )


# --- merchant-side rules -------------------------------------------------------------------------


def merchant_endpoint_down(ev: dict[str, Evidence], request: InvestigationRequest) -> Hypothesis | None:
    failures = _failures(ev)
    if failures is None:
        return None
    total, rows = failures
    server = [r for r in rows if _is_server_error(r["status"])]
    server_count = sum(r["count"] for r in server)
    if total < MIN_FAILURES or server_count / total < DOMINANT_SHARE:
        return None

    destinations = Counter()
    for row in server:
        destinations.update(row.get("destinations", {}))
    top_destination, top_count = destinations.most_common(1)[0]
    concentration = top_count / server_count
    statuses = ", ".join(f"{r['count']}× {r['status']}" for r in server)
    first_seen = min(r["first_seen"] for r in server if r.get("first_seen"))

    reasons = [
        f"{server_count} of {total} failed deliveries are 5xx or timeouts ({statuses})",
        f"{concentration:.0%} of them are on endpoint {top_destination}",
        f"first 5xx/timeout at {first_seen}",
    ]
    supporting = ["failed_events"]
    score = 0.6 + (0.15 if concentration >= CONCENTRATED_SHARE else 0)

    service_wide = _metrics(ev, "webhook.delivery_failures")
    if service_wide:
        if service_wide[0]["data"]["spikes"]:
            score -= 0.2
            reasons.append("service-wide delivery failures also spiked, which points at our side")
        else:
            score += 0.1
            reasons.append("service-wide delivery failures are normal, so it's specific to this site")
        supporting.append(service_wide[0]["check"])
    deploys = _data(ev, "deployments")
    if deploys is not None and not _deploys_near(deploys, first_seen):
        score += 0.05
        reasons.append("no deploy of our services shortly before the failures started")
        supporting.append("deployments")
    logs = _data(ev, "site_logs")
    if logs:
        healthy = [d for d in logs["destinations"] if d["destination_id"] != top_destination and d["success_rate"] >= 0.95]
        if healthy:
            reasons.append(
                "the site's other endpoint(s) are healthy: "
                + ", ".join(f"{d['destination_id']} {d['success_rate']:.0%} success" for d in healthy)
            )
            supporting.append("site_logs")
    return Hypothesis(
        category=Category.MERCHANT_ENDPOINT_DOWN,
        score=round(min(score, 0.9), 2),
        reasons=reasons,
        supporting_checks=supporting,
        missing_checks=_not_ok(ev, ["deployments", "site_logs"]) + ([] if service_wide else ["delivery_failures"]),
    )


def merchant_rejecting(ev: dict[str, Evidence], request: InvestigationRequest) -> Hypothesis | None:
    failures = _failures(ev)
    if failures is None:
        return None
    total, rows = failures
    rejected = [r for r in rows if r["status"] in REJECT_STATUSES]
    count = sum(r["count"] for r in rejected)
    if count < 10 or count / total < DOMINANT_SHARE:
        return None
    statuses = ", ".join(f"{r['count']}× {r['status']}" for r in rejected)
    return Hypothesis(
        category=Category.MERCHANT_REJECTING,
        score=0.75,
        reasons=[f"{count} of {total} failed deliveries were rejected by the endpoint ({statuses})"],
        supporting_checks=["failed_events"],
        missing_checks=_not_ok(ev, ["webhook_config", "site_logs"]),
    )


def merchant_rate_limiting(ev: dict[str, Evidence], request: InvestigationRequest) -> Hypothesis | None:
    reasons, supporting, score = [], [], 0.0
    failures = _failures(ev)
    if failures:
        total, rows = failures
        limited = sum(r["count"] for r in rows if r["status"] == 429)
        if limited >= 10 and limited / total >= 0.5:
            reasons.append(f"{limited} of {total} failed deliveries got 429 Too Many Requests")
            supporting.append("failed_events")
            score = 0.65
    logs = _data(ev, "site_logs")
    if logs:
        for d in logs["destinations"]:
            share = int(d["by_status"].get("429", 0)) / d["attempts"]
            if share >= 0.2:
                reasons.append(f"{d['destination_id']}: {share:.0%} of {d['attempts']} attempts returned 429")
                supporting.append("site_logs")
                score = max(score, 0.6) + 0.1
                break
    if not reasons:
        return None
    for metric in _metrics(ev, "kafka.time_lag"):
        if metric["data"]["dimensions"].get("Topic") == "webhook-ratelimited-events" and metric["data"]["status"] != "normal":
            reasons.append(f"rate-limited deliveries are backing up: time lag {metric['data']['max']:g}s on webhook-ratelimited-events")
            supporting.append(metric["check"])
            score += 0.1
    return Hypothesis(
        category=Category.MERCHANT_RATE_LIMITING,
        score=round(min(score, 0.9), 2),
        reasons=reasons,
        supporting_checks=supporting,
    )


# --- our-side rules ------------------------------------------------------------------------------


def config_fetch_failure(ev: dict[str, Evidence], request: InvestigationRequest) -> Hypothesis | None:
    reasons, supporting, score = [], [], 0.0
    first_at = None
    failed = _data(ev, "failed_events")
    config = (failed or {}).get("config_fetch_failures", {})
    if config.get("count"):
        statuses = ", ".join(f"{c}× {s}" for s, c in config.get("by_status", {}).items())
        reasons += [
            f"{config['count']} events went to the DLQ because the webhook config couldn't be loaded ({statuses})",
            f"first at {config.get('first_seen')}",
        ]
        supporting.append("failed_events")
        score = 0.75 if config["count"] >= 10 else 0.55
        first_at = config.get("first_seen")
    for key in ("webhook.config_fetch_errors", "config_api.errors"):
        for metric in _metrics(ev, key):
            if metric["data"]["spikes"] or metric["data"]["status"] != "normal":
                spike_start = metric["data"]["spikes"][0]["start"] if metric["data"]["spikes"] else None
                reasons.append(f"{key} rose to {metric['data']['max']:g} ({metric['data']['status']})"
                               + (f" from {spike_start}" if spike_start else ""))
                supporting.append(metric["check"])
                score = max(score, 0.6) + 0.1
                first_at = first_at or spike_start
    if not reasons:
        return None
    deploys = _data(ev, "deployments")
    near = _deploys_near(deploys, first_at) if deploys and first_at else []
    for deploy in near:
        pr = deploy["pr"]
        reasons.append(
            f"{deploy['service']} was deployed at {deploy['finished_at']} just before the first failure"
            + (f" (PR #{pr['number']}: {pr['title']})" if pr else "")
        )
        supporting.append("deployments")
        score += 0.05
    return Hypothesis(
        category=Category.CONFIG_FETCH_FAILURE,
        score=round(min(score, 0.92), 2),
        reasons=reasons,
        supporting_checks=sorted(set(supporting), key=supporting.index),
        missing_checks=_not_ok(ev, ["deployments"]),
    )


def bad_deploy(ev: dict[str, Evidence], request: InvestigationRequest) -> Hypothesis | None:
    deploys = _data(ev, "deployments")
    if not deploys or not deploys["deployments"]:
        return None
    anomalies = [
        (spike["start"], metric)
        for key in ("webhook.delivery_failures", "webhook.delivery_latency", "kafka.time_lag")
        for metric in _metrics(ev, key)
        for spike in metric["data"]["spikes"][:1]
    ]
    if not anomalies:
        return None
    onset = min(start for start, _ in anomalies)
    candidates = _deploys_near(deploys, onset)
    if not candidates:
        return None
    deploy = candidates[0]
    pr = deploy["pr"]
    spiked = sorted({m["data"]["metric"] for _, m in anomalies})
    return Hypothesis(
        category=Category.BAD_DEPLOY,
        score=0.85 if deploy["service"] == "webhook-service" else 0.7,
        reasons=[
            f"{deploy['service']} deploy finished at {deploy['finished_at']}"
            + (f" (PR #{pr['number']} by {pr['author']}: {pr['title']})" if pr else ""),
            f"{', '.join(spiked)} started rising at {onset}, within 30 minutes of the deploy",
        ],
        supporting_checks=["deployments", *sorted({m["check"] for _, m in anomalies})],
    )


def consumer_lag(ev: dict[str, Evidence], request: InvestigationRequest) -> Hypothesis | None:
    lagging = [m for m in _metrics(ev, "kafka.time_lag") if m["data"]["status"] != "normal"]
    if not lagging:
        return None
    failures = _metrics(ev, "webhook.delivery_failures")
    if failures and failures[0]["data"]["spikes"]:
        return None  # failing as well as lagging: other rules explain it better
    reasons = [
        f"time lag on {m['data']['dimensions'].get('Topic')} ({m['data']['dimensions'].get('Consumer Group')}) "
        f"reached {m['data']['max']:g}s ({m['data']['status']})"
        for m in lagging
    ]
    if failures:
        reasons.append("delivery failures are normal: deliveries are delayed, not failing")
    return Hypothesis(
        category=Category.CONSUMER_LAG,
        score=0.7,
        reasons=reasons,
        supporting_checks=[m["check"] for m in lagging] + ([failures[0]["check"]] if failures else []),
        missing_checks=[] if failures else ["delivery_failures"],
    )


def traffic_burst(ev: dict[str, Evidence], request: InvestigationRequest) -> Hypothesis | None:
    offset = [m for m in _metrics(ev, "kafka.max_offset_lag") + _metrics(ev, "kafka.sum_offset_lag")
              if m["data"]["status"] != "normal" or m["data"]["spikes"]]
    if not offset:
        return None
    topic = offset[0]["data"]["dimensions"].get("Topic")
    time_lag = [m for m in _metrics(ev, "kafka.time_lag") if m["data"]["dimensions"].get("Topic") == topic]
    if not time_lag or time_lag[0]["data"]["status"] != "normal":
        return None
    reasons = [
        f"offset lag on {topic} peaked at {offset[0]['data']['max']:g} messages ({offset[0]['data']['status']})",
        f"time lag stayed low (max {time_lag[0]['data']['max']:g}s): consumers are keeping up",
    ]
    supporting = [offset[0]["check"], time_lag[0]["check"]]
    score = 0.8
    attempts = _metrics(ev, "webhook.delivery_attempts")
    if attempts and attempts[0]["data"]["spikes"]:
        reasons.append(f"delivery volume rose to {attempts[0]['data']['max']:g} per period (baseline {attempts[0]['data']['baseline']:g})")
        supporting.append(attempts[0]["check"])
        score = 0.9
    return Hypothesis(category=Category.TRAFFIC_BURST, score=score, reasons=reasons, supporting_checks=supporting)


RULES: list[Rule] = [
    no_endpoint,
    not_subscribed,
    delivered_ok,
    still_retrying,
    not_consumed,
    merchant_endpoint_down,
    merchant_rejecting,
    merchant_rate_limiting,
    config_fetch_failure,
    bad_deploy,
    consumer_lag,
    traffic_burst,
]


# --- helpers -------------------------------------------------------------------------------------


def _ok(ev: dict[str, Evidence], check: str) -> bool:
    return check in ev and ev[check].status == "ok" and ev[check].data is not None


def _data(ev: dict[str, Evidence], check: str) -> dict | None:
    return ev[check].data if _ok(ev, check) else None


def _events(ev: dict[str, Evidence]) -> list[dict]:
    return (_data(ev, "event_history") or {}).get("events", [])


def _metrics(ev: dict[str, Evidence], key: str) -> list[dict]:
    """Successful metric checks for a metric key, whatever the check is called."""
    return [
        {"check": e.check, "data": e.data}
        for e in ev.values()
        if e.status == "ok" and e.data and e.data.get("metric") == key and e.data.get("points")
    ]


def _failures(ev: dict[str, Evidence]) -> tuple[int, list[dict]] | None:
    """(total delivery failures, by-status rows) from the failed_events check, if it ran."""
    failed = _data(ev, "failed_events")
    if failed is None:
        return None
    rows = failed.get("by_status", [])
    total = sum(r["count"] for r in rows)
    return (total, rows) if total else None


def _deploys_near(deploys: dict, onset: str | None) -> list[dict]:
    """Successful deploys that finished up to 30 minutes before `onset` (or during it)."""
    if not onset:
        return []
    onset_at = _parse(onset)
    return [
        d for d in deploys["deployments"]
        if d["conclusion"] == "success"
        and _parse(d["started_at"]) <= onset_at <= _parse(d["finished_at"]) + DEPLOY_ONSET_WINDOW
    ]


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _is_server_error(status: int) -> bool:
    return status >= 500 or status in (0, 408)


def _not_ok(ev: dict[str, Evidence], checks: list[str]) -> list[str]:
    return [c for c in checks if not _ok(ev, c)]


def _undetermined(ev: dict[str, Evidence]) -> Hypothesis:
    ran = [e for e in ev.values() if e.status == "ok"]
    reasons = ["No rule matched the evidence"]
    failed = _data(ev, "failed_events")
    if failed:
        totals = failed.get("totals", {})
        reasons.append(f"failed/DLQ in window: {totals.get('failed', 0)} failed, {totals.get('dlq', 0)} DLQ")
    return Hypothesis(
        category=Category.UNDETERMINED,
        score=0.0,
        reasons=reasons,
        supporting_checks=[e.check for e in ran],
        missing_checks=[e.check for e in ev.values() if e.status != "ok"],
    )
