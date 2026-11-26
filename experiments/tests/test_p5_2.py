"""P5.2's files (card 76, docs/task-cards/76-P5.2-autoscaling.md): E2's real 1<->2 square, its
square on simulated pods, and the replays with a scale-down in them.

Here, not in loadgen/tests, for the reason test_p5_squares.py gives: experiments/ is outside the
system hash that make autoscale-square holds equal across a square's files.
"""

import itertools
from pathlib import Path

import pytest
import yaml
from load_generator.autoscale_square import load_autoscale_series
from load_generator.experiment import (
    Experiment,
    check_autoscaling,
    check_routing,
    load_experiment,
)
from load_generator.plan import burst_starts, load_replay_config, start_times

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENTS = REPO_ROOT / "experiments"
PROFILES = {
    name: yaml.safe_load((REPO_ROOT / "config" / f"{name}.yaml").read_text())
    for name in ("real", "sim")
}
LETTERS = ("a", "b", "c", "d")
REAL_SQUARE = [EXPERIMENTS / f"p5-2-real-{letter}.yaml" for letter in LETTERS]
SIM_SQUARE = [EXPERIMENTS / f"p5-2-sim-{letter}.yaml" for letter in LETTERS]
SCALE_DOWN = {name: EXPERIMENTS / f"p5-2-scale-down-{name}.yaml" for name in ("real", "sim")}
# loadgen/tests/test_replay_plan.py's SQUARE_SEEDS (card 73), in the files' order.
SQUARE_SEEDS = [6, 9, 10, 13]
# The runs that start before the first burst at 120 s at each seed, as in P5.1's squares.
RUNS_BEFORE_THE_FIRST_BURST = [4, 5, 3, 4]
# P5.1a-2's check of the squares' load, which the square files repeat but for seed and variants.
HEAVY = EXPERIMENTS / "p5-1a-heavy.yaml"
# P4.5's eager files, whose Williams rows the real square keeps (A cpu, B pressure, C queue,
# D remaining).
EAGER = [EXPERIMENTS / f"autoscale-real-1to2-{letter}.yaml" for letter in LETTERS]
REAL_AUTOSCALERS = ["cpu", "pressure", "queue", "remaining"]
# cpu and pressure do not run on the simulator (D5; P3.7b).
SIM_AUTOSCALERS = ["queue", "remaining"]
REFERENCE = "queue"
# Kubernetes' HPA sync period, its default, which this cluster keeps (P3.9b saw the rise one 15 s
# sync after the crossing): a scale-down needs the window and then a sync.
HPA_SYNC_SECONDS = 15
# burst-gap's bursts, as many runs as burst-fan-out-heavy's.
GAP_BURST_RUNS = 10


def committed(path: Path) -> Experiment:
    profile = PROFILES[yaml.safe_load(path.read_text())["profile"]]
    return load_experiment(path, load_replay_config(profile["replay"]).patterns)


def orders(paths: list[Path]) -> list[list[str]]:
    return [[variant.name for variant in committed(path).variants] for path in paths]


@pytest.mark.parametrize(("paths", "profile"), [(REAL_SQUARE, "real"), (SIM_SQUARE, "sim")])
def test_each_square_file_runs_the_heavy_checks_load_at_its_own_seed(
    paths: list[Path], profile: str
) -> None:
    heavy = committed(HEAVY)
    files = [committed(path) for path in paths]
    for experiment in files:
        assert experiment.profile == profile
        assert (experiment.trace, experiment.pattern_name) == (heavy.trace, heavy.pattern_name)
        assert (
            experiment.kv_capacity_tokens_per_pod == PROFILES[profile]["model"]["kv_cache_tokens"]
        )
        warmup, checked = experiment.warmup, heavy.warmup
        assert (warmup.calls_per_pod, warmup.words, warmup.completion_tokens) == (
            checked.calls_per_pod,
            checked.words,
            checked.completion_tokens,
        )
    assert [experiment.seed for experiment in files] == SQUARE_SEEDS


def test_the_real_files_are_stall_checked_as_p5_1s() -> None:
    assert all(committed(path).pod_stall_seconds == 30 for path in REAL_SQUARE)


@pytest.mark.parametrize("paths", [REAL_SQUARE, SIM_SQUARE], ids=["real", "sim"])
def test_each_square_file_drops_the_runs_before_the_first_burst(paths: list[Path]) -> None:
    discarded = []
    for path in paths:
        experiment = committed(path)
        burst = experiment.pattern.burst
        assert burst is not None
        starts = start_times(experiment.pattern, experiment.seed)
        assert experiment.warmup.runs_discarded == sum(
            start < burst.every_seconds for start in starts
        )
        discarded.append(experiment.warmup.runs_discarded)
    assert discarded == RUNS_BEFORE_THE_FIRST_BURST


