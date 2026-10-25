"""Expands a scenario YAML spec into concrete mock data.

Specs describe *groups* (e.g. "380 × 503 on wg_3532 over the last 95 minutes") instead of
literal rows. Times are minutes relative to "now", so every scenario looks current and
time-window queries always hit data. Generation is seeded by the scenario name, so a scenario
always produces the same data for a given "now".

Every scenario is layered on `_base.yaml` (healthy platform: endpoints, metric baselines,
background traffic, an old deploy): list sections are appended, mapping sections are merged with
the scenario winning.
"""

import json
import random
import string
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MINUTE_MS = 60_000
BASE = "_base"
_LIST_SECTIONS = ("failed_events", "dlq_events", "failed_items", "logs", "event_histories",
                  "metrics", "deployments")
_MAP_SECTIONS = ("webhook_config", "metric_baselines")
_SERVICE_NAMES = {"webhook_service": "webhook-service", "events_service": "events-service"}


@dataclass
class Scenario:
    name: str
    description: str
    now_ms: int = 0
    failed_events: list[dict[str, Any]] = field(default_factory=list)
    dlq_events: list[dict[str, Any]] = field(default_factory=list)
    webhook_config: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    log_lines: list[dict[str, Any]] = field(default_factory=list)
    metric_baselines: dict[str, float] = field(default_factory=dict)
    metric_specs: list[dict[str, Any]] = field(default_factory=list)
    deployments: list[dict[str, Any]] = field(default_factory=list)


def available_scenarios(fixtures_dir: Path) -> list[str]:
    return sorted(p.stem for p in (fixtures_dir / "scenarios").glob("*.yaml") if not p.stem.startswith("_"))


def load_scenario(fixtures_dir: Path, name: str, now_ms: int | None = None) -> Scenario:
    path = fixtures_dir / "scenarios" / f"{name}.yaml"
    if name.startswith("_") or not path.exists():
        known = ", ".join(available_scenarios(fixtures_dir)) or "none"
        raise FileNotFoundError(f"Unknown scenario '{name}'. Available: {known}")
    spec = _merge(_read(fixtures_dir / "scenarios" / f"{BASE}.yaml"), _read(path))
    return expand(spec, now_ms if now_ms is not None else int(time.time() * 1000))


def expand(spec: dict[str, Any], now_ms: int) -> Scenario:
    rng = random.Random(spec["name"])
    at = lambda minutes: now_ms + int(minutes * MINUTE_MS)  # noqa: E731

    failed = [i for g in spec.get("failed_events", []) for i in _failure_group(g, at, rng, dlq=False)]
    dlq = [i for g in spec.get("dlq_events", []) for i in _failure_group(g, at, rng, dlq=True)]
    for item in spec.get("failed_items", []):
        failed.append(_failed_item(item, at))

    logs = [line for g in spec.get("logs", []) for line in _log_group(g, at, rng)]
    for history in spec.get("event_histories", []):
        logs.extend(_event_history(history, at, rng))
    logs.sort(key=lambda line: line["time_ms"])

    deployments = [_deployment(d, at) for d in spec.get("deployments", [])]
    return Scenario(
        name=spec["name"],
        description=(spec.get("description") or "").strip(),
        now_ms=now_ms,
        failed_events=failed,
        dlq_events=dlq,
        webhook_config=spec.get("webhook_config", {}),
        log_lines=logs,
        metric_baselines=spec.get("metric_baselines", {}),
        metric_specs=spec.get("metrics", []),
        deployments=deployments,
    )


def _read(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text()) if path.exists() else {}


def _merge(base: dict[str, Any], scenario: dict[str, Any]) -> dict[str, Any]:
    merged = {**base, **scenario}
    for key in _LIST_SECTIONS:
        merged[key] = list(base.get(key, [])) + list(scenario.get(key, []))
    for key in _MAP_SECTIONS:
        merged[key] = {**base.get(key, {}), **scenario.get(key, {})}
    return merged


