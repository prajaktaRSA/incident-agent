from datetime import UTC, datetime, timedelta

from incident_agent.intake.models import InvestigationRequest
from incident_agent.investigation.models import Category, Evidence
from incident_agent.investigation.triage import triage

NOW = datetime(2026, 10, 25, 15, 0, tzinfo=UTC)
REQUEST = InvestigationRequest(
    source="slack_form", source_ref="C1/1", reply_to="C1/1", issue_type="failing", site_id="acme",
    window_start=NOW - timedelta(hours=2), window_end=NOW,
)


def _row(status: int, count: int, destination: str = "wg_3532") -> dict:
    return {"status": status, "count": count, "destinations": {destination: count},
            "first_seen": "2026-10-25T13:05:00Z", "last_seen": "2026-10-25T14:59:00Z"}


def _failed_events(rows: list[dict], config_fetch: int = 0) -> Evidence:
    return Evidence(
        check="failed_events", tool="query_failed_events", args={}, purpose="", status="ok",
        summary="",
        data={
            "totals": {"failed": sum(r["count"] for r in rows), "dlq": config_fetch},
            "by_status": rows,
            "config_fetch_failures": {"count": config_fetch, "by_status": {"401": config_fetch} if config_fetch else {},
                                      "first_seen": "2026-10-25T13:10:00Z" if config_fetch else None},
        },
    )


def _skipped(check: str) -> Evidence:
    return Evidence(check=check, tool="t", args={}, purpose="", status="skipped", summary="not available")


def _categories(hypotheses) -> list[Category]:
    return [h.category for h in hypotheses]


def test_one_endpoint_with_5xx_is_merchant_down():
    hypotheses = triage([_failed_events([_row(503, 417), _row(504, 32)]), _skipped("deployments")], REQUEST)
    top = hypotheses[0]
    assert top.category == Category.MERCHANT_ENDPOINT_DOWN
    assert top.score == 0.75
    assert "449 of 449" in top.reasons[0]
    assert "100% of them are on endpoint wg_3532" in top.reasons[1]
    # Without these, "merchant down" can't be told apart from "our side is broken".
    assert set(top.missing_checks) == {"delivery_failures", "deployments", "site_logs"}


def test_5xx_spread_across_endpoints_scores_lower():
    rows = [_row(503, 30, "wg_1"), _row(502, 30, "wg_2")]
    (top,) = triage([_failed_events(rows)], REQUEST)
    assert top.category == Category.MERCHANT_ENDPOINT_DOWN
    assert top.score == 0.6


def test_background_noise_is_undetermined():
    (top,) = triage([_failed_events([_row(503, 5)])], REQUEST)
    assert top.category == Category.UNDETERMINED
    assert "5 failed" in top.reasons[1]


def test_auth_and_gone_errors_mean_the_merchant_is_rejecting():
    assert _categories(triage([_failed_events([_row(401, 40), _row(410, 5)])], REQUEST)) == [
        Category.MERCHANT_REJECTING
    ]


def test_429s_mean_rate_limiting():
    assert _categories(triage([_failed_events([_row(429, 50), _row(503, 5)])], REQUEST)) == [
        Category.MERCHANT_RATE_LIMITING
    ]


def test_config_fetch_failures_outrank_noise():
    hypotheses = triage([_failed_events([], config_fetch=25)], REQUEST)
    assert _categories(hypotheses) == [Category.CONFIG_FETCH_FAILURE]
    assert hypotheses[0].score == 0.75
    assert "401" in hypotheses[0].reasons[0]


def test_multiple_hypotheses_are_ranked():
    spread_5xx = [_row(503, 150, "wg_1"), _row(503, 150, "wg_2")]  # scores 0.6
    hypotheses = triage([_failed_events(spread_5xx, config_fetch=12)], REQUEST)  # config: 0.75
    assert _categories(hypotheses) == [Category.CONFIG_FETCH_FAILURE, Category.MERCHANT_ENDPOINT_DOWN]
    assert [h.score for h in hypotheses] == [0.75, 0.6]


def test_failed_check_means_undetermined_with_it_missing():
    error = Evidence(check="failed_events", tool="query_failed_events", args={}, purpose="",
                     status="error", summary="boom")
    (top,) = triage([error], REQUEST)
    assert top.category == Category.UNDETERMINED
    assert top.missing_checks == ["failed_events"]
