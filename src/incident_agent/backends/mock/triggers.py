"""Loads trigger fixtures, resolving relative times like "-95m" against now."""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_RELATIVE = re.compile(r"^([+-]\d+)([mhd])$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}
# Only these keys hold times; nothing else in a trigger is rewritten.
_TIME_KEYS = {"started_at", "StateChangeTime"}


def load_trigger(path: Path, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    return _resolve(json.loads(path.read_text()), now)


def _resolve(value: Any, now: datetime, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {k: _resolve(v, now, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, now) for v in value]
    if key in _TIME_KEYS and isinstance(value, str) and (match := _RELATIVE.match(value)):
        amount, unit = match.groups()
        moment = now + timedelta(**{_UNITS[unit]: int(amount)})
        return moment.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return value
