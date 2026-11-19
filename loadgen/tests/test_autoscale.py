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
    AutoscaleSummary,
    CallSpan,
    CpuRow,
    Crossings,
    check_cpu_target,
    check_section,
    cpu_cores,
    cpu_quiet,
    cpu_rows_from,
    crossing_text,
    first_crossings,
    first_scale_down,
    go_seconds,
    helm_values,
    idle_pod_minutes,
    interrupted_runs,
    rows_from,
    scale_up_before_queue,
    scaled_before_replay,
    summarise_autoscale,
    summary_text,
    time_to_capacity,
    wall_spans,
    write_autoscale_csv,
    write_cpu_csv,
    write_summary_csv,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
VLLM_CHART = REPO_ROOT / "infra" / "charts" / "vllm"
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
    """P3.10 made cpu a kind, so an unknown name is now memory (spec change, experiments/log.md)."""
    with pytest.raises(AutoscaleError, match="memory"):
        helm_values(SECTION, "memory")


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
    ("rows", "scaled"),
    [
        ([row(0, 2, 0, 0), row(5, 2, 0, 0)], True),
        ([row(0, None, 0, 0), row(5, 2, 0, 0)], True),
        ([row(0, 1, 0, 0), row(5, 2, 0, 0)], False),
        ([row(0, None, 0, 0)], False),
        ([], False),
    ],
    ids=["first sample over", "first count over", "at the floor first", "no count", "no sample"],
)
def test_a_series_whose_first_count_is_over_the_floor_was_scaled_before_the_replay(
    rows: list[AutoscaleRow], scaled: bool
) -> None:
    """P3.10's review: the warm-up's calls go straight to the pods, so an autoscaler installed
    before them could scale on them, out of the series' sight."""
    assert scaled_before_replay(rows, SECTION) is scaled


def test_a_scale_up_before_the_replay_has_no_time_and_says_so() -> None:
    rows = [row(0, 2, 0, 0), row(5, 2, 3, 30)]
    assert first_crossings(rows, SECTION) == Crossings(
        remaining_seconds=5, scale_up_seconds=None, queue_seconds=5, scaled_before_replay=True
    )


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


def test_a_scale_up_before_the_replay_has_no_verdict_against_the_queue() -> None:
    assert scale_up_before_queue(Crossings(None, None, 15, scaled_before_replay=True)) is None


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


def test_the_text_says_when_the_pods_were_scaled_before_the_replay() -> None:
    text = crossing_text("cpu", SECTION, Crossings(None, None, 125.0, scaled_before_replay=True))
    assert text == (
        "  autoscaler cpu: remaining work passed 10 per pod never, spec replicas were over 1 from "
        "the replay's start, the queue passed 1 per pod at 125.0 s; the scale-up came before the "
        "replay, so it is not timed against the queue"
    )


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
        "scaled_before_replay": False,
    }


def test_crossings_from_a_meta_written_before_the_flag_were_not_scaled_before() -> None:
    """P3.9b's .meta.json files have no scaled_before_replay; their series started at the floor."""
    meta: dict[str, Any] = {
        "remaining_seconds": 125.0,
        "scale_up_seconds": 140.0,
        "queue_seconds": 130.0,
    }
    assert Crossings(**meta).scaled_before_replay is False


# P3.10: a plain HPA on CPU, the baseline the approved topic names, on real pods only (D5).
CPU_SECTION = {**SECTION, "max_replicas": 2, "cpu_target_percent": 80}


def test_cpu_scales_on_the_pods_cpu_at_the_profiles_target_percent() -> None:
    assert helm_values(CPU_SECTION, "cpu") == {
        "minReplicas": 1,
        "maxReplicas": 2,
        "cpuTargetPercent": 80,
        "scaleDownStabilizationSeconds": 300,
    }


