"""Static configuration files under config/."""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from incident_agent.settings import get_settings


@dataclass(frozen=True)
class Topic:
    name: str
    consumer_group: str
    event_types: tuple[str, ...]


@dataclass(frozen=True)
class TopicMapping:
    cluster: str
    topics: tuple[Topic, ...]

    @property
    def event_types(self) -> set[str]:
        return {t for topic in self.topics for t in topic.event_types}

    def topic_for(self, event_type: str) -> Topic | None:
        return next((t for t in self.topics if event_type in t.event_types), None)


@dataclass(frozen=True)
class MetricDef:
    key: str
    namespace: str
    name: str
    stat: str
    unit: str
    dimensions: tuple[str, ...]
    warning: float | None
    critical: float | None
    min_spike: float


@dataclass(frozen=True)
class MetricsConfig:
    metrics: dict[str, MetricDef]

    def get(self, key: str) -> MetricDef | None:
        return self.metrics.get(key)

    def find(self, namespace: str, name: str) -> MetricDef | None:
        return next(
            (m for m in self.metrics.values() if m.namespace == namespace and m.name == name), None
        )


def load_topic_mapping(config_dir: Path | None = None) -> TopicMapping:
    return _load_topic_mapping(config_dir or get_settings().config_dir)


def load_known_sites(config_dir: Path | None = None) -> frozenset[str]:
    return _load_known_sites(config_dir or get_settings().config_dir)


def load_metrics_config(config_dir: Path | None = None) -> MetricsConfig:
    return _load_metrics_config(config_dir or get_settings().config_dir)


def load_log_sources(config_dir: Path | None = None) -> dict[str, dict[str, str]]:
    return _load_log_sources(config_dir or get_settings().config_dir)


@lru_cache
def _load_metrics_config(config_dir: Path) -> MetricsConfig:
    raw = yaml.safe_load((config_dir / "metrics.yaml").read_text())["metrics"]
    metrics = {
        key: MetricDef(
            key=key,
            namespace=spec["namespace"],
            name=spec["name"],
            stat=spec["stat"],
            unit=spec["unit"],
            dimensions=tuple(spec["dimensions"]),
            warning=(spec.get("alarms") or {}).get("warning"),
            critical=(spec.get("alarms") or {}).get("critical"),
            min_spike=spec["min_spike"],
        )
        for key, spec in raw.items()
    }
    return MetricsConfig(metrics=metrics)


@lru_cache
def _load_log_sources(config_dir: Path) -> dict[str, dict[str, str]]:
    return yaml.safe_load((config_dir / "logs.yaml").read_text())["sources"]


@lru_cache
def _load_topic_mapping(config_dir: Path) -> TopicMapping:
    raw = yaml.safe_load((config_dir / "topic_mapping.yaml").read_text())
    topics = tuple(
        Topic(name=name, consumer_group=spec["consumer_group"], event_types=tuple(spec["event_types"]))
        for name, spec in raw["topics"].items()
    )
    return TopicMapping(cluster=raw["cluster"], topics=topics)


@lru_cache
def _load_known_sites(config_dir: Path) -> frozenset[str]:
    return frozenset(yaml.safe_load((config_dir / "sites.yaml").read_text())["sites"])
