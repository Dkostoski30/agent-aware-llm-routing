"""P5.1's real squares' files (cards 74 and 75; docs/task-cards/74-P5.1b-headline-square.md and
75-P5.1c-ablation-square.md).

Here, not in loadgen/tests: each file's .meta.json hashes every file under experiment.SYSTEM_PATHS,
loadgen/ included, and make square-compare refuses files of different hashes. A test of these
files written between a square's first file and its last would refuse the square (the user,
2026-11-24, card 74). experiments/ is outside the hash, and make test runs this folder too.
"""

import itertools
from pathlib import Path

import pytest
import yaml
from load_generator.experiment import (
    ONE_AT_A_TIME,
    Experiment,
    Variant,
    check_routing,
    load_experiment,
)
from load_generator.plan import load_replay_config, start_times
from load_generator.square_compare import load_series

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENTS = REPO_ROOT / "experiments"
REAL = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())

# P5.1b's headline square (card 74): files a-d, one seed each.
HEADLINE = [EXPERIMENTS / f"p5-1b-{name}.yaml" for name in ("a", "b", "c", "d")]
HEADLINE_SERIES = EXPERIMENTS / "series" / "p5-1b.yaml"
# loadgen/tests/test_replay_plan.py's SQUARE_SEEDS (card 73), in the files' order.
SQUARE_SEEDS = [6, 9, 10, 13]
# The runs that start before the first burst at 120 s at each seed, counted in card 73.
RUNS_BEFORE_THE_FIRST_BURST = [4, 5, 3, 4]
# A Williams square with A plugin, B default, C round-robin, D session-affinity (the user,
# 2026-11-24, card 74): the plugin runs first in file a, so a fault of the priority rule, which no
# replay had run before, shows in the night's first replay.
HEADLINE_ORDERS = [
    ["plugin", "default", "session-affinity", "round-robin"],
    ["default", "round-robin", "plugin", "session-affinity"],
    ["round-robin", "session-affinity", "default", "plugin"],
    ["session-affinity", "plugin", "round-robin", "default"],
]
# P5.1c's ablation square (card 75): the plugin, and the plugin with one rule off each. Card 74's
# rows with A plugin, B no-overload-guard, C no-parallel-spill, D no-priority (the user,
# 2026-11-25, card 75), held to the plugin.
ABLATION = [EXPERIMENTS / f"p5-1c-{name}.yaml" for name in ("a", "b", "c", "d")]
ABLATION_SERIES = EXPERIMENTS / "series" / "p5-1c.yaml"
ABLATION_ORDERS = [
    ["plugin", "no-overload-guard", "no-priority", "no-parallel-spill"],
    ["no-overload-guard", "no-parallel-spill", "plugin", "no-priority"],
    ["no-parallel-spill", "no-priority", "no-overload-guard", "plugin"],
    ["no-priority", "plugin", "no-parallel-spill", "no-overload-guard"],
]
# Each ablated variant and the one rule it turns off.
ABLATED = {
    "no-overload-guard": "overload_guard",
    "no-parallel-spill": "parallel_spill",
    "no-priority": "priority",
}
# Both squares: their files, the variant each holds the others to, and their decided orders.
SQUARES = {
    "headline": (HEADLINE, "default", HEADLINE_ORDERS),
    "ablation": (ABLATION, "plugin", ABLATION_ORDERS),
}
SQUARE_NAMES = list(SQUARES)
# P5.1a-2's check of the squares' load (card 72), which the files repeat but for seed and variants.
HEAVY = EXPERIMENTS / "p5-1a-heavy.yaml"
# The round-robin and session-affinity variants as the files that first ran them.
BASELINE = EXPERIMENTS / "baseline.yaml"
SPIKE = EXPERIMENTS / "p5-1a-spike.yaml"


def committed(path: Path) -> Experiment:
    return load_experiment(path, load_replay_config(REAL["replay"]).patterns)


def picker(variant: Variant) -> tuple[object, ...]:
    """What makes two variants the same picker, whatever their pods' count and start."""
    return (variant.values, variant.epp_config, variant.profile, variant.routing)


def orders(paths: list[Path]) -> list[list[str]]:
    return [[variant.name for variant in committed(path).variants] for path in paths]


@pytest.mark.parametrize("square", SQUARE_NAMES)
def test_each_file_runs_the_heavy_checks_load_at_its_own_seed(square: str) -> None:
    paths, _, _ = SQUARES[square]
    heavy = committed(HEAVY)
    files = [committed(path) for path in paths]
    for experiment in files:
        assert experiment.profile == "real"
        assert (experiment.trace, experiment.pattern_name) == (heavy.trace, heavy.pattern_name)
        assert experiment.kv_capacity_tokens_per_pod == heavy.kv_capacity_tokens_per_pod
        assert experiment.pod_stall_seconds == heavy.pod_stall_seconds == 30
        warmup, checked = experiment.warmup, heavy.warmup
        assert (warmup.calls_per_pod, warmup.words, warmup.completion_tokens) == (
            checked.calls_per_pod,
            checked.words,
            checked.completion_tokens,
        )
    assert [experiment.seed for experiment in files] == SQUARE_SEEDS


