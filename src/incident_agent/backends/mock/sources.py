"""Mock config API, logs, metrics, deployments and runbooks, all driven by a Scenario."""

import random
from pathlib import Path
from typing import Any

from incident_agent.backends.mock.scenario import Scenario
from incident_agent.config import MetricDef


class MockWebhookConfigBackend:
    def __init__(self, scenario: Scenario, known_sites: frozenset[str]) -> None:
        self._config = scenario.webhook_config
        self._known_sites = known_sites

    def get_site_webhooks(self, site_id: str) -> list[dict[str, Any]] | None:
        if site_id not in self._known_sites:
            return None
        return list(self._config.get(site_id, []))


class MockLogsBackend:
    def __init__(self, scenario: Scenario) -> None:
        self._lines = scenario.log_lines

    def search(
        self, source: str, start_ms: int, end_ms: int, filters: dict[str, Any]
    ) -> list[dict[str, Any]]:
        event_ids = set(filters.get("event_ids") or [])
        text = (filters.get("text") or "").lower()
        matches = []
        for line in self._lines:
            if line["source"] != source or not start_ms <= line["time_ms"] < end_ms:
                continue
            if event_ids and line["event_id"] not in event_ids:
                continue
            if any(
                filters.get(key) is not None and line[key] != filters[key]
                for key in ("tenant_id", "event_type", "destination_id", "status", "level")
            ):
                continue
            if text and text not in line["msg"].lower():
                continue
            matches.append(line)
        return matches


class MockMetricsBackend:
    """Series = baseline with jitter, overridden inside spike windows. Alarms derive from thresholds."""

    def __init__(self, scenario: Scenario) -> None:
        self._scenario = scenario

    def get_series(
        self, metric: MetricDef, dimensions: dict[str, str], start_ms: int, end_ms: int, period_s: int
    ) -> list[tuple[int, float]]:
        spec = self._spec_for(metric.key, dimensions)
        baseline = float(spec.get("baseline", self._scenario.metric_baselines.get(metric.key, 0)))
        jitter = float(spec.get("jitter", 0.1))
        period_ms = period_s * 1000
        first = start_ms - start_ms % period_ms + (period_ms if start_ms % period_ms else 0)
        points = []
        for t in range(first, end_ms, period_ms):
            rng = random.Random(f"{self._scenario.name}|{metric.key}|{sorted(dimensions.items())}|{t}")
            value = baseline
            for spike in spec.get("spikes", []):
                if self._minutes(spike["from_min"]) <= t < self._minutes(spike.get("to_min", 1)):
                    value = float(spike["value"])
            points.append((t, round(max(0.0, value * (1 + rng.uniform(-jitter, jitter))), 2)))
        return points

    def get_alarms(
        self, metric: MetricDef, dimensions: dict[str, str], start_ms: int, end_ms: int
    ) -> list[dict[str, Any]]:
        series = self.get_series(metric, dimensions, start_ms, end_ms, 60)
        alarms = []
        for level, threshold in (("warning", metric.warning), ("critical", metric.critical)):
            if threshold is None:
                continue
            changes, state = [], "OK"
            for t, value in series:
                new_state = "ALARM" if value >= threshold else "OK"
                if new_state != state:
                    changes.append({"time_ms": t, "state": new_state})
                    state = new_state
            alarms.append(
                {
                    "name": f"{metric.key}-{level}",
                    "level": level,
                    "threshold": threshold,
                    "comparison": ">=",
                    "state": state,
                    "state_changes": changes,
                }
            )
        return alarms

    def _spec_for(self, key: str, dimensions: dict[str, str]) -> dict[str, Any]:
        """The most specific spec whose dimensions are all present in the request."""
        candidates = [
            s
            for s in self._scenario.metric_specs
            if s["metric"] == key
            and all(dimensions.get(k) == v for k, v in (s.get("dimensions") or {}).items())
        ]
        return max(candidates, key=lambda s: len(s.get("dimensions") or {}), default={})

    def _minutes(self, minutes: float) -> int:
        return self._scenario.now_ms + int(minutes * 60_000)


class MockDeploymentsBackend:
    def __init__(self, scenario: Scenario) -> None:
        self._deployments = scenario.deployments

    def list_deployments(self, services: list[str], since_ms: int, until_ms: int) -> list[dict[str, Any]]:
        runs = [
            d for d in self._deployments
            if d["service"] in services and since_ms <= d["started_ms"] < until_ms
        ]
        return sorted(runs, key=lambda d: d["started_ms"], reverse=True)


class FileRunbooksBackend:
    """Runbooks from `<root>/<org>/<repo>/docs/**/*.md` (mock fixtures, or cloned repos later)."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def documents(self) -> list[dict[str, str]]:
        docs = []
        for path in sorted(self._root.glob("*/*/docs/**/*.md")):
            org, repo = path.relative_to(self._root).parts[:2]
            relative = path.relative_to(self._root / org / repo).as_posix()
            docs.append(
                {
                    "repo": f"{org}/{repo}",
                    "path": relative,
                    "text": path.read_text(),
                    "url": f"https://github.com/{org}/{repo}/blob/main/{relative}",
                }
            )
        return docs