def _id(rng: random.Random, prefix: str, length: int = 12) -> str:
    return prefix + "".join(rng.choices(string.ascii_lowercase + string.digits, k=length))


def _event_content(event_id: str, event_type: str, tenant_id: str, rng: random.Random) -> str:
    return json.dumps(
        {
            "id": event_id,
            "type": event_type,
            "tenant_id": tenant_id,
            # Stand-in for the customer/payment data a real event carries.
            "data": {"customer_email": "jane.doe@example.com", "amount": rng.randint(5, 900)},
        }
    )


def _failure_group(group: dict[str, Any], at, rng: random.Random, *, dlq: bool) -> list[dict[str, Any]]:
    start_ms, end_ms = at(group["from_min"]), at(group.get("to_min", 0))
    failure_type = group.get("failure_type", "DELIVERY_ERROR")
    is_config_fetch = failure_type == "CONFIG_FETCH_ERROR"
    event_types = group.get("event_types", ["payment.succeeded"])
    retry_low, retry_high = group.get("retry_attempts", [1, 6])

    items = []
    for _ in range(group["count"]):
        at_ms = rng.randint(start_ms, max(start_ms, end_ms - 1))
        event_id = _id(rng, "evt_")
        item = _row(
            event_id=event_id,
            tenant_id=group["tenant_id"],
            destination_id=None if is_config_fetch else group["destination_id"],
            status=group["status"],
            message=group["message"],
            content=_event_content(event_id, rng.choice(event_types), group["tenant_id"], rng),
        )
        if dlq:
            item |= {
                "failureType": failure_type,
                "dlqReason": group.get(
                    "dlq_reason", "CONFIG_UNAVAILABLE" if is_config_fetch else "MAX_RETRIES_EXCEEDED"
                ),
                "retryAttempts": 0 if is_config_fetch else 7,
                "created_at": at_ms,
                "updatedAt": at_ms,
            }
        else:
            item |= {
                "retryAttempts": rng.randint(retry_low, retry_high),
                "failedAt": at_ms,
                "created_at": at_ms - rng.randint(1, 30) * MINUTE_MS,
                "updatedAt": at_ms,
            }
        items.append(item)
    return items


def _failed_item(spec: dict[str, Any], at) -> dict[str, Any]:
    """One literal row in the failed table (for event-level scenarios)."""
    rng = random.Random(spec["event_id"])
    failed_at = at(spec["failed_min"])
    return _row(
        event_id=spec["event_id"],
        tenant_id=spec["tenant_id"],
        destination_id=spec["destination_id"],
        status=spec["status"],
        message=spec["message"],
        content=_event_content(spec["event_id"], spec["event_type"], spec["tenant_id"], rng),
    ) | {
        "retryAttempts": spec["retry_attempts"],
        "failedAt": failed_at,
        "created_at": at(spec.get("created_min", spec["failed_min"])),
        "updatedAt": failed_at,
    }


def _row(*, event_id, tenant_id, destination_id, status, message, content) -> dict[str, Any]:
    return {
        "eventId": event_id,
        "destinationKey": f"{destination_id}:WEBHOOK" if destination_id else "NONE:CONFIG_FETCH",
        "tenant_id": tenant_id,
        "destinationId": destination_id,
        "destinationType": "WEBHOOK" if destination_id else "CONFIG_FETCH",
        "failureStatusCode": status,
        "failureMessage": message,
        "eventContent": content,
    }