@pytest.mark.parametrize("square", SQUARE_NAMES)
def test_each_file_drops_the_runs_before_the_first_burst(square: str) -> None:
    paths, _, _ = SQUARES[square]
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


@pytest.mark.parametrize("square", SQUARE_NAMES)
def test_the_files_run_the_decided_orders(square: str) -> None:
    paths, _, decided = SQUARES[square]
    assert orders(paths) == decided


@pytest.mark.parametrize("square", SQUARE_NAMES)
def test_the_orders_are_a_williams_square(square: str) -> None:
    """Each variant once in each position, and each variant right after each other exactly once,
    so neither a drift over a sitting nor what ran just before lands on one variant."""
    paths, _, _ = SQUARES[square]
    rows = orders(paths)
    variants = sorted(rows[0])
    for position in range(len(variants)):
        assert sorted(row[position] for row in rows) == variants
    neighbours = [pair for row in rows for pair in itertools.pairwise(row)]
    assert sorted(neighbours) == sorted(itertools.permutations(variants, 2))


@pytest.mark.parametrize("square", SQUARE_NAMES)
def test_each_variant_runs_before_the_reference_in_half_the_files(square: str) -> None:
    """PLAN.md's real-pod comparisons: each one in both orders, here twice each."""
    paths, reference, _ = SQUARES[square]
    rows = orders(paths)
    for variant in rows[0]:
        if variant != reference:
            before = sum(row.index(variant) < row.index(reference) for row in rows)
            assert before == len(rows) // 2


@pytest.mark.parametrize("square", SQUARE_NAMES)
def test_the_variants_are_the_same_in_every_file_and_start_one_pod_at_a_time(square: str) -> None:
    """Two compiled pods started together froze in CUDA graph capture for over 15 min in file b's
    first run (2026-11-25), so every variant starts its second pod beside a Ready first (P4.13c),
    which needs the count set (the user, 2026-11-25, cards 74 and 75)."""
    paths, _, _ = SQUARES[square]
    by_name = [{variant.name: variant for variant in committed(path).variants} for path in paths]
    assert all(variants == by_name[0] for variants in by_name)
    assert all(variant.start == ONE_AT_A_TIME for variant in by_name[0].values())
    assert all(variant.replicas == REAL["replicas"] for variant in by_name[0].values())


def test_the_headline_variants_are_the_four_decided() -> None:
    """E1's headline (card 72's decisions 4 and 5): the default picker; round-robin as P2.5 ran it;
    upstream's session-affinity-filter as P5.1a ran it; the plugin with affinity, the overload
    guard, parallel spill and priority, warm-up left to E2."""
    variants = {variant.name: variant for variant in committed(HEADLINE[0]).variants}
    assert variants["default"].default
    assert variants["default"].epp_config == "default-plugins.yaml"
    baseline = {variant.name: variant for variant in committed(BASELINE).variants}
    spike = {variant.name: variant for variant in committed(SPIKE).variants}
    assert picker(variants["round-robin"]) == picker(baseline["round-robin"])
    assert picker(variants["session-affinity"]) == picker(spike["session-affinity"])
    plugin = variants["plugin"]
    assert plugin.values is None
    assert plugin.epp_config == "thesis-plugins.yaml"
    assert plugin.routing == {
        "plugin": True,
        "affinity": True,
        "overload_guard": True,
        "parallel_spill": True,
        "priority": True,
    }


def test_the_ablations_plugin_is_the_headlines() -> None:
    """The same variant in both squares at the same seeds, so the two sittings' plugins can be read
    side by side as the host's own spread."""
    headline = {variant.name: variant for variant in committed(HEADLINE[0]).variants}
    ablation = {variant.name: variant for variant in committed(ABLATION[0]).variants}
    assert ablation["plugin"] == headline["plugin"]


def test_each_ablated_variant_is_the_plugin_with_exactly_one_rule_off() -> None:
    variants = {variant.name: variant for variant in committed(ABLATION[0]).variants}
    plugin = variants["plugin"]
    assert plugin.routing is not None
    assert sorted(variants) == sorted(["plugin", *ABLATED])
    for name, rule in ABLATED.items():
        variant = variants[name]
        assert (variant.values, variant.epp_config) == (plugin.values, plugin.epp_config)
        assert variant.routing == {**plugin.routing, rule: False}


@pytest.mark.parametrize("square", SQUARE_NAMES)
def test_the_real_profile_takes_every_variant(square: str) -> None:
    """make experiment refuses the priority rule on a profile without vLLM's priority scheduling
    (P4.1), and the guard or the spill without affinity: real has priority, and affinity stays
    on in every variant."""
    paths, _, _ = SQUARES[square]
    for path in paths:
        check_routing(committed(path), {"real": REAL})


@pytest.mark.parametrize("square", SQUARE_NAMES)
def test_the_series_runs_the_files_in_order_against_its_reference(square: str) -> None:
    paths, reference, _ = SQUARES[square]
    series = load_series({"headline": HEADLINE_SERIES, "ablation": ABLATION_SERIES}[square])
    assert series.files == paths
    assert series.reference == reference
