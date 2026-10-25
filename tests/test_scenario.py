import pytest

from conftest import FIXTURES_DIR, MINUTE_MS
from incident_agent.backends.mock import MockFailedEventsBackend, available_scenarios, load_scenario


def test_merchant_down_is_listed():
    assert "merchant_down" in available_scenarios(FIXTURES_DIR)


def test_unknown_scenario_lists_available_ones():
    with pytest.raises(FileNotFoundError, match="merchant_down"):
        load_scenario(FIXTURES_DIR, "nope")


def test_generation_is_deterministic(now_ms):
    a = load_scenario(FIXTURES_DIR, "merchant_down", now_ms=now_ms)
    b = load_scenario(FIXTURES_DIR, "merchant_down", now_ms=now_ms)
    assert a.failed_events == b.failed_events
    assert a.dlq_events == b.dlq_events


def test_items_match_table_shape(merchant_down):
    failed = merchant_down.failed_events[0]
    assert failed["destinationKey"] == f"{failed['destinationId']}:WEBHOOK"
    assert 1 <= failed["retryAttempts"] <= 7
    dlq = merchant_down.dlq_events[0]
    assert dlq["retryAttempts"] == 7
    assert dlq["dlqReason"] == "MAX_RETRIES_EXCEEDED"


def test_backend_filters_by_tenant_and_window(merchant_down, now_ms):
    backend = MockFailedEventsBackend(merchant_down)
    last_2h = (now_ms - 120 * MINUTE_MS, now_ms + 1)
    # The 3 background 400s on wg_7710 happened 200-300 minutes ago.
    assert len(backend.query_by_tenant("failed", "acme", *last_2h)) == 380 + 32
    assert len(backend.query_by_tenant("failed", "acme", now_ms - 400 * MINUTE_MS, now_ms + 1)) == 415
    assert len(backend.query_by_tenant("dlq", "acme", *last_2h)) == 37
    assert backend.query_by_tenant("failed", "nobody", *last_2h) == []