def test_the_chart_renders_a_plain_hpa_on_the_real_profiles_cpu_target() -> None:
    """One object, not KEDA's: rendered() holds the count to one."""
    section = profile("real")["autoscaling"]
    document = rendered(helm_values(section, "cpu"))
    assert document["apiVersion"] == "autoscaling/v2"
    assert document["kind"] == "HorizontalPodAutoscaler"
    assert document["metadata"]["name"] == "vllm"
    spec = document["spec"]
    assert spec["scaleTargetRef"] == {"apiVersion": "apps/v1", "kind": "Deployment", "name": "vllm"}
    assert (spec["minReplicas"], spec["maxReplicas"]) == (1, 2)
    assert spec["behavior"] == {
        "scaleDown": {"stabilizationWindowSeconds": section["scale_down_stabilization_seconds"]}
    }
    assert spec["metrics"] == [
        {
            "type": "Resource",
            "resource": {
                "name": "cpu",
                "target": {
                    "type": "Utilization",
                    "averageUtilization": section["cpu_target_percent"],
                },
            },
        }
    ]


@pytest.mark.parametrize("name", ["real", "real-plain"])
def test_each_real_profile_has_a_cpu_target_that_passes_the_check(name: str) -> None:
    check_cpu_target(profile(name)["autoscaling"])


@pytest.mark.parametrize("value", [0, -80, 80.5, True, "80"])
def test_a_cpu_target_that_is_not_a_whole_percent_over_0_is_refused(value: object) -> None:
    with pytest.raises(AutoscaleError, match="cpu_target_percent"):
        check_cpu_target({**SECTION, "cpu_target_percent": value})


def test_a_section_with_no_cpu_target_is_refused_for_cpu_only() -> None:
    """The simulator's section has none: KEDA's two kinds never read it."""
    with pytest.raises(AutoscaleError, match="cpu_target_percent"):
        check_cpu_target(SECTION)
    check_section(SECTION)


def test_the_cpu_request_is_one_measurement_in_every_real_and_demo_profile() -> None:
    """Measured once on the 0.6B (P3.10); demo's is bookkeeping, it never autoscales."""
    requests = {
        name: profile(name)["pod"]["cpu_request"] for name in ("real", "real-plain", "demo")
    }
    assert len(set(requests.values())) == 1


def vllm_deployment(values: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", "vllm", str(VLLM_CHART), "--values", "-"],
        input=yaml.safe_dump(values),
        capture_output=True,
        text=True,
        check=False,
    )


def real_pod_values(gpu_access: str) -> dict[str, Any]:
    """What make vllm-deploy hands the chart from config/real.yaml, with host paths of its own."""
    real = profile("real")
    return {
        **{key: real[key] for key in ("replicas", "model", "pod", "monitoring", "drain")},
        "gpuAccess": gpu_access,
        "hfCacheHostPath": "/hf",
        "loraHostPath": "/lora",
        "drainImage": "thesis/picker:0123456789ab",
    }


@pytest.mark.parametrize("gpu_access", ["plugin", "runtime"])
def test_the_vllm_pod_requests_the_profiles_cpu_however_it_reaches_the_gpu(
    gpu_access: str,
) -> None:
    """An HPA's CPU target is a share of this request."""
    done = vllm_deployment(real_pod_values(gpu_access))
    assert done.returncode == 0, done.stderr
    [deployment] = [
        document
        for document in yaml.safe_load_all(done.stdout)
        if document and document["kind"] == "Deployment"
    ]
    [container] = deployment["spec"]["template"]["spec"]["containers"]
    resources = container["resources"]
    assert resources["requests"] == {"cpu": profile("real")["pod"]["cpu_request"]}
    gpu = {"nvidia.com/gpu": 1} if gpu_access == "plugin" else None
    assert resources.get("limits") == gpu


def test_the_vllm_chart_refuses_a_profile_with_no_cpu_request() -> None:
    values = real_pod_values("plugin")
    values["pod"] = {key: value for key, value in values["pod"].items() if key != "cpu_request"}
    done = vllm_deployment(values)
    assert done.returncode != 0
    assert "pod.cpu_request" in done.stderr


