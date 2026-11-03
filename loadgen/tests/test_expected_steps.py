"""expected_steps per task type (P2.4, D1): the rule on known runs and what the profiles hold."""

from pathlib import Path

import pytest
import yaml
from load_generator.expected_steps import expected_steps
from load_generator.plan import load_trace

REPO_ROOT = Path(__file__).resolve().parents[2]
TRACES = REPO_ROOT / "experiments" / "results" / "traces"
RECORDED_TRACE = TRACES / "record-demo-e1044feb534c-d86ed436c5b2-5554fcf81b09.jsonl"


@pytest.mark.parametrize(
    ("steps", "wanted"),
    [
        ([4], 4),
        ([2, 9, 3], 3),
        ([9, 2, 3], 3),
        # An even count's median falls between two runs. Rounding up: a remaining-work figure that
        # is too low starts the scale-up late, and x-expected-steps must be a whole number.
        ([2, 3], 3),
        ([2, 2, 3, 3], 3),
        ([2, 2, 2, 4], 2),
    ],
)
def test_it_is_the_median_of_a_types_runs_rounded_up(steps: list[int], wanted: int) -> None:
    assert expected_steps(("csv", step) for step in steps) == {"csv": wanted}


def test_each_type_has_its_own_median() -> None:
    runs = [("csv", 2), ("support", 7), ("csv", 4), ("support", 9), ("support", 8)]
    assert expected_steps(runs) == {"csv": 3, "support": 8}


def test_no_runs_give_no_types() -> None:
    assert expected_steps([]) == {}


def test_the_recorded_demo_runs_give_these_values() -> None:
    """The P2.2b trace by hand: csv 2,2,2,2,2,2,2,2,3,3; contracts and support 2,2,2,2,3,3,3,3;
    arithmetic all 2."""
    runs = [(run.task_type, len(run.calls)) for run in load_trace(RECORDED_TRACE)]
    assert expected_steps(runs) == {"csv": 2, "contracts": 3, "support": 3, "arithmetic": 2}


@pytest.mark.parametrize("profile", ["real", "sim", "demo"])
def test_the_profiles_hold_the_medians_of_the_committed_traces(profile: str) -> None:
    """make convert prints these values and they are pasted, since rewriting a profile with PyYAML
    would lose its comments. So a trace added without its values fails here (D1, D11)."""
    runs = [
        (run.task_type, len(run.calls))
        for trace in sorted(TRACES.glob("*.jsonl"))
        for run in load_trace(trace)
    ]
    section = yaml.safe_load((REPO_ROOT / "config" / f"{profile}.yaml").read_text())["agent"]
    assert section["expected_steps_by_type"] == expected_steps(runs)
