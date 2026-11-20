"""The KV cache the experiment files split their rows by is the one their profile states (P4.11):
model.kv_cache_tokens, which the cluster panel's cache pressure also divides by."""

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENTS = sorted((REPO_ROOT / "experiments").glob("*.yaml"))
# Files that run on simulated pods but count "over" against a real pod's cache, so a call is
# over when it would wait for KV blocks on real vLLM; each says so in its comment (P2.9, P3.1).
MEASURED_AGAINST_REAL = {"self-built-check.yaml", "validate-sim.yaml"}


def load(path: Path) -> Any:
    return yaml.safe_load(path.read_text())


def kv_cache_tokens(profile: str) -> int:
    tokens: int = load(REPO_ROOT / "config" / f"{profile}.yaml")["model"]["kv_cache_tokens"]
    return tokens


def with_kv_capacity() -> list[Path]:
    return [path for path in EXPERIMENTS if "kv_capacity_tokens_per_pod" in load(path)]


def test_the_experiment_files_are_found() -> None:
    names = {path.name for path in with_kv_capacity()}
    assert {"baseline.yaml", "autoscale-sim.yaml"} | MEASURED_AGAINST_REAL <= names


@pytest.mark.parametrize("path", with_kv_capacity(), ids=lambda path: path.name)
def test_each_file_holds_its_profiles_kv_cache(path: Path) -> None:
    spec = load(path)
    profile = "real" if path.name in MEASURED_AGAINST_REAL else spec["profile"]
    assert spec["kv_capacity_tokens_per_pod"] == kv_cache_tokens(profile)


def test_the_simulated_pods_cache_is_its_blocks() -> None:
    """A simulated pod holds simulator.kv_cache_blocks of block_size tokens; the profile's
    kv_cache_tokens states the same number for the panel and the charts, which read model."""
    simulator = load(REPO_ROOT / "config" / "sim.yaml")["simulator"]
    assert kv_cache_tokens("sim") == simulator["kv_cache_blocks"] * simulator["block_size"]


def test_plain_and_adapter_pods_hold_the_same_cache() -> None:
    """real-plain is real without the adapter (P2.8): the same KV cache bytes, the same tokens."""
    assert kv_cache_tokens("real-plain") == kv_cache_tokens("real")