def test_the_real_square_keeps_p4_5s_williams_rows() -> None:
    assert orders(REAL_SQUARE) == orders(EAGER)


def test_the_real_orders_are_a_williams_square() -> None:
    """Each autoscaler once in each position, and right after each other exactly once."""
    rows = orders(REAL_SQUARE)
    for position in range(len(REAL_AUTOSCALERS)):
        assert sorted(row[position] for row in rows) == REAL_AUTOSCALERS
    neighbours = [pair for row in rows for pair in itertools.pairwise(row)]
    assert sorted(neighbours) == sorted(itertools.permutations(REAL_AUTOSCALERS, 2))


def test_the_sim_orders_alternate_so_each_runs_first_in_half_the_files() -> None:
    rows = orders(SIM_SQUARE)
    assert all(sorted(row) == SIM_AUTOSCALERS for row in rows)
    assert [row[0] for row in rows] == ["remaining", "queue", "remaining", "queue"]


@pytest.mark.parametrize(
    "path", [*REAL_SQUARE, *SIM_SQUARE, *SCALE_DOWN.values()], ids=lambda path: path.stem
)
def test_every_variant_is_its_autoscaler_on_the_default_picker(path: Path) -> None:
    """Autoscaling does not depend on the plugin (D2); make experiment's own checks take each."""
    experiment = committed(path)
    for variant in experiment.variants:
        assert variant.autoscaler == variant.name
        assert variant.default
        assert variant.epp_config == "default-plugins.yaml"
    check_autoscaling(experiment, PROFILES)
    check_routing(experiment, PROFILES)


@pytest.mark.parametrize(
    ("name", "paths", "idle_judged"),
    [("p5-2-real", REAL_SQUARE, False), ("p5-2-sim", SIM_SQUARE, True)],
)
def test_the_series_runs_the_files_in_order_against_the_queue(
    name: str, paths: list[Path], idle_judged: bool
) -> None:
    """Idle pod-minutes are judged only where an autoscaler can ask for fewer pods (P4.5)."""
    autoscaled = load_autoscale_series(EXPERIMENTS / "series" / f"{name}.yaml")
    assert autoscaled is not None
    assert autoscaled.series.files == paths
    assert autoscaled.series.reference == REFERENCE
    assert autoscaled.idle_judged is idle_judged


@pytest.mark.parametrize("profile", list(SCALE_DOWN))
def test_the_scale_down_files_run_the_squares_autoscalers_on_burst_gap_at_seed_6(
    profile: str,
) -> None:
    experiment = committed(SCALE_DOWN[profile])
    square = committed((REAL_SQUARE if profile == "real" else SIM_SQUARE)[0])
    assert experiment.profile == profile
    assert (experiment.trace, experiment.pattern_name, experiment.seed) == (
        square.trace,
        "burst-gap",
        6,
    )
    assert experiment.kv_capacity_tokens_per_pod == square.kv_capacity_tokens_per_pod
    assert [variant.name for variant in experiment.variants] == [
        variant.name for variant in square.variants
    ]


@pytest.mark.parametrize("profile", list(SCALE_DOWN))
def test_burst_gap_leaves_time_for_a_scale_down_before_its_second_burst(profile: str) -> None:
    """Two whole bursts, then nothing: no run arrives within the HPA's scale-down window and one
    sync before the second burst, since one new run of 12 expected steps already asks remaining
    work for a second pod and so restarts the window (card 76)."""
    experiment = committed(SCALE_DOWN[profile])
    pattern, seed = experiment.pattern, experiment.seed
    assert pattern.burst is not None
    starts = start_times(pattern, seed)
    first, second = burst_starts(pattern, seed)
    assert [starts.count(first), starts.count(second)] == [GAP_BURST_RUNS, GAP_BURST_RUNS]
    assert max(starts) == second
    between = [start for start in starts if first < start < second]
    quiet = PROFILES[profile]["autoscaling"]["scale_down_stabilization_seconds"] + HPA_SYNC_SECONDS
    assert second - max(between) > quiet
    assert experiment.warmup.runs_discarded == sum(start < first for start in starts)


def test_burst_gaps_bursts_fan_out_as_the_squares_load() -> None:
    gap, heavy = committed(SCALE_DOWN["real"]).pattern, committed(REAL_SQUARE[0]).pattern
    assert gap.fan_out == heavy.fan_out
    assert gap.burst is not None and gap.burst.runs == GAP_BURST_RUNS
