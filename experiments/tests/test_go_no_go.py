"""Scoring of the P1.6b go/no-go: numbers in an answer, the expected values, a run's verdict and the
decision between the two D10 modes."""

from pathlib import Path
from typing import Any

import pytest
from go_no_go import (
    Question,
    decide,
    expected_value,
    is_correct,
    load_experiment,
    numbers_in,
    ready_pod_ip,
    result_path,
    served_mode,
)

EXPERIMENT = Path(__file__).resolve().parents[1] / "demo-go-no-go.yaml"

# Small enough to check by hand: North revenue 400, South 210, East 40; Widget units 20, Gadget 3.
ROWS = [
    {"region": "North", "product": "Widget", "units": "10", "revenue": "100"},
    {"region": "North", "product": "Gadget", "units": "2", "revenue": "300"},
    {"region": "South", "product": "Widget", "units": "6", "revenue": "60"},
    {"region": "South", "product": "Gadget", "units": "1", "revenue": "150"},
    {"region": "East", "product": "Widget", "units": "4", "revenue": "40"},
]

TOLERANCE = 0.01


# A tool call as the run's steps show it: name, arguments, result (None for final_answer).
Call = tuple[str, dict[str, Any], dict[str, Any] | None]


def run(answer: str | None, calls: list[Call], status: str = "done") -> dict[str, Any]:
    """A GET /runs/{id} body: one step per tool call."""
    return {
        "status": status,
        "answer": answer,
        "invalid_calls": 0,
        "steps": [
            {
                "step": i + 1,
                "tool_calls": [{"name": name, "arguments": arguments, "result": result}],
            }
            for i, (name, arguments, result) in enumerate(calls)
        ],
    }


def question(expected_calls: list[dict[str, Any]], wrong_if_matches: str | None = None) -> Question:
    return Question(
        id="q",
        text="",
        expected_calls=expected_calls,
        expected={},
        wrong_if_matches=wrong_if_matches,
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("The total revenue is 14351.1.", [14351.1]),
        # A thousands comma is part of the number, not a separator between two numbers.
        ("The total revenue is 14,351.10.", [14351.1]),
        ("1,234,567 units", [1234567.0]),
        # A comma followed by a space separates two numbers.
        ("North has 400, South has 210.", [400.0, 210.0]),
        ("Gadget sold 17.83 units per row on average, up 12% from 15.9.", [17.83, 12.0, 15.9]),
        # Unsigned: every expected value of the experiment is positive.
        ("The difference is -3,728.90.", [3728.9]),
        ("No number here.", []),
    ],
)
def test_numbers_in(text: str, expected: list[float]) -> None:
    assert numbers_in(text) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ({"kind": "aggregate", "agg": "mean", "value": "revenue"}, 130.0),
        (
            {
                "kind": "aggregate",
                "agg": "sum",
                "value": "revenue",
                "group": {"by": "region", "equals": "North"},
            },
            400.0,
        ),
        (
            {
                "kind": "aggregate",
                "agg": "mean",
                "value": "units",
                "group": {"by": "product", "equals": "Widget"},
            },
            20 / 3,
        ),
        # right minus left.
        (
            {
                "kind": "difference",
                "agg": "sum",
                "value": "revenue",
                "by": "region",
                "left": "South",
                "right": "North",
            },
            190.0,
        ),
        # Widget sold the most units (20), Gadget made the most revenue (450): the answer is
        # Widget's revenue.
        (
            {
                "kind": "of_top_group",
                "by": "product",
                "rank": {"agg": "sum", "value": "units"},
                "agg": "sum",
                "value": "revenue",
            },
            200.0,
        ),
    ],
)
def test_expected_value(spec: dict[str, Any], expected: float) -> None:
    assert expected_value(ROWS, spec) == pytest.approx(expected)


def test_expected_value_rejects_a_tied_top_group() -> None:
    rows = [{"g": "a", "x": "1"}, {"g": "b", "x": "1"}]
    spec = {
        "kind": "of_top_group",
        "by": "g",
        "rank": {"agg": "sum", "value": "x"},
        "agg": "sum",
        "value": "x",
    }
    with pytest.raises(ValueError, match="tie"):
        expected_value(rows, spec)