def test_the_cpu_rows_are_the_hpas_reading_from_the_replays_start() -> None:
    result = answer({95.0: 10, 100.0: 12, 105.0: 97.5, 115.0: 5})
    assert cpu_rows_from(result, 100.0, 110.0) == [CpuRow(0.0, 12), CpuRow(5.0, 97.5)]


def test_no_reading_from_the_hpa_is_no_rows() -> None:
    """A KEDA variant has no plain HPA, and neither has a cpu variant whose HPA never read."""
    assert cpu_rows_from([], 100.0, 110.0) == []


def test_a_cpu_answer_with_two_series_is_refused() -> None:
    with pytest.raises(AutoscaleError, match="cpu"):
        cpu_rows_from(answer({100.0: 1}) + answer({100.0: 2}), 100.0, 110.0)


def test_the_cpu_csv_has_the_seconds_and_the_percent(tmp_path: Path) -> None:
    path = tmp_path / "x-cpu-hpa.csv"
    write_cpu_csv(path, [CpuRow(0.0, 12), CpuRow(5.0, 97.5)])
    with path.open() as file:
        assert list(csv.DictReader(file)) == [
            {"seconds": "0.0", "cpu_utilization_percent": "12"},
            {"seconds": "5.0", "cpu_utilization_percent": "97.5"},
        ]
    with pytest.raises(FileExistsError):
        write_cpu_csv(path, [])


@pytest.mark.parametrize(
    ("quantity", "cores"),
    [("155102366n", 0.155102366), ("250u", 0.00025), ("1174m", 1.174), ("2", 2.0)],
)
def test_a_cpu_quantity_is_read_in_cores(quantity: str, cores: float) -> None:
    assert cpu_cores(quantity) == pytest.approx(cores)


@pytest.mark.parametrize(
    ("duration", "seconds"),
    [("32.247s", 32.247), ("1m0.5s", 60.5), ("250ms", 0.25), ("1h2m3s", 3723.0)],
)
def test_a_go_duration_is_read_in_seconds(duration: str, seconds: float) -> None:
    """metrics-server writes a sample's window as Go prints a duration."""
    assert go_seconds(duration) == pytest.approx(seconds)


@pytest.mark.parametrize("duration", ["32", "32x", "s", "", "1s2"])
def test_a_window_that_is_not_a_go_duration_is_refused(duration: str) -> None:
    with pytest.raises(AutoscaleError, match="duration"):
        go_seconds(duration)


def pod_metrics(*pods: tuple[str, str, list[str]]) -> dict[str, Any]:
    """metrics-server's PodMetricsList: per pod its sample's end, its window and each container's
    CPU."""
    return {
        "kind": "PodMetricsList",
        "items": [
            {
                "metadata": {"name": f"vllm-{index}"},
                "timestamp": timestamp,
                "window": window,
                "containers": [
                    {"name": f"c{number}", "usage": {"cpu": cpu, "memory": "1Ki"}}
                    for number, cpu in enumerate(cpus)
                ],
            }
            for index, (timestamp, window, cpus) in enumerate(pods)
        ],
    }


# 2026-11-17T16:41:00Z, the warm-up's end in these tests.
WARMED = 1794933660.0


@pytest.mark.parametrize(
    ("answer", "quiet"),
    [
        (pod_metrics(("2026-11-17T16:41:30Z", "20s", ["140m"])), True),
        (pod_metrics(("2026-11-17T16:41:30Z", "30s", ["140m"])), True),
        (pod_metrics(("2026-11-17T16:41:30Z", "31s", ["140m"])), False),
        (pod_metrics(("2026-11-17T16:41:30Z", "20s", ["940m"])), False),
        (pod_metrics(("2026-11-17T16:41:30Z", "20s", ["939m"])), True),
        (pod_metrics(("2026-11-17T16:41:30Z", "20s", ["500m", "500m"])), False),
        (
            pod_metrics(
                ("2026-11-17T16:41:30Z", "20s", ["140m"]),
                ("2026-11-17T16:41:20Z", "25s", ["140m"]),
            ),
            False,
        ),
        (pod_metrics(), False),
    ],
    ids=[
        "after the warm-up and idle",
        "window starting at the warm-up's end",
        "window reaching into the warm-up",
        "just past the target",
        "just under the target",
        "containers summed",
        "one pod's window in the warm-up",
        "no sample",
    ],
)
def test_the_cpu_is_quiet_when_every_pods_sample_is_wholly_after_the_warm_up_and_under_target(
    answer: dict[str, Any], quiet: bool
) -> None:
    """The HPA's first reading is metrics-server's latest sample; on 2026-11-17 a window reached
    32 s back, into the warm-up, and the HPA scaled on it (P3.10's review). 80% of 1174m is
    939.2m, and the HPA scales only past its target."""
    assert cpu_quiet(answer, cpu_cores("1174m"), 80, WARMED) is quiet


