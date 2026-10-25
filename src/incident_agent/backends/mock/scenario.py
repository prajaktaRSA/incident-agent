"""Expands a scenario YAML spec into concrete mock items.

Specs describe failure *groups* (tenant, destination, status, count, time range) instead of
hundreds of literal rows. Times are minutes relative to "now", so every scenario looks like it
is happening right now and time-window queries always hit data. Generation is seeded by the
scenario name, so the same scenario always produces the same items for a given "now".
"""

import json
import random
import string
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MINUTE_MS = 60_000


@dataclass
class Scenario:
    name: str
    description: str
    failed_events: list[dict[str, Any]] = field(default_factory=list)
    dlq_events: list[dict[str, Any]] = field(default_factory=list)


def available_scenarios(fixtures_dir: Path) -> list[str]:
    return sorted(p.stem for p in (fixtures_dir / "scenarios").glob("*.yaml"))


def load_scenario(fixtures_dir: Path, name: str, now_ms: int | None = None) -> Scenario:
    path = fixtures_dir / "scenarios" / f"{name}.yaml"
    if not path.exists():
        known = ", ".join(available_scenarios(fixtures_dir)) or "none"
        raise FileNotFoundError(f"Unknown scenario '{name}'. Available: {known}")
    spec = yaml.safe_load(path.read_text())
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    rng = random.Random(name)

    failed = [
        item
        for group in spec.get("failed_events", [])
        for item in _expand_group(group, now_ms, rng, dlq=False)
    ]
    dlq = [
        item
        for group in spec.get("dlq_events", [])
        for item in _expand_group(group, now_ms, rng, dlq=True)
    ]
    return Scenario(
        name=spec["name"],
        description=spec.get("description", "").strip(),
        failed_events=failed,
        dlq_events=dlq,
    )


def _expand_group(
    group: dict[str, Any], now_ms: int, rng: random.Random, *, dlq: bool
) -> list[dict[str, Any]]:
    start_ms = now_ms + group["from_min"] * MINUTE_MS
    end_ms = now_ms + group.get("to_min", 0) * MINUTE_MS
    failure_type = group.get("failure_type", "DELIVERY_ERROR")
    is_config_fetch = failure_type == "CONFIG_FETCH_ERROR"
    event_types = group.get("event_types", ["payment.succeeded"])
    retry_low, retry_high = group.get("retry_attempts", [1, 7])

    items = []
    for _ in range(group["count"]):
        at_ms = rng.randint(start_ms, max(start_ms, end_ms - 1))
        event_id = "evt_" + "".join(rng.choices(string.ascii_lowercase + string.digits, k=12))
        event_type = rng.choice(event_types)
        event_content = json.dumps(
            {
                "id": event_id,
                "type": event_type,
                "tenant_id": group["tenant_id"],
                # Stand-in for the customer/payment data a real event carries.
                "data": {"customer_email": "jane.doe@example.com", "amount": rng.randint(5, 900)},
            }
        )
        if is_config_fetch:
            destination_id, destination_type = None, "CONFIG_FETCH"
            destination_key = "NONE:CONFIG_FETCH"
        else:
            destination_id, destination_type = group["destination_id"], "WEBHOOK"
            destination_key = f"{destination_id}:WEBHOOK"

        item: dict[str, Any] = {
            "eventId": event_id,
            "destinationKey": destination_key,
            "tenant_id": group["tenant_id"],
            "destinationId": destination_id,
            "destinationType": destination_type,
            "failureStatusCode": group["status"],
            "failureMessage": group["message"],
            "eventContent": event_content,
        }
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
