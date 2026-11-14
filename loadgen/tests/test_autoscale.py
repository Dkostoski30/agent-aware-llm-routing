"""KEDA on the vLLM Deployment (P3.9b, D2): the ScaledObject a profile gives, the series a replay
leaves in Prometheus, and the moments the report reads from them.

The chart is rendered by the pinned helm, with no cluster: that KEDA acts on the object is what
make experiment NAME=autoscale-remaining proves (docs/task-cards/45-P3.9b-keda-remaining-work.md).
"""

import csv
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from load_generator.autoscale import (
    CHART,
    SERIES_QUERIES,
    AutoscaleError,
    AutoscaleRow,
    Crossings,
    check_section,
    crossing_text,
    first_crossings,
    helm_values,
    rows_from,
    scale_up_before_queue,
    write_autoscale_csv,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SECTION = {
    "min_replicas": 1,
    "max_replicas": 8,
    "remaining_work_target": 10,
    "queue_target": 1,
    "scale_down_stabilization_seconds": 300,
}


def profile(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((REPO_ROOT / "config" / f"{name}.yaml").read_text())
    return loaded


def rendered(values: dict[str, Any]) -> dict[str, Any]:
    done = subprocess.run(
        ["helm", "template", "autoscaling", str(CHART), "--values", "-"],
        input=yaml.safe_dump(values),
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    )
    documents = [document for document in yaml.safe_load_all(done.stdout) if document]
    assert len(documents) == 1
    document: dict[str, Any] = documents[0]
    return document


def test_remaining_work_scales_on_the_trackers_sum_at_the_profiles_target() -> None:
    assert helm_values(SECTION, "remaining") == {
        "minReplicas": 1,
        "maxReplicas": 8,
        "query": "sum(thesis_remaining_work)",
        "threshold": 10,
        "scaleDownStabilizationSeconds": 300,
    }


def test_queue_scales_on_the_pods_waiting_calls_at_the_queue_target() -> None:
    values = helm_values(SECTION, "queue")
    assert values["query"] == "sum(vllm:num_requests_waiting)"
    assert values["threshold"] == 1


def test_an_autoscaler_with_no_query_is_refused() -> None:
    with pytest.raises(AutoscaleError, match="cpu"):
        helm_values(SECTION, "cpu")


def test_the_chart_renders_the_sim_profiles_object_on_remaining_work() -> None:
    document = rendered(helm_values(profile("sim")["autoscaling"], "remaining"))
    assert document["apiVersion"] == "keda.sh/v1alpha1"
    assert document["kind"] == "ScaledObject"
    spec = document["spec"]
    assert spec["scaleTargetRef"] == {"name": "vllm"}
    assert (spec["minReplicaCount"], spec["maxReplicaCount"]) == (1, 8)
    behavior = spec["advanced"]["horizontalPodAutoscalerConfig"]["behavior"]
    assert behavior == {"scaleDown": {"stabilizationWindowSeconds": 300}}
    [trigger] = spec["triggers"]
    assert trigger["type"] == "prometheus"
    assert trigger["metricType"] == "AverageValue"
    assert trigger["metadata"] == {
        "serverAddress": "http://monitoring-kube-prometheus-prometheus.monitoring.svc:9090",
        "query": "sum(thesis_remaining_work)",
        # KEDA reads trigger metadata as strings.
        "threshold": "10",
    }


def test_the_chart_renders_the_queue_object_with_the_queue_target() -> None:
    document = rendered(helm_values(profile("sim")["autoscaling"], "queue"))
    [trigger] = document["spec"]["triggers"]
    assert trigger["metadata"]["query"] == "sum(vllm:num_requests_waiting)"
    assert trigger["metadata"]["threshold"] == "1"


def test_the_chart_refuses_to_render_without_a_profiles_values() -> None:
    """An object with KEDA's own defaults would scale by numbers no profile holds."""
    done = subprocess.run(
        ["helm", "template", "autoscaling", str(CHART)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode != 0
    assert "config/" in done.stderr


@pytest.mark.parametrize("name", ["real", "real-plain", "sim"])
def test_each_scaling_profile_has_a_section_that_passes_the_check(name: str) -> None:
    check_section(profile(name)["autoscaling"])


def test_real_scales_between_one_pod_and_the_two_that_fit_on_the_gpu() -> None:
    section = profile("real")["autoscaling"]
    assert (section["min_replicas"], section["max_replicas"]) == (1, profile("real")["replicas"])


def test_demo_has_no_autoscaling() -> None:
    """One pod: the 1.7B fills the GPU (D8)."""
    assert "autoscaling" not in profile("demo")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"min_replicas": 0}, "min_replicas"),
        ({"max_replicas": 1}, "max_replicas"),
        ({"remaining_work_target": 0}, "remaining_work_target"),
        ({"queue_target": -1}, "queue_target"),
        ({"scale_down_stabilization_seconds": -5}, "scale_down_stabilization_seconds"),
        ({"min_replicas": True}, "min_replicas"),
    ],
)
def test_a_section_that_could_not_scale_is_refused(changes: dict[str, Any], message: str) -> None:
    with pytest.raises(AutoscaleError, match=message):
        check_section({**SECTION, **changes})


