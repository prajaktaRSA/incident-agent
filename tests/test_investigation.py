"""End to end: trigger fixture → intake → graph → outbox and record (scripted RCA writer)."""

import json
import time
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from conftest import FIXTURES_DIR
from incident_agent.apps import cli
from incident_agent.audit import AuditLog
from incident_agent.backends.mock import load_scenario
from incident_agent.backends.mock.triggers import load_trigger
from incident_agent.config import load_metrics_config, load_topic_mapping
from incident_agent.intake import intake
from incident_agent.investigation.graph import build_investigation_graph
from incident_agent.investigation.models import Category
from incident_agent.investigation.rca import RCA, RcaDraft
from incident_agent.settings import Settings, get_settings
from incident_agent.tools import build_mock_tools


class EchoTopHypothesis:
    """Stands in for the LLM: writes an RCA for the top hypothesis, citing failed_events."""

    def __call__(self, messages):
        bundle = json.loads(messages[1].content)
        top = bundle["triage_hypotheses"][0]
        rca = RCA(
            title=f"{top['category']} for {bundle['investigation']['site_id']}",
            category=top["category"], summary="s", root_cause="; ".join(top["reasons"]),
            confidence="medium", evidence=[{"claim": top["reasons"][0], "source": "failed_events"}],
            timeline=[], impact="acme", next_steps=["contact merchant"],
            unknowns=[c["check"] for c in bundle["checks_not_run"]],
        )
        return RcaDraft(rca=rca, model="scripted")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(_env_file=None, outbox_dir=tmp_path / "outbox", records_dir=tmp_path / "records",
                    logs_dir=tmp_path / "logs")


def test_merchant_down_from_slack_form_to_outbox(settings):
    now = datetime.now(UTC)
    result = intake(load_trigger(FIXTURES_DIR / "triggers" / "merchant_down.json", now), settings, now)
    request = result.request
    scenario = load_scenario(FIXTURES_DIR, "merchant_down", now_ms=int(now.timestamp() * 1000))
    audit = AuditLog(settings.logs_dir, request.id)
    graph = build_investigation_graph(
        tools=build_mock_tools(scenario), rca_writer=EchoTopHypothesis(),
        topics=load_topic_mapping(settings.config_dir), metrics=load_metrics_config(), settings=settings, audit=audit,
    )

    state = graph.invoke({"request": request, "started_at": time.perf_counter()})

    evidence = {e.check: e for e in state["evidence"]}
    assert all(e.status == "ok" for e in evidence.values()), {c: e.summary for c, e in evidence.items()}
    assert evidence["runbooks"].data["results"][0]["path"] == "docs/merchant-endpoint-5xx.md"
    assert state["hypotheses"][0].category == Category.MERCHANT_ENDPOINT_DOWN

    message = (settings.outbox_dir / f"{request.id}.md").read_text()
    assert message.startswith("<!-- reply_to: C0WEBHOOKS/1729861234.000100 -->")
    assert "Automated RCA — internal" in message
    assert "`MERCHANT_ENDPOINT_DOWN`" in message
    assert "SUP-4821" in message  # Jira link shown for reference
    assert f"{len(evidence)} checks run · 0 failed or unavailable" in message

    record = json.loads((settings.records_dir / f"{request.id}.json").read_text())
    assert record["rca"]["category"] == "MERCHANT_ENDPOINT_DOWN"
    assert record["rca_meta"] == {"model": "scripted", "attempts": 1, "deterministic": False, "problems": []}
    assert "eventContent" not in json.dumps(record)

    kinds = [json.loads(line)["kind"] for line in audit.path.read_text().splitlines()]
    assert kinds[0] == "playbook" and kinds[-1] == "published"
    assert {"tool_call", "triage", "rca"} <= set(kinds)


def test_missing_fields_get_questions_and_no_investigation(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTBOX_DIR", str(tmp_path / "outbox"))
    monkeypatch.setenv("RECORDS_DIR", str(tmp_path / "records"))
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(
            cli.app, ["investigate", "--trigger", str(FIXTURES_DIR / "triggers" / "missing_fields.json")]
        )
    finally:
        get_settings.cache_clear()

    assert result.exit_code == 0, result.output
    assert "Which site ID is this about?" in result.output
    (outbox,) = (tmp_path / "outbox").glob("questions-*.md")
    text = outbox.read_text()
    assert "@se.raj" in text
    assert "Roughly when did it start?" in text
    assert "Which event type or event ID" in text
    assert not (tmp_path / "records").exists()