def _log_group(group: dict[str, Any], at, rng: random.Random) -> list[dict[str, Any]]:
    start_ms, end_ms = at(group["from_min"]), at(group.get("to_min", 0))
    low, high = group.get("time_taken_ms", [80, 400])
    attempt_low, attempt_high = group.get("attempt", [1, 1])
    lines = []
    for _ in range(group["count"]):
        lines.append(
            _line(
                source=group.get("source", "webhook_service"),
                time_ms=rng.randint(start_ms, max(start_ms, end_ms - 1)),
                level=group.get("level", "INFO" if (group.get("status") or 200) < 400 else "WARN"),
                tenant_id=group.get("tenant_id"),
                event_id=_id(rng, "evt_"),
                event_type=rng.choice(group.get("event_types", ["payment.succeeded"])),
                destination_id=group.get("destination_id"),
                request_id=_id(rng, "req_", 8) if group.get("destination_id") else None,
                attempt=rng.randint(attempt_low, attempt_high) if group.get("destination_id") else None,
                status=group.get("status"),
                time_taken_ms=rng.randint(low, high) if group.get("destination_id") else None,
                msg=group.get("msg") or _default_msg(group.get("status")),
                rng=rng,
            )
        )
    return lines


def _event_history(spec: dict[str, Any], at, rng: random.Random) -> list[dict[str, Any]]:
    """Log lines for one specific event: optional publish line, then each delivery attempt."""
    lines = []
    if "published_min" in spec:
        lines.append(
            _line(
                source="events_service", time_ms=at(spec["published_min"]), level="INFO",
                tenant_id=spec["tenant_id"], event_id=spec["event_id"], event_type=spec["event_type"],
                destination_id=None, request_id=None, attempt=None, status=None, time_taken_ms=None,
                msg=f"event published to {spec.get('topic', 'kafka')}", rng=rng,
            )
        )
    for n, attempt in enumerate(spec.get("attempts", []), start=1):
        status = attempt["status"]
        if 200 <= status < 300:
            msg = "delivered"
        elif n >= 7:
            msg = "max retries exceeded, moved to DLQ"
        else:
            msg = "delivery failed, scheduling retry"
        lines.append(
            _line(
                source="webhook_service", time_ms=at(attempt["at_min"]),
                level="INFO" if status < 400 else "WARN",
                tenant_id=spec["tenant_id"], event_id=spec["event_id"], event_type=spec["event_type"],
                destination_id=spec["destination_id"], request_id=_id(rng, "req_", 8), attempt=n,
                status=status, time_taken_ms=attempt.get("time_taken_ms", 250), msg=msg, rng=rng,
            )
        )
    return lines


def _line(*, source, time_ms, level, tenant_id, event_id, event_type, destination_id, request_id,
          attempt, status, time_taken_ms, msg, rng) -> dict[str, Any]:
    return {
        "source": source,
        "time_ms": time_ms,
        "level": level,
        "service": _SERVICE_NAMES.get(source, source),
        "tenant_id": tenant_id,
        "event_id": event_id,
        "event_type": event_type,
        "destination_id": destination_id,
        "request_id": request_id,
        "attempt": attempt,
        "status": status,
        "time_taken_ms": time_taken_ms,
        "msg": msg,
        "trace_id": _id(rng, "tr_", 10),
    }


def _default_msg(status: int | None) -> str:
    if status is None:
        return "event processed"
    if 200 <= status < 300:
        return "delivered"
    return "delivery failed, scheduling retry"


def _deployment(spec: dict[str, Any], at) -> dict[str, Any]:
    started = at(spec["at_min"])
    finished = started + int(spec.get("duration_min", 6) * MINUTE_MS)
    return {
        "service": spec["service"],
        "repo": f"example-org/{spec['service']}",
        "workflow": ".github/workflows/deploy-ecs.yml",
        "run_id": spec.get("run_id", zlib.crc32(f"{spec['service']}{spec['at_min']}".encode()) % 10_000_000),
        "started_ms": started,
        "finished_ms": finished,
        "commit": spec.get("commit", "0000000"),
        "pr_number": spec.get("pr_number"),
        "pr_title": spec.get("pr_title"),
        "author": spec.get("author"),
        "conclusion": spec.get("conclusion", "success"),
        "rollout_state": spec.get("rollout", "COMPLETED"),
        "rollout_completed_ms": finished + 3 * MINUTE_MS if spec.get("rollout", "COMPLETED") == "COMPLETED" else None,
    }
