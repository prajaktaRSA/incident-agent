"""Rules that turn evidence into ranked hypotheses. Plain code, each rule tested on its own.

M1 covers the rules that need only failed/DLQ data. The rules that need config, logs, metrics or
deploys arrive with those tools in M2.
"""

from collections import Counter
from collections.abc import Callable

from incident_agent.intake.models import InvestigationRequest
from incident_agent.investigation.models import Category, Evidence, Hypothesis

MIN_FAILURES = 20  # below this, failures are treated as background noise
DOMINANT_SHARE = 0.6
CONCENTRATED_SHARE = 0.8
MIN_SCORE = 0.5
REJECT_STATUSES = {401, 403, 404, 410}

Rule = Callable[[dict[str, Evidence], InvestigationRequest], Hypothesis | None]


def triage(evidence: list[Evidence], request: InvestigationRequest) -> list[Hypothesis]:
    by_check = {e.check: e for e in evidence}
    hypotheses = [h for rule in RULES if (h := rule(by_check, request)) and h.score >= MIN_SCORE]
    if not hypotheses:
        return [_undetermined(by_check)]
    return sorted(hypotheses, key=lambda h: h.score, reverse=True)


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
    score = 0.6 + (0.15 if concentration >= CONCENTRATED_SHARE else 0)
    return Hypothesis(
        category=Category.MERCHANT_ENDPOINT_DOWN,
        score=score,
        reasons=reasons,
        supporting_checks=["failed_events"],
        # These would separate "merchant down" from "our side" (service-wide failures, a deploy).
        missing_checks=_not_ok(ev, ["delivery_failures", "deployments", "site_logs"]),
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
        score=0.65,
        reasons=[f"{count} of {total} failed deliveries were rejected by the endpoint ({statuses})"],
        supporting_checks=["failed_events"],
        missing_checks=_not_ok(ev, ["webhook_config", "site_logs"]),
    )


def merchant_rate_limiting(ev: dict[str, Evidence], request: InvestigationRequest) -> Hypothesis | None:
    failures = _failures(ev)
    if failures is None:
        return None
    total, rows = failures
    limited = sum(r["count"] for r in rows if r["status"] == 429)
    if limited < 10 or limited / total < 0.5:
        return None
    return Hypothesis(
        category=Category.MERCHANT_RATE_LIMITING,
        score=0.65,
        reasons=[f"{limited} of {total} failed deliveries got 429 Too Many Requests"],
        supporting_checks=["failed_events"],
        missing_checks=_not_ok(ev, ["site_logs"]),
    )


def config_fetch_failure(ev: dict[str, Evidence], request: InvestigationRequest) -> Hypothesis | None:
    failed = ev.get("failed_events")
    if failed is None or failed.status != "ok" or not failed.data:
        return None
    config = failed.data.get("config_fetch_failures", {})
    count = config.get("count", 0)
    if not count:
        return None
    statuses = ", ".join(f"{c}× {s}" for s, c in config.get("by_status", {}).items())
    return Hypothesis(
        category=Category.CONFIG_FETCH_FAILURE,
        score=0.75 if count >= 10 else 0.55,
        reasons=[
            f"{count} events went to the DLQ because the webhook config couldn't be loaded ({statuses})",
            f"first at {config.get('first_seen')}",
        ],
        supporting_checks=["failed_events"],
        missing_checks=_not_ok(ev, ["config_api_errors", "deployments"]),
    )


RULES: list[Rule] = [
    merchant_endpoint_down,
    merchant_rejecting,
    merchant_rate_limiting,
    config_fetch_failure,
]


def _failures(ev: dict[str, Evidence]) -> tuple[int, list[dict]] | None:
    """(total delivery failures, by-status rows) from the failed_events check, if it ran."""
    failed = ev.get("failed_events")
    if failed is None or failed.status != "ok" or not failed.data:
        return None
    rows = failed.data.get("by_status", [])
    total = sum(r["count"] for r in rows)
    return (total, rows) if total else None


def _is_server_error(status: int) -> bool:
    return status >= 500 or status in (0, 408)


def _not_ok(ev: dict[str, Evidence], checks: list[str]) -> list[str]:
    return [c for c in checks if c not in ev or ev[c].status != "ok"]


def _undetermined(ev: dict[str, Evidence]) -> Hypothesis:
    ran = [e for e in ev.values() if e.status == "ok"]
    reasons = ["No rule matched the evidence"]
    failed = ev.get("failed_events")
    if failed and failed.status == "ok" and failed.data:
        totals = failed.data.get("totals", {})
        reasons.append(f"failed/DLQ in window: {totals.get('failed', 0)} failed, {totals.get('dlq', 0)} DLQ")
    return Hypothesis(
        category=Category.UNDETERMINED,
        score=0.0,
        reasons=reasons,
        supporting_checks=[e.check for e in ran],
        missing_checks=[e.check for e in ev.values() if e.status != "ok"],
    )
