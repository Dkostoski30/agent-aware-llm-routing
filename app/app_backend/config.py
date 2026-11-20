"""The app backend's settings, read from the app section of a config/ profile (D11), and the
agent, monitoring and model settings it must agree with."""

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ScryptParams:
    """hashlib.scrypt's cost: n (CPU and memory), r (block size), p (parallel lanes)."""

    n: int
    r: int
    p: int


@dataclass(frozen=True)
class AppConfig:
    session_ttl_seconds: int
    scrypt: ScryptParams
    min_password_length: int
    cookie_secure: bool
    # agent.max_files: a run carries the chat's newest files up to it, so the agent runtime never
    # refuses a run for having too many (P3.12a).
    max_files: int
    runtime_timeout_seconds: float
    # monitoring.scrape_interval_seconds: the cluster panel asks again after it, since Prometheus
    # has nothing newer sooner and asking later would lag Grafana (P3.11). Also the panel's
    # Prometheus timeout: an answer later than the next poll is no use.
    cluster_poll_seconds: int
    # model.kv_cache_tokens: one pod's KV cache, which the panel's cache pressure divides the
    # active runs' context by (P4.11).
    kv_cache_tokens: int
    # The cache pressure the panel shows in the warning colour from (P4.11).
    pressure_warn: float


def load_config(path: Path) -> AppConfig:
    """Reads the app section of a profile, agent.max_files, monitoring.scrape_interval_seconds and
    model.kv_cache_tokens; a missing key raises KeyError."""
    profile = yaml.safe_load(path.read_text())
    app = profile["app"]
    return AppConfig(
        session_ttl_seconds=app["session_ttl_seconds"],
        scrypt=ScryptParams(n=app["scrypt"]["n"], r=app["scrypt"]["r"], p=app["scrypt"]["p"]),
        min_password_length=app["min_password_length"],
        cookie_secure=app["cookie_secure"],
        max_files=profile["agent"]["max_files"],
        runtime_timeout_seconds=app["runtime_timeout_seconds"],
        cluster_poll_seconds=profile["monitoring"]["scrape_interval_seconds"],
        kv_cache_tokens=profile["model"]["kv_cache_tokens"],
        pressure_warn=app["pressure_warn"],
    )
