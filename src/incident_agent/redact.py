"""Strip secrets from text before it reaches the LLM, the terminal or the audit log."""

import re

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\b(authorization)\s*[:=]\s*\S+(\s+\S+)?"), r"\1: [REDACTED]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/=]{8,}"), "Basic [REDACTED]"),
    (
        re.compile(r"(?i)\b(api[_-]?key|secret|token|password|signature|sig)\s*[=:]\s*[^\s&\"',]+"),
        r"\1=[REDACTED]",
    ),
    # Query strings on URLs often carry credentials; keep the host and path only.
    (re.compile(r"(https?://[^\s?\"']+)\?[^\s\"']*"), r"\1?[REDACTED]"),
]


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
