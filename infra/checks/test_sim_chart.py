"""What the simulator's chart and profile must keep true (P2.7, card 30)."""

import math
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CHARTS = REPO_ROOT / "infra" / "charts"


def test_real_and_simulated_pods_show_on_one_dashboard() -> None:
    """The sim chart holds its own copy, so a panel added to one must reach the other."""
    real = (CHARTS / "vllm" / "dashboards" / "vllm.json").read_bytes()
    simulated = (CHARTS / "sim" / "dashboards" / "vllm.json").read_bytes()
    assert simulated == real


def test_the_simulated_kv_cache_can_hold_every_running_prompt() -> None:
    """A full cache makes the simulator answer HTTP 500 where real vLLM makes the call wait
    (pkg/kvcache/block_cache.go in v1.0.0), so running requests must always fit, even at the
    longest prompt the model takes."""
    profile = yaml.safe_load((REPO_ROOT / "config" / "sim.yaml").read_text())
    simulator = profile["simulator"]
    blocks_per_prompt = math.ceil(profile["model"]["max_model_len"] / simulator["block_size"])
    assert simulator["kv_cache_blocks"] >= simulator["max_num_seqs"] * blocks_per_prompt
