"""P5.3's file (card 77, docs/task-cards/77-P5.3-failover.md): E3, a pod killed mid-replay with the
retry off and on, and a replay with no kill, over the squares' seeds on simulated pods.

Here, not in loadgen/tests, for the reason test_p5_squares.py gives: experiments/ is outside the
system hash each .meta.json records.
"""

from pathlib import Path

import yaml
from load_generator.failover import load_failover, load_failover_seeds
from load_generator.plan import burst_starts, load_replay_config

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENTS = REPO_ROOT / "experiments"
P5_3 = EXPERIMENTS / "p5-3-failover.yaml"
SIM = yaml.safe_load((REPO_ROOT / "config" / "sim.yaml").read_text())
# loadgen/tests/test_replay_plan.py's SQUARE_SEEDS (card 73).
SQUARE_SEEDS = [6, 9, 10, 13]
# The kill comes this long into the second burst, as P4.4's came 5 s into its first.
INTO_THE_BURST_SECONDS = 5
# P5's squares' load (cards 74-76), whose trace and pattern this file replays.
SQUARE_FILE = EXPERIMENTS / "p5-2-sim-a.yaml"


def test_it_replays_the_squares_load_on_simulated_pods_at_their_seeds() -> None:
    experiments = load_failover_seeds(P5_3)
    square = yaml.safe_load(SQUARE_FILE.read_text())
    for experiment in experiments:
        assert experiment.profile == "sim"
        assert experiment.trace == (EXPERIMENTS / square["trace"]).resolve()
        assert experiment.pattern == square["pattern"]
        assert experiment.routing == {"plugin": True, "affinity": True}
    assert [experiment.seed for experiment in experiments] == SQUARE_SEEDS
    assert SIM["replicas"] == 2


def test_the_kill_comes_into_the_second_burst_at_every_seed() -> None:
    patterns = load_replay_config(SIM["replay"]).patterns
    for experiment in load_failover_seeds(P5_3):
        second = sorted(burst_starts(patterns[experiment.pattern], experiment.seed))[1]
        assert experiment.kill_after_seconds == second + INTO_THE_BURST_SECONDS


def test_it_keeps_p4_4s_wait_and_settle() -> None:
    p4_4 = load_failover(EXPERIMENTS / "failover.yaml")
    for experiment in load_failover_seeds(P5_3):
        assert experiment.wait_for_calls_seconds == p4_4.wait_for_calls_seconds
        assert experiment.settle_seconds == p4_4.settle_seconds


def test_each_variant_runs_once_in_each_place_over_the_first_three_seeds() -> None:
    orders = [[v.name for v in experiment.variants] for experiment in load_failover_seeds(P5_3)]
    assert {(v.name, v.retry, v.kill) for v in load_failover_seeds(P5_3)[0].variants} == {
        ("retry-off", False, True),
        ("retry-on", True, True),
        ("no-kill", True, False),
    }
    for place in range(3):
        assert sorted(order[place] for order in orders[:3]) == ["no-kill", "retry-off", "retry-on"]


def test_retry_on_runs_before_no_kill_at_two_seeds_and_after_it_at_two() -> None:
    """retry-on's runs are paired with no-kill's for the recovery cost, and the host's speed drifts
    through a sitting (P5.1a)."""
    orders = [[v.name for v in experiment.variants] for experiment in load_failover_seeds(P5_3)]
    before = [order.index("retry-on") < order.index("no-kill") for order in orders]
    assert sorted(before) == [False, False, True, True]
