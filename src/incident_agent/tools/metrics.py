"""`get_cloudwatch_metric`: one allowlisted metric, summarized, with its alarms."""

from datetime import datetime, timedelta
from statistics import median

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from incident_agent.backends.base import MetricsBackend
from incident_agent.config import MetricDef, MetricsConfig
from incident_agent.tools.common import ToolResult, error, hhmm, iso, ok, to_ms, window_error

MAX_WINDOW = timedelta(days=7)
MAX_POINTS = 30


class GetMetricInput(BaseModel):
    metric: str = Field(description="Allowlisted metric key, e.g. 'kafka.time_lag'.")
    dimensions: dict[str, str] = Field(
        default_factory=dict,
        description="Dimension name → value, e.g. {'Consumer Group': 'webhook-delivery-cg', 'Topic': 'payment-events'}.",
    )
    start: datetime = Field(description="ISO-8601 UTC.")
    end: datetime = Field(description="ISO-8601 UTC.")


def get_cloudwatch_metric(
    backend: MetricsBackend,
    config: MetricsConfig,
    metric: str,
    start: datetime,
    end: datetime,
    dimensions: dict[str, str] | None = None,
) -> ToolResult:
    definition = config.get(metric)
    if definition is None:
        return error(f"Metric '{metric}' is not in the allowlist. Allowed: {', '.join(sorted(config.metrics))}.")
    dimensions = dimensions or {}
    unknown = sorted(set(dimensions) - set(definition.dimensions))
    if unknown:
        return error(f"Unknown dimension(s) {unknown} for {metric}; valid: {list(definition.dimensions)}.")
    if problem := window_error(start, end, MAX_WINDOW):
        return error(problem)

    start_ms, end_ms = to_ms(start), to_ms(end)
    period_s = _period(end_ms - start_ms)
    series = backend.get_series(definition, dimensions, start_ms, end_ms, period_s)
    if not series:
        return ok({"metric": metric, "dimensions": dimensions, "points": []}, f"{metric}: no data")

    values = [v for _, v in series]
    baseline = median(values)
    spikes = _spikes(series, baseline, definition)
    peak = max(values)
    status = _status(peak, definition)
    alarms = [
        {
            "name": a["name"],
            "level": a["level"],
            "threshold": a["threshold"],
            "comparison": a["comparison"],
            "state_at_end": a["state"],
            "state_changes": [{"at": iso(c["time_ms"]), "state": c["state"]} for c in a["state_changes"]],
        }
        for a in backend.get_alarms(definition, dimensions, start_ms, end_ms)
    ]
    payload = {
        "metric": metric,
        "namespace": definition.namespace,
        "name": definition.name,
        "stat": definition.stat,
        "unit": definition.unit,
        "dimensions": dimensions,
        "window": {"start": iso(start_ms), "end": iso(end_ms)},
        "period_s": period_s,
        "min": min(values),
        "max": peak,
        "avg": round(sum(values) / len(values), 2),
        "last": values[-1],
        "baseline": baseline,
        "status": status,
        "thresholds": {"warning": definition.warning, "critical": definition.critical},
        "spikes": spikes,
        "points": [[iso(t), v] for t, v in _downsample(series)],
        "alarms": alarms,
    }
    return ok(payload, _summary(definition, dimensions, payload))


def _period(span_ms: int) -> int:
    hours = span_ms / 3_600_000
    return 60 if hours <= 3 else 300 if hours <= 24 else 3600


def _spikes(series: list[tuple[int, float]], baseline: float, definition: MetricDef) -> list[dict]:
    """Runs of points well above the window's median (by at least `min_spike` and 2×)."""
    spikes, current = [], None
    for t, v in series:
        high = v - baseline >= definition.min_spike and (baseline == 0 or v >= 2 * baseline)
        if high and current is None:
            current = {"start": t, "end": t, "peak": v}
        elif high:
            current["end"], current["peak"] = t, max(current["peak"], v)
        elif current is not None:
            spikes.append(current)
            current = None
    if current is not None:
        spikes.append(current)
    return [{"start": iso(s["start"]), "end": iso(s["end"]), "peak": s["peak"]} for s in spikes]


def _status(peak: float, definition: MetricDef) -> str:
    if definition.critical is not None and peak >= definition.critical:
        return "critical"
    if definition.warning is not None and peak >= definition.warning:
        return "warning"
    return "normal"


def _downsample(series: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """At most MAX_POINTS points, keeping each bucket's maximum so spikes stay visible."""
    if len(series) <= MAX_POINTS:
        return series
    size = -(-len(series) // MAX_POINTS)
    return [max(series[i : i + size], key=lambda p: p[1]) for i in range(0, len(series), size)]


def _summary(definition: MetricDef, dimensions: dict[str, str], payload: dict) -> str:
    scope = dimensions.get("Topic") or dimensions.get("ServiceName") or ""
    label = f"{definition.key}{f' {scope}' if scope else ''}"
    text = f"{label}: max {payload['max']:g} {definition.unit} (baseline {payload['baseline']:g}) · {payload['status']}"
    if payload["spikes"]:
        first = payload["spikes"][0]
        text += f" · spike from {hhmm(to_ms(datetime.fromisoformat(first['start'])))}"
    return text


def make_get_metric_tool(backend: MetricsBackend, config: MetricsConfig) -> BaseTool:
    def _run(metric: str, start: datetime, end: datetime, dimensions: dict[str, str] | None = None) -> ToolResult:
        return get_cloudwatch_metric(backend, config, metric, start, end, dimensions)

    keys = ", ".join(sorted(config.metrics))
    return StructuredTool.from_function(
        func=_run,
        name="get_cloudwatch_metric",
        description=(
            "One CloudWatch metric over a window: min/max/avg/last, baseline (median), status "
            "against alarm thresholds, detected spikes, up to 30 points, and the metric's alarms "
            f"with state changes. Allowed metric keys: {keys}."
        ),
        args_schema=GetMetricInput,
        response_format="content_and_artifact",
    )
