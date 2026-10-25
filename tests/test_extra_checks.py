"""The optional extra-checks round in the investigation graph."""

import json
import time
from datetime import UTC, datetime, timedelta

from conftest import FIXTURES_DIR
from incident_agent.backends.mock import load_scenario
from incident_agent.backends.mock.triggers import load_trigger
from incident_agent.config import load_metrics_config, load_topic_mapping
from incident_agent.intake import intake
from incident_agent.investigation.graph import build_investigation_graph
from incident_agent.investigation.rca import RCA, RcaDraft
from incident_agent.settings import Settings
from incident_agent.tools import build_mock_tools


class Writer:
    """Asks for more checks on the first call (if allowed), then writes a final RCA."""

    def __init__(self, ask: bool = True):
        self.ask = ask
        self.bundles: list[dict] = []

    def __call__(self, messages):
        bundle = json.loads(messages[1].content)
        self.bundles.append(bundle)
        top = bundle["triage_hypotheses"][0]
        wants = ["compare acme failures with the same window yesterday"] if self.ask and bundle["extra_checks_allowed"] else []
        return RcaDraft(
            rca=RCA(
                title="t", category=top["category"], summary="s", root_cause="r", confidence="medium",
                evidence=[{"claim": "c", "source": "failed_events"}], timeline=[], impact="i",
                next_steps=["n"], unknowns=[], needs_more_checks=wants,
            ),
            model="scripted",
        )


class Planner:
    def __init__(self, calls):
        self.calls = calls
        self.seen: list = []

    def __call__(self, messages):
        self.seen.append(messages)
        return self.calls


def _run(writer, planner, tmp_path):
    settings = Settings(_env_file=None, outbox_dir=tmp_path / "o", records_dir=tmp_path / "r", logs_dir=tmp_path / "l")
    now = datetime.now(UTC)
    request = intake(load_trigger(FIXTURES_DIR / "triggers" / "merchant_down.json", now), settings, now).request
    graph = build_investigation_graph(
        tools=build_mock_tools(load_scenario(FIXTURES_DIR, "merchant_down")), rca_writer=writer,
        extra_check_planner=planner, topics=load_topic_mapping(), metrics=load_metrics_config(), settings=settings,
    )
    return graph.invoke({"request": request, "started_at": time.perf_counter()})


def _yesterday_call(n: int) -> dict:
    end = datetime.now(UTC) - timedelta(days=1)
    return {
        "id": f"c{n}", "name": "query_failed_events",
        "args": {"site_id": "acme", "start": (end - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "end": end.strftime("%Y-%m-%dT%H:%M:%SZ")},
    }


def test_requested_checks_run_and_the_rca_is_rewritten_once(tmp_path):
    writer, planner = Writer(), Planner([_yesterday_call(1)])
    state = _run(writer, planner, tmp_path)

    extra = [e for e in state["evidence"] if e.check.startswith("extra_")]
    assert [e.check for e in extra] == ["extra_1_query_failed_events"]
    assert extra[0].status == "ok"
    assert len(writer.bundles) == 2
    assert [b["extra_checks_allowed"] for b in writer.bundles] == [True, False]
    assert "extra_1_query_failed_events" in [c["check"] for c in writer.bundles[1]["checks"]]
    assert "compare acme failures" in planner.seen[0][1].content
    assert "(incl. 1 extra)" in state["message"]


def test_at_most_three_extra_checks(tmp_path):
    state = _run(Writer(), Planner([_yesterday_call(n) for n in range(1, 6)]), tmp_path)
    assert len([e for e in state["evidence"] if e.check.startswith("extra_")]) == 3


def test_no_planned_checks_keeps_the_first_draft(tmp_path):
    writer = Writer()
    state = _run(writer, Planner([]), tmp_path)
    assert len(writer.bundles) == 1
    assert "Suggested follow-up checks" in state["message"]


def test_no_round_when_the_draft_is_confident(tmp_path):
    writer, planner = Writer(ask=False), Planner([_yesterday_call(1)])
    _run(writer, planner, tmp_path)
    assert len(writer.bundles) == 1 and planner.seen == []


def test_without_a_planner_extra_checks_are_not_offered(tmp_path):
    writer = Writer()
    _run(writer, None, tmp_path)
    assert writer.bundles[0]["extra_checks_allowed"] is False