def test_every_question_of_the_experiment_has_a_positive_expected_value() -> None:
    """The scorer reads numbers without their sign, so it relies on this."""
    experiment = load_experiment(EXPERIMENT)
    for question in experiment.questions:
        assert expected_value(experiment.rows, question.expected) > 0, question.id


UNITS_BY_PRODUCT = {"file": "sales", "by": "product", "value": "units", "agg": "sum"}
GROUP_BY_OK: Call = ("group_by", UNITS_BY_PRODUCT, {"rows": []})
ANY_GROUP_BY = question([{"tool": "group_by"}])


@pytest.mark.parametrize(
    ("body", "correct"),
    [
        (run("The total revenue of North is 1,234.5.", [GROUP_BY_OK]), True),
        # 0.45% off: a rounded answer.
        (run("About 1,240.", [GROUP_BY_OK]), True),
        # 1.26% off.
        (run("About 1,250.", [GROUP_BY_OK]), False),
        # Right number among others.
        (run("North 1,234.5, South 980.", [GROUP_BY_OK]), True),
        (run("I could not compute it.", [GROUP_BY_OK]), False),
        # The expected tool was never called.
        (run("1,234.5", [("csv_summary", {"file": "sales"}, {"rows": []})]), False),
        # Called, but it never returned a result: a rejected call or a tool error.
        (run("1,234.5", [("group_by", UNITS_BY_PRODUCT, {"error": "bad"})]), False),
        (run("1,234.5", [("group_by", UNITS_BY_PRODUCT, None)]), False),
        # A failed call followed by one that ran.
        (run("1,234.5", [("group_by", UNITS_BY_PRODUCT, {"error": "bad"}), GROUP_BY_OK]), True),
        # A failed run is wrong, whatever its answer says.
        (run("1,234.5", [GROUP_BY_OK], status="failed"), False),
        (run(None, [GROUP_BY_OK], status="failed"), False),
    ],
)
def test_is_correct(body: dict[str, Any], correct: bool) -> None:
    assert is_correct(body, ANY_GROUP_BY, 1234.5, TOLERANCE) is correct


@pytest.mark.parametrize(
    ("calls", "correct"),
    [
        ([GROUP_BY_OK], True),
        # The arguments the question names must match; the others (file) may be anything.
        ([("group_by", {**UNITS_BY_PRODUCT, "file": "other"}, {"rows": []})], True),
        # The P1.6b two-step runs: units counted, not summed, so every product tied at 12 and the
        # product in the answer was a guess.
        ([("group_by", {**UNITS_BY_PRODUCT, "agg": "count"}, {"rows": []})], False),
        ([("group_by", {**UNITS_BY_PRODUCT, "by": "region"}, {"rows": []})], False),
        # The right call, but it hit a tool error.
        ([("group_by", UNITS_BY_PRODUCT, {"error": "bad"})], False),
        # A wrong call does not undo a right one.
        ([("group_by", {**UNITS_BY_PRODUCT, "agg": "count"}, {"rows": []}), GROUP_BY_OK], True),
    ],
)
def test_is_correct_checks_the_arguments_a_question_names(calls: list[Call], correct: bool) -> None:
    by_units = question(
        [{"tool": "group_by", "arguments": {"by": "product", "value": "units", "agg": "sum"}}]
    )
    assert is_correct(run("1,234.5", calls), by_units, 1234.5, TOLERANCE) is correct


@pytest.mark.parametrize(
    ("answer", "correct"),
    [
        # The four compare answers of the first P1.6b run (2026-10-28); North made more.
        ("The North region made $3728.90 less revenue than the South region.", False),
        ("The North region made $3,728.90 less revenue than the South region.", False),
        ("The North region made $3728.90 less than the South region in revenue.", False),
        ("The total revenue for the North region is $3728.90 more than the South region.", True),
        # The compare summary sentence, copied by the model in P1.6c: "more" after South's value
        # is the percentage of North's lead, not a claim about South.
        (
            "North's sum of revenue (14351.1) is 3728.9 higher than South's (10622.2), 35.1% more.",
            True,
        ),
        ("North's revenue is 3,728.90 lower than South's.", False),
        # The same fact from the other side, and the opposite claim from the other side.
        ("South made 3,728.90 less than North.", True),
        ("South made 3,728.90 more than North.", False),
        # A bare amount answers "how much more" without a direction to get wrong.
        ("The difference is 3,728.90.", True),
    ],
)
def test_the_compare_question_rejects_the_wrong_direction(answer: str, correct: bool) -> None:
    compare = next(q for q in load_experiment(EXPERIMENT).questions if q.id == "compare")
    calls: list[Call] = [("compare", {}, {"difference": 3728.9})]
    assert is_correct(run(answer, calls), compare, 3728.9, TOLERANCE) is correct