def ready_row(seconds: float, spec: float | None, ready: float | None) -> AutoscaleRow:
    return AutoscaleRow(seconds, spec, ready, None, None)


def test_time_to_capacity_runs_from_the_first_scale_up_to_its_pods_ready() -> None:
    rows = [
        ready_row(0, 1, 1),
        ready_row(5, 2, 1),
        ready_row(10, 2, 1),
        ready_row(15, 2, 2),
        ready_row(20, 1, 2),
    ]
    assert time_to_capacity(rows, CPU_SECTION) == 10


def test_a_scale_up_whose_pod_was_ready_at_once_took_no_time() -> None:
    assert time_to_capacity([ready_row(0, 1, 1), ready_row(5, 2, 2)], CPU_SECTION) == 0


@pytest.mark.parametrize(
    "rows",
    [
        [ready_row(0, 1, 1), ready_row(5, 1, 1)],
        [ready_row(0, 1, 1), ready_row(5, 2, 1), ready_row(10, 2, None)],
    ],
    ids=["never scaled", "never ready"],
)
def test_capacity_that_never_came_has_no_time(rows: list[AutoscaleRow]) -> None:
    assert time_to_capacity(rows, CPU_SECTION) is None


def test_capacity_asked_for_before_the_replay_is_not_timed() -> None:
    """The series cannot say when it was asked for, so 150 s would only be a lower bound (the
    real run of 2026-11-17, P3.10's review)."""
    rows = [ready_row(0, 2, 1), ready_row(5, 2, 1), ready_row(150, 2, 2)]
    assert time_to_capacity(rows, CPU_SECTION) is None


def span(run: str, pod: str | None, begun_ms: int, ended_ms: int | None) -> CallSpan:
    return CallSpan(run, pod, begun_ms, ended_ms)


def test_an_idle_pod_is_a_ready_one_with_no_call_in_flight_each_sample() -> None:
    """At 0 s pod-a is busy and the only pod; at 5 s two are ready, one busy; at 10 s pod-a has
    ended and pod-b runs. One idle pod for two 5 s samples is 10 s, a sixth of a minute."""
    rows = [ready_row(0, 1, 1), ready_row(5, 2, 2), ready_row(10, 2, 2)]
    spans = [span("a-1", "pod-a", 0, 7000), span("a-2", "pod-b", 9000, 12000)]
    assert idle_pod_minutes(rows, spans, 5) == pytest.approx(10 / 60)


def test_a_call_counts_in_flight_from_its_start_until_just_before_its_end() -> None:
    rows = [ready_row(0, 1, 1), ready_row(5, 1, 1)]
    assert idle_pod_minutes(rows, [span("a-1", "pod-a", 0, 5000)], 5) == pytest.approx(5 / 60)


def test_a_failed_call_names_no_pod_and_keeps_none_busy() -> None:
    rows = [ready_row(0, 1, 1)]
    assert idle_pod_minutes(rows, [span("a-1", None, 0, None)], 5) == pytest.approx(5 / 60)


def test_more_busy_pods_than_ready_is_no_idle_pod_and_a_blank_ready_is_no_sample() -> None:
    """A pod leaving Ready may still finish its calls; a starting pod is not idle."""
    rows = [ready_row(0, 2, 1), ready_row(5, 2, None)]
    spans = [span("a-1", "pod-a", 0, 9000), span("a-2", "pod-b", 0, 9000)]
    assert idle_pod_minutes(rows, spans, 5) == 0