def test_a_section_missing_a_key_is_refused() -> None:
    section = {key: value for key, value in SECTION.items() if key != "queue_target"}
    with pytest.raises(AutoscaleError, match="queue_target"):
        check_section(section)


def answer(points: dict[float, float]) -> list[dict[str, Any]]:
    """A range answer for a query with one series and no labels, as sum() and max() give."""
    return [{"metric": {}, "values": [[time, str(value)] for time, value in points.items()]}]


def test_the_rows_line_up_the_four_series_by_time_from_the_replays_start() -> None:
    answers = {
        "spec_replicas": answer({100.0: 1, 105.0: 1, 110.0: 2}),
        "ready_replicas": answer({100.0: 1, 105.0: 1, 110.0: 1}),
        "waiting": answer({100.0: 0, 105.0: 3, 110.0: 2}),
        "remaining_work": answer({105.0: 12, 110.0: 20}),
    }
    assert rows_from(answers, 100.0, 110.0) == [
        AutoscaleRow(0.0, 1, 1, 0, None),
        AutoscaleRow(5.0, 1, 1, 3, 12),
        AutoscaleRow(10.0, 2, 1, 2, 20),
    ]


def test_samples_outside_the_replay_are_left_out() -> None:
    """Before it is the warm-up, whose calls go straight to the pods and are no load to scale on."""
    answers = {name: answer({95.0: 4, 100.0: 1, 115.0: 9}) for name in SERIES_QUERIES}
    assert [row.seconds for row in rows_from(answers, 100.0, 110.0)] == [0.0]


def test_a_series_with_no_samples_is_blank_not_zero() -> None:
    """No remaining-work series is no replayer serving it (P3.8), which is not a reading of 0."""
    answers = {name: answer({100.0: 1}) for name in SERIES_QUERIES}
    answers["remaining_work"] = []
    assert rows_from(answers, 100.0, 110.0) == [AutoscaleRow(0.0, 1, 1, 1, None)]


def test_an_answer_with_two_series_is_refused() -> None:
    """Each query adds its series into one; two would be a query that does not."""
    answers = {name: answer({100.0: 1}) for name in SERIES_QUERIES}
    answers["waiting"] = answer({100.0: 1}) + answer({100.0: 2})
    with pytest.raises(AutoscaleError, match="waiting"):
        rows_from(answers, 100.0, 110.0)


def row(seconds: float, spec: float | None, waiting: float | None, remaining: float | None) -> Any:
    return AutoscaleRow(seconds, spec, spec, waiting, remaining)


