import time
from datetime import UTC, datetime, timedelta

from langchain_core.tools import StructuredTool

from incident_agent.backends.mock import MockFailedEventsBackend
from incident_agent.investigation.gather import gather_evidence
from incident_agent.investigation.models import Check
from incident_agent.tools.common import ok
from incident_agent.tools.failed_events import make_query_failed_events_tool


def _window() -> dict:
    end = datetime.now(UTC)
    return {"start": (end - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ")}


def _check(name: str, tool: str, args: dict) -> Check:
    return Check(name=name, tool=tool, args=args, purpose=f"purpose of {name}")


def _tool(name: str, func) -> StructuredTool:
    return StructuredTool.from_function(func=func, name=name, description=name,
                                        response_format="content_and_artifact")


def test_runs_available_checks_and_skips_missing_tools(merchant_down, tmp_path):
    tools = [make_query_failed_events_tool(MockFailedEventsBackend(merchant_down))]
    checks = [
        _check("failed_events", "query_failed_events", {"site_id": "acme", **_window()}),
        _check("deployments", "get_recent_deployments", {"since": "x"}),
    ]

    evidence = gather_evidence(checks, tools, timeout_seconds=5)

    assert [e.check for e in evidence] == ["failed_events", "deployments"]  # playbook order kept
    failed, deploys = evidence
    assert failed.status == "ok"
    assert failed.data["totals"] == {"failed": 412, "dlq": 37}
    assert failed.duration_ms is not None
    assert deploys.status == "skipped"
    assert "not available" in deploys.summary


def test_bad_arguments_become_error_evidence(merchant_down):
    tools = [make_query_failed_events_tool(MockFailedEventsBackend(merchant_down))]
    checks = [_check("failed_events", "query_failed_events", {"site_id": "acme"})]

    (evidence,) = gather_evidence(checks, tools, timeout_seconds=5)

    assert evidence.status == "error"
    assert evidence.data is None


def test_slow_checks_time_out_without_blocking_others():
    def slow() -> tuple[str, dict]:
        time.sleep(2)
        return ok({}, "late")

    def fast() -> tuple[str, dict]:
        return ok({"n": 1}, "fast")

    checks = [_check("slow", "slow", {}), _check("fast", "fast", {})]
    started = time.perf_counter()

    evidence = gather_evidence(checks, [_tool("slow", slow), _tool("fast", fast)], timeout_seconds=0.3)

    assert time.perf_counter() - started < 1.5
    assert [e.status for e in evidence] == ["timeout", "ok"]


def test_checks_are_audited(merchant_down, tmp_path):
    from incident_agent.audit import AuditLog

    audit = AuditLog(tmp_path, "inv1")
    tools = [make_query_failed_events_tool(MockFailedEventsBackend(merchant_down))]
    gather_evidence(
        [_check("failed_events", "query_failed_events", {"site_id": "acme", **_window()})],
        tools, timeout_seconds=5, audit=audit,
    )
    assert '"call_id": "failed_events"' in audit.path.read_text()
