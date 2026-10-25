"""`get_recent_deployments`: deploys of the platform services in a window."""

from datetime import UTC, datetime
from typing import Literal

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from incident_agent.backends.base import DeploymentsBackend
from incident_agent.tools.common import ToolResult, error, hhmm, iso, ok, to_ms

Service = Literal["events-service", "webhook-service"]

DESCRIPTION = """\
Deploys of events-service and/or webhook-service since a time: GitHub Actions deploy runs on main
(time, commit, PR title/author/link, outcome) and the ECS rollout state and completion time."""


class GetDeploymentsInput(BaseModel):
    services: list[Service] = Field(default=["events-service", "webhook-service"])
    since: datetime = Field(description="ISO-8601 UTC.")


def get_recent_deployments(backend: DeploymentsBackend, services: list[str], since: datetime) -> ToolResult:
    now_ms = to_ms(datetime.now(UTC))
    since_ms = to_ms(since)
    if since_ms >= now_ms:
        return error("since must be in the past.")
    runs = backend.list_deployments(services, since_ms, now_ms)
    deployments = [
        {
            "service": r["service"],
            "repo": r["repo"],
            "workflow": r["workflow"],
            "run_id": r["run_id"],
            "started_at": iso(r["started_ms"]),
            "finished_at": iso(r["finished_ms"]),
            "conclusion": r["conclusion"],
            "commit": r["commit"],
            "pr": {
                "number": r["pr_number"],
                "title": r["pr_title"],
                "author": r["author"],
                "url": f"https://github.com/{r['repo']}/pull/{r['pr_number']}",
            } if r["pr_number"] else None,
            "ecs_rollout": {
                "state": r["rollout_state"],
                "completed_at": iso(r["rollout_completed_ms"]) if r["rollout_completed_ms"] else None,
            },
        }
        for r in runs
    ]
    payload = {"services": services, "since": iso(since_ms), "deployments": deployments}
    if not runs:
        return ok(payload, f"no deploys since {hhmm(since_ms)}")
    latest = runs[0]
    pr = f" PR #{latest['pr_number']}" if latest["pr_number"] else ""
    return ok(payload, f"{len(runs)} deploy(s); latest {latest['service']}{pr} at {hhmm(latest['finished_ms'])}")


def make_get_deployments_tool(backend: DeploymentsBackend) -> BaseTool:
    def _run(since: datetime, services: list[str] | None = None) -> ToolResult:
        return get_recent_deployments(backend, services or ["events-service", "webhook-service"], since)

    return StructuredTool.from_function(
        func=_run, name="get_recent_deployments", description=DESCRIPTION,
        args_schema=GetDeploymentsInput, response_format="content_and_artifact",
    )
