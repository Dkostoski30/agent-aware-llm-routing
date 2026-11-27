"""P5.3b's file (card 79, docs/task-cards/79-P5.3b-failover-cut.md): P5.3's kill again, with the
pod's containers stopped at once and the retry judged on the calls the kill cut.

Here, not in loadgen/tests, for the reason test_p5_3.py gives.
"""

import dataclasses
from pathlib import Path

import yaml
from load_generator.failover import load_failover_seeds

EXPERIMENTS = Path(__file__).resolve().parents[2] / "experiments"
P5_3 = EXPERIMENTS / "p5-3-failover.yaml"
P5_3B = EXPERIMENTS / "p5-3b-failover.yaml"
# What a file's experiments differ in by its name and text alone.
NAMING = ("name", "hash", "stem")


def test_it_is_p5_3s_file_with_the_container_stopped_and_a_cut_required() -> None:
    p5_3, p5_3b = load_failover_seeds(P5_3), load_failover_seeds(P5_3B)
    assert len(p5_3) == len(p5_3b)
    for before, after in zip(p5_3, p5_3b, strict=True):
        assert (before.stop_container, before.require_cut) == (False, False)
        assert (after.stop_container, after.require_cut) == (True, True)
        changed = {field: getattr(after, field) for field in NAMING}
        changed.update(stop_container=False, require_cut=False)
        assert dataclasses.replace(after, **changed) == dataclasses.replace(
            before, **{field: getattr(after, field) for field in NAMING}
        )


def test_its_keys_are_p5_3s_and_the_two_new_ones() -> None:
    keys = set(yaml.safe_load(P5_3B.read_text()))
    assert keys - set(yaml.safe_load(P5_3.read_text())) == {"stop_container", "require_cut"}