def test_the_first_scale_down_is_the_first_spec_below_the_one_before() -> None:
    rows = [ready_row(0, 1, 1), ready_row(5, 2, 1), ready_row(10, None, 2), ready_row(15, 1, 2)]
    assert first_scale_down(rows) == 15
    assert first_scale_down(rows[:3]) is None


def test_an_interrupted_run_failed_a_call_begun_within_a_deadline_of_the_scale_down() -> None:
    """A call begun more than a deadline before the scale-down had failed before it could
    begin; each run counts once."""
    rows = [ready_row(0, 2, 2), ready_row(300, 1, 2)]
    spans = [
        span("a-1", None, 179_000, None),
        span("a-2", None, 181_000, None),
        span("a-2", None, 182_000, None),
        span("a-3", "pod-a", 250_000, 260_000),
        span("a-4", None, 290_000, None),
    ]
    assert interrupted_runs(rows, spans, 120) == 2


def test_no_scale_down_interrupts_no_run() -> None:
    rows = [ready_row(0, 1, 1), ready_row(5, 2, 1)]
    assert interrupted_runs(rows, [span("a-1", None, 1000, None)], 120) == 0


def call(
    run: str, step: int, gap_ms: int, wall_ms: int, duration_ms: int | None, pod: str | None
) -> dict[str, Any]:
    """A replay line: wall_ms is its ts_start on the VM's clock, as ms from the replay's start at
    1000 s; a failed call has no duration and no pod."""
    line: dict[str, Any] = {
        "run_id": run,
        "step": step,
        "gap_before_ms": gap_ms,
        "ts_start": 1_000_000 + wall_ms,
    }
    if duration_ms is not None:
        line["duration_ms"] = duration_ms
        line["pod"] = pod
    return line


def test_the_summary_times_runs_by_plan_and_lines_calls_up_with_the_series_by_the_wall() -> None:
    """Run 1 is discarded, and fails a call at 75 s, after the scale-down at 60 s, which must not
    count. By plan (D9): run 2 starts at 10 s and ends at 40 s; run 3 starts at 20 s and fails at
    70 s; run 4 starts at 30 s and ends at 50 s. The VM's clock put run 4 10 s later, 40-60 s
    (P3.10's review: on real pods the two drift apart), so its pod is idle at the 35 s sample by
    the wall, and busy there by plan. Pods: one until 10 s, two asked for at 5 s and Ready at
    15 s, one again from 60 s. Idle by the wall: one at 5, 15 and 35 s, two at 60 s."""
    rows = [
        ready_row(0, 1, 1),
        ready_row(5, 2, 1),
        ready_row(15, 2, 2),
        ready_row(35, 2, 2),
        ready_row(60, 1, 2),
    ]
    lines = [
        call("a-1", 1, 0, 0, 5_000, "pod-a"),
        call("a-1", 2, 0, 75_000, None, None),
        call("a-2", 1, 0, 10_000, 15_000, "pod-a"),
        call("a-2", 2, 2, 25_002, 14_998, "pod-a"),
        call("a-3", 1, 0, 20_000, 10_000, "pod-b"),
        call("a-3", 2, 40_000, 70_000, None, None),
        call("a-4", 1, 0, 40_000, 20_000, "pod-b"),
    ]
    summary = summarise_autoscale(
        rows, lines, [0.0, 10.0, 20.0, 30.0], 1000.0, 1, CPU_SECTION, 5, 120
    )
    assert summary.as_meta() == {
        "runs": 3,
        "finished_runs": 2,
        "failed_runs": 1,
        "interrupted_runs": 1,
        "completion_p50_ms": 20_000,
        "completion_p95_ms": 30_000,
        "completion_total_ms": 50_000,
        "makespan_ms": 40_000,
        "runs_per_minute": pytest.approx(3.0),
        "scaled_before_replay": False,
        "time_to_capacity_seconds": 10,
        "idle_pod_minutes": pytest.approx(25 / 60),
    }