def test_the_first_crossings_are_per_pod_of_the_spec_replicas() -> None:
    rows = [
        row(0, 1, 0, 5),
        row(5, 1, 0, 11),
        row(10, 1, 1, 14),
        row(15, 2, 3, 30),
        row(20, 2, 2, 25),
    ]
    assert first_crossings(rows, SECTION) == Crossings(
        remaining_seconds=5, scale_up_seconds=15, queue_seconds=15
    )


def test_at_the_target_is_not_past_it() -> None:
    rows = [row(0, 2, 2, 20), row(5, 2, 3, 21)]
    assert first_crossings(rows, {**SECTION, "min_replicas": 2}) == Crossings(5, None, 5)


def test_what_never_crosses_has_no_time() -> None:
    rows = [row(0, 1, 0, 1), row(5, 1, 1, 10)]
    assert first_crossings(rows, SECTION) == Crossings(None, None, None)


def test_a_blank_sample_crosses_nothing() -> None:
    rows = [row(0, None, 5, 50), row(5, 1, None, None), row(10, 1, 2, 11)]
    assert first_crossings(rows, SECTION) == Crossings(10, None, 10)


def test_the_scale_up_is_the_first_spec_over_the_floor() -> None:
    section = {**SECTION, "min_replicas": 2}
    rows = [row(0, 2, 0, 0), row(5, 3, 0, 0)]
    assert first_crossings(rows, section).scale_up_seconds == 5


@pytest.mark.parametrize(
    ("crossings", "before"),
    [
        (Crossings(5, 15, 20), True),
        (Crossings(5, 15, None), True),
        (Crossings(5, 15, 15), False),
        (Crossings(5, 20, 15), False),
        (Crossings(None, None, 15), False),
        (Crossings(None, None, None), False),
    ],
)
def test_the_scale_up_comes_before_the_queue_only_strictly_and_only_if_it_came(
    crossings: Crossings, before: bool
) -> None:
    assert scale_up_before_queue(crossings) is before


def test_the_text_gives_each_moment_and_the_verdict() -> None:
    text = crossing_text("remaining", SECTION, Crossings(62.0, 80.0, 125.0))
    assert text == (
        "  autoscaler remaining: remaining work passed 10 per pod at 62.0 s, spec replicas rose "
        "over 1 at 80.0 s, the queue passed 1 per pod at 125.0 s; the scale-up began before the "
        "queue passed its target"
    )


def test_the_text_says_never_and_not_before() -> None:
    text = crossing_text("remaining", SECTION, Crossings(None, None, 30.0))
    assert "remaining work passed 10 per pod never" in text
    assert "spec replicas rose over 1 never" in text
    assert text.endswith("the scale-up did not begin before the queue passed its target")


def test_the_csv_has_a_column_per_series_and_blanks_for_gaps(tmp_path: Path) -> None:
    path = tmp_path / "x-autoscale.csv"
    write_autoscale_csv(path, [AutoscaleRow(0.0, 1, 1, 0, None), AutoscaleRow(5.0, 2, 1, 3, 12)])
    with path.open() as file:
        rows = list(csv.DictReader(file))
    assert rows == [
        {
            "seconds": "0.0",
            "spec_replicas": "1",
            "ready_replicas": "1",
            "waiting": "0",
            "remaining_work": "",
        },
        {
            "seconds": "5.0",
            "spec_replicas": "2",
            "ready_replicas": "1",
            "waiting": "3",
            "remaining_work": "12",
        },
    ]


def test_a_csv_left_behind_is_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "x-autoscale.csv"
    path.write_text("kept")
    with pytest.raises(FileExistsError):
        write_autoscale_csv(path, [])
    assert path.read_text() == "kept"


def test_crossings_go_into_json_as_seconds_or_null() -> None:
    assert json.loads(json.dumps(Crossings(5.0, None, 15.0).as_meta())) == {
        "remaining_seconds": 5.0,
        "scale_up_seconds": None,
        "queue_seconds": 15.0,
    }
