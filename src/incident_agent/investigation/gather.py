"""Runs a playbook's checks in parallel through the shared tool runner."""

import json
from concurrent.futures import ThreadPoolExecutor, wait

from langchain_core.tools import BaseTool

from incident_agent.audit import AuditLog
from incident_agent.investigation.models import Check, Evidence
from incident_agent.tools.runner import run_tool_call

MAX_PARALLEL_CHECKS = 8


def gather_evidence(
    checks: list[Check],
    tools: list[BaseTool],
    *,
    timeout_seconds: float,
    audit: AuditLog | None = None,
) -> list[Evidence]:
    """Never raises: a check that fails, times out or has no tool yet becomes evidence of that."""
    tools_by_name = {t.name: t for t in tools}
    results: dict[str, Evidence] = {}
    runnable = []
    for check in checks:
        if check.tool in tools_by_name:
            runnable.append(check)
        else:
            results[check.name] = _evidence(check, "skipped", f"tool {check.tool} not available yet")

    pool = ThreadPoolExecutor(max_workers=MAX_PARALLEL_CHECKS)
    futures = {
        pool.submit(
            run_tool_call,
            tools_by_name,
            {"id": check.name, "name": check.tool, "args": check.args},
            audit,
        ): check
        for check in runnable
    }
    done, pending = wait(futures, timeout=timeout_seconds)
    for future in done:
        check = futures[future]
        message = future.result()
        artifact = message.artifact or {}
        status = "ok" if artifact.get("ok") else "error"
        data = _json(message.content) if status == "ok" else None
        results[check.name] = _evidence(
            check, status, artifact.get("summary", ""), data, artifact.get("duration_ms")
        )
    for future in pending:
        check = futures[future]
        results[check.name] = _evidence(check, "timeout", f"no result within {timeout_seconds:g}s")
    # Don't wait for timed-out checks; their threads finish in the background.
    pool.shutdown(wait=False, cancel_futures=True)

    return [results[c.name] for c in checks]


def _evidence(
    check: Check,
    status: str,
    summary: str,
    data: dict | None = None,
    duration_ms: int | None = None,
) -> Evidence:
    return Evidence(
        check=check.name,
        tool=check.tool,
        args=check.args,
        purpose=check.purpose,
        status=status,  # type: ignore[arg-type]
        summary=summary,
        data=data,
        duration_ms=duration_ms,
    )


def _json(content: object) -> dict | None:
    try:
        parsed = json.loads(content) if isinstance(content, str) else None
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None