@pytest.mark.parametrize(
    ("body", "mode"),
    [
        # Only required mode offers final_answer (D10).
        (run("1,234.5", [GROUP_BY_OK, ("final_answer", {"answer": "1,234.5"}, None)]), "required"),
        (run("1,234.5", [GROUP_BY_OK]), "auto"),
        # A failed run may have stopped before final_answer in either mode.
        (run(None, [GROUP_BY_OK], status="failed"), None),
        (run(None, [GROUP_BY_OK], status="timeout"), None),
    ],
)
def test_served_mode(body: dict[str, Any], mode: str | None) -> None:
    """The first P1.6b rerun sent its first auto run to the old, still terminating required pod."""
    assert served_mode(body) == mode


@pytest.mark.parametrize(
    ("correct", "decision"),
    [
        ({"required": 8, "auto": 9}, "auto"),
        ({"required": 10, "auto": 9}, "required"),
        ({"required": 9, "auto": 10}, "auto"),
        # A tie goes to required: it cannot produce a malformed call (D10).
        ({"required": 9, "auto": 9}, "required"),
        ({"required": 10, "auto": 10}, "required"),
        ({"required": 8, "auto": 8}, None),
        ({"required": 0, "auto": 5}, None),
    ],
)
def test_decide(correct: dict[str, int], decision: str | None) -> None:
    assert decide(correct, threshold=9) == decision


def test_result_path_carries_the_experiment_hash_and_the_agent_image_tag() -> None:
    """A result is tied to the experiment file and to the agent code that produced it (D11)."""
    experiment = load_experiment(EXPERIMENT)
    path = result_path(experiment, "thesis/agent:c61bb199c383")
    assert path == EXPERIMENT.parent / "results" / (
        f"demo-go-no-go-{experiment.hash}-c61bb199c383.csv"
    )


def pod(ip: str, ready: str = "True", deleting: bool = False) -> dict[str, Any]:
    """A pod as kubectl get pods -o json lists it: the conditions carry their timestamps too, as
    real ones do (a first version compared whole condition dicts and never found a Ready pod)."""
    metadata: dict[str, Any] = {"name": f"agent-{ip}"}
    if deleting:
        metadata["deletionTimestamp"] = "2026-10-28T01:16:32Z"
    conditions = [
        {
            "type": kind,
            "status": status,
            "lastProbeTime": None,
            "lastTransitionTime": "2026-10-28T01:16:31Z",
        }
        for kind, status in [("Initialized", "True"), ("Ready", ready), ("ContainersReady", ready)]
    ]
    return {"metadata": metadata, "status": {"podIP": ip, "conditions": conditions}}


@pytest.mark.parametrize(
    ("pods", "ip"),
    [
        ([pod("10.42.0.33")], "10.42.0.33"),
        # The old pod of the last mode, still terminating.
        ([pod("10.42.0.32", deleting=True), pod("10.42.0.33")], "10.42.0.33"),
    ],
)
def test_ready_pod_ip(pods: list[dict[str, Any]], ip: str) -> None:
    assert ready_pod_ip({"items": pods}) == ip


@pytest.mark.parametrize(
    "pods",
    [
        [],
        [pod("10.42.0.33", ready="False")],
        [pod("10.42.0.32", deleting=True)],
        # The Deployment has one replica; two Ready pods mean a rollout is still going.
        [pod("10.42.0.32"), pod("10.42.0.33")],
    ],
    ids=["none", "not-ready", "only-terminating", "two-ready"],
)
def test_ready_pod_ip_needs_exactly_one_ready_pod(pods: list[dict[str, Any]]) -> None:
    with pytest.raises(RuntimeError, match="Ready agent pod"):
        ready_pod_ip({"items": pods})
