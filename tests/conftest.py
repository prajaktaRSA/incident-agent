import time
from pathlib import Path

import pytest

from incident_agent.backends.mock import Scenario, load_scenario

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
MINUTE_MS = 60_000


@pytest.fixture
def now_ms() -> int:
    return int(time.time() * 1000)


@pytest.fixture
def merchant_down(now_ms: int) -> Scenario:
    return load_scenario(FIXTURES_DIR, "merchant_down", now_ms=now_ms)