def test_a_summary_with_no_finished_run_has_no_times() -> None:
    rows = [ready_row(0, 1, 1)]
    lines = [call("a-1", 1, 0, 0, None, None)]
    summary = summarise_autoscale(rows, lines, [0.0], 1000.0, 0, CPU_SECTION, 5, 120)
    assert summary.finished_runs == 0
    assert summary.completion_p50_ms is None
    assert summary.completion_p95_ms is None
    assert summary.completion_total_ms == 0
    assert summary.makespan_ms is None
    assert summary.runs_per_minute is None


def test_a_summary_of_pods_scaled_before_the_replay_says_so_and_has_no_capacity_time() -> None:
    rows = [ready_row(0, 2, 1), ready_row(150, 2, 2)]
    lines = [call("a-1", 1, 0, 0, 100, "pod-a")]
    summary = summarise_autoscale(rows, lines, [0.0], 1000.0, 0, CPU_SECTION, 5, 120)
    assert summary.scaled_before_replay is True
    assert summary.time_to_capacity_seconds is None


def test_the_spans_are_the_calls_on_the_vms_clock_from_the_replays_start() -> None:
    """The clock Prometheus stamps its samples with, so the two line up (P3.10's review). A step's
    start follows the wall, not its run's plan: here 2 s after its step 1 ended."""
    lines = [
        call("a-2", 1, 0, 50_000, 900, "pod-a"),
        call("a-2", 2, 3, 52_900, None, None),
    ]
    assert wall_spans(lines, 1000.0) == [
        CallSpan("a-2", "pod-a", 50_000, 50_900),
        CallSpan("a-2", None, 52_900, None),
    ]


def test_the_replays_start_is_rounded_to_the_millisecond() -> None:
    assert wall_spans([call("a-1", 1, 0, 0, 10, "pod-a")], 999.9996) == [
        CallSpan("a-1", "pod-a", 0, 10)
    ]


SUMMARY = AutoscaleSummary(
    runs=26,
    finished_runs=25,
    failed_runs=1,
    interrupted_runs=1,
    completion_p50_ms=41_000,
    completion_p95_ms=90_500,
    completion_total_ms=1_200_000,
    makespan_ms=600_000,
    runs_per_minute=2.5,
    scaled_before_replay=False,
    time_to_capacity_seconds=None,
    idle_pod_minutes=1.25,
)


def test_the_summary_csv_has_a_row_per_variant_with_its_autoscaler(tmp_path: Path) -> None:
    path = tmp_path / "x-autoscale-summary.csv"
    write_summary_csv(path, {"remaining": ("remaining", SUMMARY.as_meta())})
    with path.open() as file:
        [only] = list(csv.DictReader(file))
    assert only["variant"] == "remaining"
    assert only["autoscaler"] == "remaining"
    assert only["completion_p95_ms"] == "90500"
    assert only["runs_per_minute"] == "2.5"
    assert only["scaled_before_replay"] == "False"
    assert only["time_to_capacity_seconds"] == ""
    with pytest.raises(FileExistsError):
        write_summary_csv(path, {})


def test_the_summary_goes_into_json_and_back() -> None:
    assert AutoscaleSummary(**json.loads(json.dumps(SUMMARY.as_meta()))) == SUMMARY


def test_the_summary_text_gives_each_number() -> None:
    assert summary_text(SUMMARY) == (
        "  25 of 26 kept runs finished in 600.0 s, 2.50 per minute; run time p50 41.0 s, p95 "
        "90.5 s, total 1200.0 s; capacity never came; 1.25 idle pod-minutes; 1 of 1 failed runs "
        "interrupted by a scale-down"
    )


def test_the_summary_text_says_capacity_was_not_timed_when_scaled_before_the_replay() -> None:
    summary = AutoscaleSummary(**{**SUMMARY.as_meta(), "scaled_before_replay": True})
    assert "capacity not timed, the pods were scaled up before the replay;" in summary_text(summary)
