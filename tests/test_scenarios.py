"""Every scenario in evals/expectations.yaml, end to end without an LLM.

Checks that intake, playbooks, mock data, tools and triage lead to the expected cause. The eval
runner (M3) checks the same scenarios with the real model writing the RCA.
"""

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from conftest import FIXTURES_DIR
from incident_agent.backends.mock import load_scenario
from incident_agent.backends.mock.triggers import load_trigger
from incident_agent.config import load_metrics_config, load_topic_mapping
from incident_agent.intake import intake
from incident_agent.investigation.graph import build_investigation_graph
from incident_agent.investigation.rca import RCA, RcaDraft
from incident_agent.settings import Settings
from incident_agent.tools import build_mock_tools

EXPECTATIONS = yaml.safe_load((Path(__file__).parents[1] / "evals" / "expectations.yaml").read_text())["scenarios"]
INVESTIGATIONS = {k: v for k, v in EXPECTATIONS.items() if "category" in v}


class TopHypothesisWriter:
    """Stands in for the LLM: an RCA for the top hypothesis citing everything that supported it."""

    def __call__(self, messages):
        bundle = json.loads(messages[1].content)
        top = bundle["triage_hypotheses"][0]
        rca = RCA(
            title=top["category"], category=top["category"], summary="s", root_cause="; ".join(top["reasons"]),
            confidence="high", impact="i", next_steps=["n"], unknowns=[], timeline=[],
            evidence=[{"claim": r, "source": c} for r, c in zip(top["reasons"], top["supporting_checks"])],
        )
        return RcaDraft(rca=rca, model="scripted")


def _run(name: str, spec: dict, tmp_path):
    settings = Settings(_env_file=None, outbox_dir=tmp_path / "o", records_dir=tmp_path / "r", logs_dir=tmp_path / "l")
    now = datetime.now(UTC)
    result = intake(load_trigger(FIXTURES_DIR / "triggers" / f"{spec['trigger']}.json", now), settings, now)
    assert result.request is not None, result.questions
    data = load_scenario(FIXTURES_DIR, spec["scenario"], now_ms=int(now.timestamp() * 1000))
    graph = build_investigation_graph(
        tools=build_mock_tools(data), rca_writer=TopHypothesisWriter(),
        topics=load_topic_mapping(), metrics=load_metrics_config(), settings=settings,
    )
    return graph.invoke({"request": result.request, "started_at": time.perf_counter()})


@pytest.mark.parametrize("name", sorted(INVESTIGATIONS))
def test_scenario_reaches_expected_cause(name, tmp_path):
    spec = INVESTIGATIONS[name]
    state = _run(name, spec, tmp_path)

    evidence = {e.check: e for e in state["evidence"]}
    failed = {c: e.summary for c, e in evidence.items() if e.status != "ok"}
    assert not failed, f"checks failed: {failed}"

    top = state["hypotheses"][0]
    assert top.category == spec["category"], [(h.category, h.score, h.reasons) for h in state["hypotheses"]]
    for check in spec.get("must_cite", []):
        assert check in top.supporting_checks, f"{check} not in {top.supporting_checks}"
    assert evidence["runbooks"].data["results"], "no runbook matched"


def test_missing_fields_asks_expected_questions():
    spec = EXPECTATIONS["missing_fields"]
    now = datetime.now(UTC)
    result = intake(load_trigger(FIXTURES_DIR / "triggers" / f"{spec['trigger']}.json", now), Settings(_env_file=None), now)
    assert [q.field for q in result.questions] == spec["questions"]
