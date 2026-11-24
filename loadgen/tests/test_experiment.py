"""make experiment (P2.5): the experiment file, the order of the steps, and what a run writes.

The cluster is a fake that records what it is asked, so these tests say what must happen around a
replay and in which order. The live path (helm, kubectl, the gateway) is what make experiment's
done-when proves.
"""

import asyncio
import csv
import hashlib
import itertools
import json
import subprocess
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from load_generator.autoscale import (
    AutoscaleRow,
    AutoscaleSummary,
    CpuRow,
    Crossings,
    ScaleUpWindows,
    Window,
    crossing_text,
    helm_values,
    scale_up_text,
    scale_up_windows,
    summary_text,
)
from load_generator.experiment import (
    GPU_FIELDS,
    WINDOWS_GPU_COUNTERS,
    Experiment,
    ExperimentError,
    LiveCluster,
    PodStall,
    Warmup,
    check_autoscaling,
    check_journal_readable,
    check_routing,
    check_same_replay,
    check_stall_window,
    count_clock_jumps,
    finish,
    load_experiment,
    main,
    outputs,
    plugin_on,
    pod_stalls_from,
    pods_to_warm,
    refuse_leftovers,
    report_lines,
    run_experiment,
    served_config_file,
    short_of_cache,
    warm_pod,
    warmed_ttft_ms,
    windows_gpu_csv,
    write_results,
)
from load_generator.plan import (
    Pattern,
    choose_runs,
    load_replay_config,
    load_trace,
    start_times,
    warmup_run,
)
from load_generator.record import experiment_hash
from load_generator.results import CallRow

REPO_ROOT = Path(__file__).resolve().parents[2]
# What the fake cluster's autoscaler applies, and the series it then reads from Prometheus:
# remaining work passes 10 per pod at 5 s, spec replicas rise at 10 s, the queue passes 1 per pod
# at 15 s; the context passes real's 4384 per pod at 5 s, and KEDA's operator restarts at 10 s
# (P4.12: the last two columns, a spec change). The second pod is never Ready.
AUTOSCALING = {
    "min_replicas": 1,
    "max_replicas": 8,
    "remaining_work_target": 10,
    "queue_target": 1,
    "pressure_target": 1.0,
    "scale_down_stabilization_seconds": 300,
}
AUTOSCALE_ROWS = [
    AutoscaleRow(0.0, 1, 1, 0, 5, 1000, 4),
    AutoscaleRow(5.0, 1, 1, 0, 11, 4500, 4),
    AutoscaleRow(10.0, 2, 1, 2, 20, 9000, 5),
    AutoscaleRow(15.0, 2, 1, 3, 20, 9000, 5),
]
# real's model.kv_cache_tokens at pressure_target 1.0, the threshold the fake's variants run at.
REAL_CONTEXT_THRESHOLD = 4384
# The plain HPA's reading of the pods' CPU over the same replay (P3.10).
CPU_ROWS = [CpuRow(0.0, 12), CpuRow(5.0, 97.5)]
ROUND_ROBIN_VALUES = REPO_ROOT / "infra" / "helm" / "router-round-robin-values.yaml"
BASELINE = REPO_ROOT / "experiments" / "baseline.yaml"
BASELINE_CHECK = REPO_ROOT / "experiments" / "baseline-check.yaml"
BASELINE_REVERSED = REPO_ROOT / "experiments" / "baseline-reversed.yaml"
APB_TRACE = "convert-apb-ca5fc26bbb1d-cd81f326aece"
TRACE_DIR = REPO_ROOT / "experiments" / "results" / "traces"


def trace_file(directory: Path) -> Path:
    path = directory / "trace.jsonl"
    lines = [
        {
            "schema_version": 1,
            "run_id": run,
            "task_type": "csv",
            "step": step,
            "ts_start": 0,
            "gap_before_ms": 0 if step == 1 else 2,
            "expected_steps": 2,
            "prompt_tokens": 50,
            "completion_tokens": 5,
            "duration_ms": 100,
        }
        for run in ("a", "b", "c")
        for step in (1, 2)
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def experiment_file(directory: Path, **changes: Any) -> Path:
    values = directory / "round-robin-values.yaml"
    values.write_text("router: {}\n")
    trace_file(directory)
    spec: dict[str, Any] = {
        "profile": "real",
        "trace": "trace.jsonl",
        "pattern": "baseline",
        "seed": 6,
        "kv_capacity_tokens_per_pod": 4384,
        "warmup": {"runs_discarded": 1, "calls_per_pod": 2, "words": 10, "completion_tokens": 3},
        "variants": [
            {
                "name": "round-robin",
                "values": "round-robin-values.yaml",
                "epp_config": "round-robin.yaml",
            },
            {"name": "default", "values": None, "epp_config": "default-plugins.yaml"},
        ],
    }
    spec.update(changes)
    path = directory / "exp.yaml"
    path.write_text(yaml.safe_dump(spec))
    return path


def replay_lines(pod: str = "pod-a") -> list[dict[str, Any]]:
    """Runs 1 and 2 of a plan, one call each, and run 2 on the given pod."""
    return [
        {
            "schema_version": 1,
            "run_id": f"a-{position}",
            "task_type": "csv",
            "step": 1,
            "ts_start": 0,
            "gap_before_ms": 1,
            "expected_steps": 2,
            "prompt_tokens": 50,
            "completion_tokens": 5,
            "duration_ms": 100,
            "ttft_ms": 10,
            "cached_tokens": 16,
            "pod": "pod-a" if position == 1 else pod,
        }
        for position in (1, 2)
    ]


class FakeCluster:
    def __init__(
        self,
        replays: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        running_config: Callable[[Path | None], str] | None = None,
        fail_replay_of: str | None = None,
        fail_restart_number: int | None = None,
        fail_switch_number: int | None = None,
        fail_gpu_check: bool = False,
        gpu_exit: int | None = None,
        stalls: Mapping[str, Sequence[PodStall]] | None = None,
        fail_autoscaler_start: bool = False,
        fail_windows_gpu_check: bool = False,
        windows_gpu_exit: int | None = None,
    ) -> None:
        self.asked: list[str] = []
        # Windows' GPU memory log's steps, kept as the GPU log's are (P4.13c).
        self.windows_gpu: list[tuple[str, int]] = []
        self.fail_windows_gpu_check = fail_windows_gpu_check
        self.windows_gpu_exit = windows_gpu_exit
        self.fail_autoscaler_start = fail_autoscaler_start
        # Each variant's pod stalls, found after its replay (P3.7).
        self.stalls = stalls or {}
        self.replayed_variant: str | None = None
        # The GPU log's steps apart from the others, each with how many cluster steps came before
        # it, so the order holds without every sequence above naming them (P3.7).
        self.gpu: list[tuple[str, int]] = []
        self.fail_gpu_check = fail_gpu_check
        self.gpu_exit = gpu_exit
        # Each vLLM-log take, with the variant just replayed and how many cluster steps came
        # before it, apart from asked for the same reason as the GPU log's (P3.7c).
        self.vllm: list[tuple[str | None, int]] = []
        self.replays = replays
        self.fail_replay_of = fail_replay_of
        self.fail_restart_number = fail_restart_number
        self.fail_switch_number = fail_switch_number
        self.values: Path | None = None
        self.routing: Mapping[str, bool] | None = None
        # The routing each replay was handed, in order.
        self.replayed_routing: list[Mapping[str, bool] | None] = []
        self.running_config = running_config or self._config_of

    def _config_of(self, values: Path | None) -> str:
        if self.routing is not None:
            return "thesis-plugins.yaml"
        return "default-plugins.yaml" if values is None else "round-robin.yaml"

    def check_ready(self) -> None:
        self.asked.append("check")

    def deploy(self, profile: str) -> None:
        self.asked.append(f"deploy {profile}")

    def scale(self, replicas: int | None) -> None:
        self.asked.append(f"scale {'profile' if replicas is None else replicas}")

    def restart_pods(self) -> None:
        self.asked.append("restart")
        if self.asked.count("restart") == self.fail_restart_number:
            raise RuntimeError("the restart broke")

    def set_routing(self, values: Path | None, routing: Mapping[str, bool] | None) -> None:
        if routing is not None:
            name = "routing " + ",".join(f"{rule}={on}" for rule, on in sorted(routing.items()))
        else:
            name = f"routing {values.name if values else 'default'}"
        self.asked.append(name)
        switches = sum(step.startswith("routing") for step in self.asked)
        if switches == self.fail_switch_number:
            raise RuntimeError("the switch broke")
        self.values = values
        self.routing = routing

    def epp_config_file(self) -> str:
        self.asked.append("config")
        return self.running_config(self.values)

    def warm_pods(self) -> dict[str, list[int]]:
        self.asked.append("warm")
        return {"pod-a": [9000, 100], "pod-b": [8000, 90]}

    def replay(self, out: Path, routing: Mapping[str, bool] | None) -> list[dict[str, Any]]:
        self.asked.append(f"replay {out.name}")
        self.replayed_routing.append(routing)
        if self.fail_replay_of and self.fail_replay_of in out.name:
            raise RuntimeError("the replay broke")
        variant = out.stem.rsplit("-", 1)[-1]
        self.replayed_variant = variant
        lines = (self.replays or {}).get(variant, replay_lines())
        out.write_text("".join(json.dumps(line) + "\n" for line in lines))
        return [dict(line) for line in lines]

    def clock_jumps(self, since: float, until: float) -> int:
        self.asked.append(f"jumps {since:.0f} {until:.0f}")
        return 3

    def check_gpu(self) -> None:
        self.gpu.append(("gpu check", len(self.asked)))
        if self.fail_gpu_check:
            raise ExperimentError("nvidia-smi exited with 9")

    def start_gpu_log(self, path: Path) -> None:
        self.gpu.append((f"gpu log {path.name}", len(self.asked)))

    def stop_gpu_log(self) -> int | None:
        self.gpu.append(("gpu stop", len(self.asked)))
        return self.gpu_exit

    def check_windows_gpu(self) -> None:
        self.windows_gpu.append(("check", len(self.asked)))
        if self.fail_windows_gpu_check:
            raise ExperimentError("typeperf exited with 2")

    def start_windows_gpu_log(self, path: Path) -> None:
        self.windows_gpu.append((f"log {path.name}", len(self.asked)))

    def stop_windows_gpu_log(self) -> int | None:
        self.windows_gpu.append(("stop", len(self.asked)))
        return self.windows_gpu_exit

    def picker_log(self) -> str:
        self.asked.append("picker log")
        return "the picker's log\n"

    def vllm_logs(self) -> dict[str, str]:
        self.vllm.append((self.replayed_variant, len(self.asked)))
        return {"vllm-x-pod-a": "pod a's log\n", "vllm-x-pod-b": "pod b's log\n"}

    def pod_stalls(self, since: float, until: float, seconds: int) -> list[PodStall]:
        self.asked.append(f"stalls {since:.0f} {until:.0f} {seconds}")
        return list(self.stalls.get(self.replayed_variant or "", []))

    def floor_autoscaler(self, kind: str) -> dict[str, Any]:
        self.asked.append(f"floor {kind}")
        return dict(AUTOSCALING)

    def start_autoscaler(self) -> None:
        self.asked.append("autoscaler on")
        if self.fail_autoscaler_start:
            raise RuntimeError("the autoscaler start broke")

    def stop_autoscaler(self) -> None:
        self.asked.append("autoscaler off")

    def autoscale_series(self, since: float, until: float) -> list[AutoscaleRow]:
        self.asked.append(f"autoscale series {since:.0f} {until:.0f}")
        return list(AUTOSCALE_ROWS)

    def cpu_series(self, since: float, until: float) -> list[CpuRow]:
        self.asked.append(f"cpu series {since:.0f} {until:.0f}")
        return list(CPU_ROWS)


def ticking_clock() -> Callable[[], float]:
    now = [100.0]

    def clock() -> float:
        now[0] += 1
        return now[0]

    return clock


def loaded(directory: Path, **changes: Any) -> Experiment:
    return load_experiment(experiment_file(directory, **changes), {"baseline": PATTERN})


PATTERN = Pattern(runs=2, rate_per_second=0.02, burst=None, fan_out=None)


def test_the_file_gives_the_experiment(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    assert experiment.name == "exp"
    assert experiment.profile == "real"
    assert experiment.trace == tmp_path / "trace.jsonl"
    assert experiment.pattern_name == "baseline"
    assert experiment.pattern == PATTERN
    assert experiment.seed == 6
    assert experiment.kv_capacity_tokens_per_pod == 4384
    assert experiment.warmup == Warmup(
        runs_discarded=1, calls_per_pod=2, words=10, completion_tokens=3
    )
    assert [variant.name for variant in experiment.variants] == ["round-robin", "default"]
    assert experiment.variants[0].values == tmp_path / "round-robin-values.yaml"
    assert experiment.variants[0].epp_config == "round-robin.yaml"
    assert experiment.variants[1].values is None


def test_the_hash_is_of_the_file_so_any_edit_changes_the_stamp(tmp_path: Path) -> None:
    path = experiment_file(tmp_path)
    before = load_experiment(path, {"baseline": PATTERN}).hash
    assert before == experiment_hash(path)
    path.write_text(path.read_text() + "# an edit\n")
    assert load_experiment(path, {"baseline": PATTERN}).hash != before


def test_a_pattern_the_profile_lacks_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ExperimentError, match="no-such"):
        load_experiment(experiment_file(tmp_path, pattern="no-such"), {"baseline": PATTERN})


def test_discarding_every_run_is_refused(tmp_path: Path) -> None:
    warmup = {"runs_discarded": 2, "calls_per_pod": 2, "words": 10, "completion_tokens": 3}
    with pytest.raises(ExperimentError, match="runs_discarded"):
        loaded(tmp_path, warmup=warmup)


def test_two_variants_of_one_name_are_refused(tmp_path: Path) -> None:
    variant = {"name": "same", "values": None, "epp_config": "default-plugins.yaml"}
    with pytest.raises(ExperimentError, match="same"):
        loaded(tmp_path, variants=[variant, variant])


def test_a_values_file_that_is_not_there_is_refused(tmp_path: Path) -> None:
    variant = {"name": "gone", "values": "gone.yaml", "epp_config": "x.yaml"}
    with pytest.raises(ExperimentError, match=r"gone\.yaml"):
        loaded(tmp_path, variants=[variant])


ROUTER_VARIANT = {
    "name": "run-router",
    "values": None,
    "routing": {"plugin": True, "affinity": True},
    "epp_config": "thesis-plugins.yaml",
}
DEFAULT_VARIANT = {"name": "default", "values": None, "epp_config": "default-plugins.yaml"}


def test_a_variant_may_turn_routing_rules_on(tmp_path: Path) -> None:
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, ROUTER_VARIANT])
    default, router = experiment.variants
    assert default.routing is None
    assert router.routing == {"plugin": True, "affinity": True}
    assert router.values is None
    assert router.epp_config == "thesis-plugins.yaml"


def test_a_variant_with_a_values_file_and_routing_is_refused(tmp_path: Path) -> None:
    """A routing variant needs the picker built from gateway/ on thesis-plugins.yaml, and a values
    file swaps the image or the config file, so the two never mean one thing together."""
    variant = {**ROUTER_VARIANT, "values": "round-robin-values.yaml"}
    with pytest.raises(ExperimentError, match="run-router"):
        loaded(tmp_path, variants=[variant])


@pytest.mark.parametrize("routing", [{"affinity": "yes"}, {"affinity": None}, {}, ["affinity"]])
def test_routing_that_is_not_rules_set_to_true_or_false_is_refused(
    tmp_path: Path, routing: Any
) -> None:
    variant = {**ROUTER_VARIANT, "routing": routing}
    with pytest.raises(ExperimentError, match="run-router"):
        loaded(tmp_path, variants=[variant])


def test_routing_rules_the_profile_has_are_accepted(tmp_path: Path) -> None:
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, ROUTER_VARIANT])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    check_routing(experiment, {"real": real})


def test_a_routing_rule_the_profile_lacks_is_refused(tmp_path: Path) -> None:
    """A typo would otherwise fail only when the variant's switch runs, maybe hours in."""
    variant = {**ROUTER_VARIANT, "routing": {"plugin": True, "afinity": True}}
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, variant])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    with pytest.raises(ExperimentError, match="afinity"):
        check_routing(experiment, {"real": real})


@pytest.mark.parametrize(
    ("profile_rules", "variant_rules"),
    [({}, {"affinity": True}), ({"affinity": True}, {"priority": False})],
)
def test_a_rule_on_while_the_plugin_is_off_is_refused(
    tmp_path: Path, profile_rules: dict[str, bool], variant_rules: dict[str, bool]
) -> None:
    """It would do nothing, and the variant would read as the rule's result. infra/routing-values.sh
    refuses it too, but only at the variant's switch."""
    variant = {**ROUTER_VARIANT, "routing": variant_rules}
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, variant])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    real["routing"].update(profile_rules)
    with pytest.raises(ExperimentError, match="plugin is off"):
        check_routing(experiment, {"real": real})


def test_main_refuses_a_routing_rule_the_profile_lacks_before_the_cluster_is_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    variant = {**ROUTER_VARIANT, "routing": {"plugin": True, "afinity": True}}
    path = experiment_file(tmp_path, variants=[variant])
    profile = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    profile["replay"]["patterns"]["baseline"] = {"runs": 2, "rate_per_second": 0.02}
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "real.yaml").write_text(yaml.safe_dump(profile))
    monkeypatch.setattr("load_generator.experiment.REPO_ROOT", tmp_path)

    def no_cluster(_experiment: Experiment, _profiles: Any) -> FakeCluster:
        raise AssertionError("the cluster was touched")

    monkeypatch.setattr("load_generator.experiment.LiveCluster", no_cluster)
    monkeypatch.setattr("sys.argv", ["experiment", str(path)])
    with pytest.raises(ExperimentError, match="afinity"):
        main()


def test_a_routing_variant_switches_the_picker_and_the_default_comes_back(
    tmp_path: Path,
) -> None:
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, ROUTER_VARIANT])
    cluster = FakeCluster()
    _rows, meta = run_experiment(
        experiment, outputs(experiment, tmp_path), cluster, ticking_clock()
    )
    switches = [step for step in cluster.asked if step.startswith("routing")]
    assert switches == [
        "routing default",
        "routing affinity=True,plugin=True",
        "routing default",
    ]
    assert meta["variants"]["run-router"]["routing"] == {"plugin": True, "affinity": True}
    assert meta["variants"]["default"]["routing"] is None


def test_a_variant_may_not_set_the_profiles_ttl(tmp_path: Path) -> None:
    """Variants differ in rules only; infra/routing-values.sh refuses it too, but at the switch."""
    variant = {**ROUTER_VARIANT, "routing": {"plugin": True, "run_ttl_seconds": True}}
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, variant])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    with pytest.raises(ExperimentError, match="run_ttl_seconds"):
        check_routing(experiment, {"real": real})


def test_the_profiles_ttl_is_not_a_rule_that_is_on(tmp_path: Path) -> None:
    """A TTL of 300 is truthy, and must not read as a rule on while the plugin is off."""
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    assert real["routing"]["run_ttl_seconds"] > 0
    check_routing(experiment, {"real": real})


@pytest.mark.parametrize("key", ["overload_queue_threshold", "overload_sustained_seconds"])
def test_a_variant_may_not_set_the_profiles_overload_limits(tmp_path: Path, key: str) -> None:
    """The overload limits are the profile's, as the TTL is (P3.5); infra/routing-values.sh refuses
    them too, but at the switch. True, as for the TTL: a number is refused on loading already."""
    variant = {**ROUTER_VARIANT, "routing": {"plugin": True, key: True}}
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, variant])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    with pytest.raises(ExperimentError, match=key):
        check_routing(experiment, {"real": real})


def test_the_profiles_overload_limits_are_not_rules_that_are_on(tmp_path: Path) -> None:
    """A threshold of 1 and 10 s are truthy, and must not read as rules on while the plugin is
    off."""
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    assert real["routing"]["overload_queue_threshold"] > 0
    assert real["routing"]["overload_sustained_seconds"] > 0
    check_routing(experiment, {"real": real})


@pytest.mark.parametrize(
    ("profile_rules", "variant_rules"),
    [
        ({}, {"plugin": True, "overload_guard": True}),
        ({"plugin": True, "overload_guard": True}, {"priority": False}),
        ({"plugin": True, "affinity": True, "overload_guard": True}, {"affinity": False}),
    ],
)
def test_the_overload_guard_with_affinity_off_is_refused(
    tmp_path: Path, profile_rules: dict[str, bool], variant_rules: dict[str, bool]
) -> None:
    """The guard moves pinned runs only: with affinity off it would do nothing, and the variant
    would read as its result. infra/routing-values.sh refuses it too, but at the switch."""
    variant = {**ROUTER_VARIANT, "routing": variant_rules}
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, variant])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    real["routing"].update(profile_rules)
    with pytest.raises(ExperimentError, match="affinity is off"):
        check_routing(experiment, {"real": real})


def test_a_variant_may_not_set_the_profiles_parallel_threshold(tmp_path: Path) -> None:
    """The parallel threshold is the profile's, as the overload limits are (P3.6)."""
    variant = {**ROUTER_VARIANT, "routing": {"plugin": True, "parallel_queue_threshold": True}}
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, variant])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    with pytest.raises(ExperimentError, match="parallel_queue_threshold"):
        check_routing(experiment, {"real": real})


def test_the_profiles_parallel_threshold_is_not_a_rule_that_is_on(tmp_path: Path) -> None:
    """A threshold of 2 is truthy, and must not read as a rule on while the plugin is off."""
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    assert real["routing"]["parallel_queue_threshold"] > 0
    check_routing(experiment, {"real": real})


@pytest.mark.parametrize(
    ("profile_rules", "variant_rules"),
    [
        ({}, {"plugin": True, "parallel_spill": True}),
        ({"plugin": True, "parallel_spill": True}, {"priority": False}),
        ({"plugin": True, "affinity": True, "parallel_spill": True}, {"affinity": False}),
    ],
)
def test_parallel_spill_with_affinity_off_is_refused(
    tmp_path: Path, profile_rules: dict[str, bool], variant_rules: dict[str, bool]
) -> None:
    """The parallel rule keeps a group on its run's pinned pod (P3.6): with affinity off it would do
    nothing, and the variant would read as its result. infra/routing-values.sh refuses it too."""
    variant = {**ROUTER_VARIANT, "routing": variant_rules}
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, variant])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    real["routing"].update(profile_rules)
    with pytest.raises(ExperimentError, match="parallel_spill on, but affinity is off"):
        check_routing(experiment, {"real": real})


def test_parallel_spill_with_affinity_on_is_accepted(tmp_path: Path) -> None:
    variant = {
        **ROUTER_VARIANT,
        "routing": {"plugin": True, "affinity": True, "parallel_spill": True},
    }
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, variant])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    check_routing(experiment, {"real": real})


def test_the_overload_guard_with_affinity_on_is_accepted(tmp_path: Path) -> None:
    variant = {
        **ROUTER_VARIANT,
        "routing": {"plugin": True, "affinity": True, "overload_guard": True},
    }
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, variant])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    check_routing(experiment, {"real": real})


@pytest.mark.parametrize(
    ("profile_plugin", "variant_routing", "on"),
    [
        (False, None, False),
        (False, {"plugin": True, "affinity": True}, True),
        (True, None, True),
        (True, {"plugin": False}, False),
        (False, {"affinity": False}, False),
    ],
)
def test_the_plugin_runs_by_the_variants_rules_over_its_profiles(
    profile_plugin: bool, variant_routing: dict[str, bool] | None, on: bool
) -> None:
    profile_routing = {"plugin": profile_plugin, "affinity": False, "run_ttl_seconds": 300}
    assert plugin_on(profile_routing, variant_routing) is on


def test_each_replay_is_handed_its_variants_routing(tmp_path: Path) -> None:
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, ROUTER_VARIANT])
    cluster = FakeCluster()
    run_experiment(experiment, outputs(experiment, tmp_path), cluster, ticking_clock())
    assert cluster.replayed_routing == [None, {"plugin": True, "affinity": True}]


@pytest.mark.parametrize(
    ("routing", "end_runs"),
    [(None, False), ({"plugin": True, "affinity": True}, True)],
    ids=["the profile's picker, plugin off", "a variant with the plugin on"],
)
def test_the_live_cluster_ends_runs_only_when_the_running_picker_has_the_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    routing: dict[str, bool] | None,
    end_runs: bool,
) -> None:
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, ROUTER_VARIANT])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    handed: list[bool] = []

    def replay_to_file(*args: Any) -> list[dict[str, Any]]:
        handed.append(args[-1])
        return []

    monkeypatch.setattr("load_generator.experiment.replay_to_file", replay_to_file)
    LiveCluster(experiment, {"real": real}).replay(tmp_path / "out.jsonl", routing)
    assert handed == [end_runs]


def test_the_live_cluster_hands_set_routing_a_file_of_the_variants_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, ROUTER_VARIANT])
    handed: list[tuple[list[str], Any]] = []

    def run(args: list[str], check: bool) -> None:
        assert check
        handed.append((args, yaml.safe_load(Path(args[-1]).read_text())))

    monkeypatch.setattr("load_generator.experiment.subprocess.run", run)
    LiveCluster(experiment, {}).set_routing(None, {"plugin": True, "affinity": True})
    ((args, content),) = handed
    assert args[:3] == ["bash", str(REPO_ROOT / "infra" / "set-routing.sh"), "real"]
    assert content == {"routing": {"plugin": True, "affinity": True}}


def test_a_trace_that_is_not_there_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ExperimentError, match=r"missing\.jsonl"):
        loaded(tmp_path, trace="missing.jsonl")


def test_every_output_carries_the_name_and_hash(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    out = outputs(experiment, tmp_path)
    stem = f"exp-{experiment.hash}"
    assert out.csv == tmp_path / "experiments" / "results" / f"{stem}.csv"
    assert out.meta == tmp_path / "experiments" / "results" / f"{stem}.meta.json"
    assert out.graph == tmp_path / "experiments" / "graphs" / f"{stem}.png"
    assert out.replay("default") == tmp_path / "experiments" / "results" / f"{stem}-default.jsonl"


@pytest.mark.parametrize(
    "leftover",
    ["exp-{hash}.csv", "exp-{hash}.meta.json", "exp-{hash}-default.jsonl",
     "exp-{hash}-default.jsonl.partial"],
)  # fmt: skip
def test_an_earlier_result_stops_the_experiment_before_it_touches_the_cluster(
    tmp_path: Path, leftover: str
) -> None:
    experiment = loaded(tmp_path)
    results = tmp_path / "experiments" / "results"
    results.mkdir(parents=True)
    (results / leftover.format(hash=experiment.hash)).write_text("kept")
    with pytest.raises(ExperimentError, match=experiment.hash):
        refuse_leftovers(experiment, outputs(experiment, tmp_path))


def test_a_graph_left_behind_stops_it_too(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    graphs = tmp_path / "experiments" / "graphs"
    graphs.mkdir(parents=True)
    (graphs / f"exp-{experiment.hash}.png").write_text("kept")
    with pytest.raises(ExperimentError, match=experiment.hash):
        refuse_leftovers(experiment, outputs(experiment, tmp_path))


def test_nothing_left_behind_passes(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    refuse_leftovers(experiment, outputs(experiment, tmp_path))


def test_each_variant_starts_from_restarted_pods_and_warm_ones_in_this_order(
    tmp_path: Path,
) -> None:
    experiment = loaded(tmp_path)
    cluster = FakeCluster()
    run_experiment(experiment, outputs(experiment, tmp_path), cluster, ticking_clock())
    stem = f"exp-{experiment.hash}"
    assert cluster.asked == [
        "check",
        "restart",
        "routing round-robin-values.yaml",
        "config",
        "warm",
        f"replay {stem}-round-robin.jsonl",
        "jumps 101 102",
        "restart",
        "routing default",
        "config",
        "warm",
        f"replay {stem}-default.jsonl",
        "jumps 103 104",
    ]


def test_ending_on_the_default_does_not_switch_routing_again(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    cluster = FakeCluster()
    run_experiment(experiment, outputs(experiment, tmp_path), cluster, ticking_clock())
    assert cluster.asked.count("routing default") == 1


def test_ending_on_another_variant_puts_the_default_back(tmp_path: Path) -> None:
    variant = {
        "name": "round-robin",
        "values": "round-robin-values.yaml",
        "epp_config": "round-robin.yaml",
    }
    experiment = loaded(tmp_path, variants=[variant])
    cluster = FakeCluster()
    run_experiment(experiment, outputs(experiment, tmp_path), cluster, ticking_clock())
    assert cluster.asked[-1] == "routing default"
    assert cluster.values is None


def test_a_replay_that_breaks_still_puts_the_default_back(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    cluster = FakeCluster(fail_replay_of="round-robin")
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="broke"):
        run_experiment(experiment, out, cluster, ticking_clock())
    assert cluster.asked[-1] == "routing default"
    assert cluster.values is None


def test_a_restart_that_breaks_after_another_variant_still_puts_the_default_back(
    tmp_path: Path,
) -> None:
    """The next variant is the default, but its switch has not happened yet."""
    experiment = loaded(tmp_path)
    cluster = FakeCluster(fail_restart_number=2)
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="restart broke"):
        run_experiment(experiment, out, cluster, ticking_clock())
    assert cluster.asked[-1] == "routing default"
    assert cluster.values is None


def test_a_switch_back_that_breaks_is_tried_again(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    cluster = FakeCluster(fail_switch_number=2)
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="switch broke"):
        run_experiment(experiment, out, cluster, ticking_clock())
    assert cluster.asked.count("routing default") == 2
    assert cluster.values is None


def test_a_first_switch_that_breaks_halfway_is_put_back_too(tmp_path: Path) -> None:
    """helm may have changed the picker before it failed."""
    experiment = loaded(tmp_path)
    cluster = FakeCluster(fail_switch_number=1)
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="switch broke"):
        run_experiment(experiment, out, cluster, ticking_clock())
    assert cluster.asked[-2:] == ["routing round-robin-values.yaml", "routing default"]
    assert "warm" not in cluster.asked


def test_a_restart_that_breaks_before_any_switch_leaves_routing_alone(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    cluster = FakeCluster(fail_restart_number=1)
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="restart broke"):
        run_experiment(experiment, out, cluster, ticking_clock())
    assert not any(step.startswith("routing") for step in cluster.asked)


def test_a_failed_switch_back_does_not_lose_a_finished_experiment(tmp_path: Path) -> None:
    """baseline-reversed ends on another variant, so its last helm call comes after a 1.5 h run."""
    variant = {
        "name": "round-robin",
        "values": "round-robin-values.yaml",
        "epp_config": "round-robin.yaml",
    }
    experiment = loaded(tmp_path, variants=[variant])
    cluster = FakeCluster(fail_switch_number=2)
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="switch broke"):
        run_experiment(
            experiment,
            out,
            cluster,
            ticking_clock(),
            lambda rows, meta: write_results(out, rows, meta),
        )
    assert out.csv.exists()
    assert json.loads(out.meta.read_text())["hash"] == experiment.hash
    assert out.graph.exists()


def test_main_prints_the_report_before_the_default_is_put_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If the switch back fails, the log's numbers are still on the screen."""
    variants = [
        {"name": "default", "values": None, "epp_config": "default-plugins.yaml"},
        {
            "name": "round-robin",
            "values": "round-robin-values.yaml",
            "epp_config": "round-robin.yaml",
        },
    ]
    path = experiment_file(tmp_path, variants=variants)
    profile = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    profile["replay"]["patterns"]["baseline"] = {"runs": 2, "rate_per_second": 0.02}
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "real.yaml").write_text(yaml.safe_dump(profile))
    monkeypatch.setattr("load_generator.experiment.REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        "load_generator.experiment.LiveCluster",
        lambda _experiment, _profile: FakeCluster(fail_switch_number=3),
    )
    monkeypatch.setattr("sys.argv", ["experiment", str(path)])
    with pytest.raises(RuntimeError, match="switch broke"):
        main()
    assert "runs finished" in capsys.readouterr().out
    experiment = load_experiment(path, {"baseline": PATTERN})
    assert outputs(experiment, tmp_path).csv.exists()


def test_results_are_handed_over_before_the_default_is_put_back(tmp_path: Path) -> None:
    variant = {
        "name": "round-robin",
        "values": "round-robin-values.yaml",
        "epp_config": "round-robin.yaml",
    }
    experiment = loaded(tmp_path, variants=[variant])
    cluster = FakeCluster()
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    switches_when_handed: list[int] = []
    run_experiment(
        experiment,
        out,
        cluster,
        ticking_clock(),
        lambda _rows, _meta: switches_when_handed.append(cluster.asked.count("routing default")),
    )
    assert switches_when_handed == [0]
    assert cluster.asked[-1] == "routing default"


def test_a_failed_variant_hands_over_no_results(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    handed: list[object] = []
    with pytest.raises(RuntimeError, match="broke"):
        run_experiment(
            experiment,
            out,
            FakeCluster(fail_replay_of="default"),
            ticking_clock(),
            lambda rows, _meta: handed.append(rows),
        )
    assert handed == []


def test_the_stamp_is_of_the_profile_at_the_start_not_after_the_long_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An edit during the 1.5 h run must not stamp the results with a profile they did not use."""
    monkeypatch.setattr("load_generator.experiment.REPO_ROOT", tmp_path)
    profile = tmp_path / "config" / "real.yaml"
    profile.parent.mkdir()
    profile.write_text("as loaded\n")

    class EditedDuringTheRun(FakeCluster):
        def replay(self, out: Path, routing: Mapping[str, bool] | None) -> list[dict[str, Any]]:
            profile.write_text("edited later\n")
            return super().replay(out, routing)

    experiment = loaded(tmp_path)
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    _, meta = run_experiment(experiment, out, EditedDuringTheRun(), ticking_clock())
    assert meta["profile_sha256"] == hashlib.sha256(b"as loaded\n").hexdigest()
    assert meta["trace"] == "trace.jsonl"


def test_a_picker_on_another_config_stops_before_any_call_is_sent(tmp_path: Path) -> None:
    """Both variants on one picker would look like a result: no difference."""
    experiment = loaded(tmp_path)
    cluster = FakeCluster(running_config=lambda _: "default-plugins.yaml")
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    with pytest.raises(ExperimentError, match=r"round-robin\.yaml"):
        run_experiment(experiment, out, cluster, ticking_clock())
    assert not any(step.startswith(("warm", "replay")) for step in cluster.asked)
    assert cluster.asked[-1] == "routing default"


def test_the_results_have_every_variants_rows_without_the_discarded_run(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    rows, _ = run_experiment(experiment, out, FakeCluster(), ticking_clock())
    assert [(row.variant, row.run) for row in rows] == [("round-robin", "a-2"), ("default", "a-2")]


def test_the_rows_split_by_the_files_kv_capacity(tmp_path: Path) -> None:
    """The fake's calls last 100 ms and start seconds apart, so each is alone on its pod with its 55
    tokens, which is over the first capacity and under the second."""
    over = loaded(tmp_path, kv_capacity_tokens_per_pod=54)
    under = loaded(tmp_path, kv_capacity_tokens_per_pod=1000)
    rows_over, _ = run_experiment(
        over, outputs(over, tmp_path / "over"), FakeCluster(), ticking_clock()
    )
    rows_under, _ = run_experiment(
        under, outputs(under, tmp_path / "under"), FakeCluster(), ticking_clock()
    )
    assert {row.over_kv_limit for row in rows_over} == {True}
    assert {row.over_kv_limit for row in rows_under} == {False}


def test_a_failed_call_does_not_stop_the_experiment_and_is_a_row(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    lines = replay_lines()
    del lines[1]["duration_ms"], lines[1]["ttft_ms"], lines[1]["pod"], lines[1]["cached_tokens"]
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    rows, meta = run_experiment(
        experiment, out, FakeCluster(replays={"robin": lines}), ticking_clock()
    )
    assert [row.duration_ms for row in rows] == [None, 100]
    assert meta["variants"]["round-robin"]["failed_calls"] == 1
    assert meta["variants"]["default"]["failed_calls"] == 0


def test_the_meta_says_what_the_csv_cannot(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    _, meta = run_experiment(
        experiment, out, FakeCluster(replays={"robin": replay_lines("pod-b")}), ticking_clock()
    )
    assert meta["hash"] == experiment.hash
    assert meta["profile"] == "real"
    assert meta["pattern"] == "baseline"
    assert meta["seed"] == 6
    robin = meta["variants"]["round-robin"]
    assert robin["epp_config"] == "round-robin.yaml"
    assert robin["warmup_ttft_ms"] == {"pod-a": [9000, 100], "pod-b": [8000, 90]}
    assert robin["clock_jumps"] == 3
    assert robin["calls_per_pod"] == {"pod-a": 1, "pod-b": 1}
    assert robin["pod_switch_share"] == 1.0
    assert meta["variants"]["default"]["pod_switch_share"] == 0.0
    # The trace's first step has gap 0; the fake's lines have gap 1.
    assert robin["max_gap_deviation_ms"] == 1


def test_results_are_written_as_csv_meta_and_a_graph_from_the_csv(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    rows, meta = run_experiment(experiment, out, FakeCluster(), ticking_clock())
    write_results(out, rows, meta)
    with out.csv.open() as file:
        assert len(list(csv.DictReader(file))) == 2
    assert json.loads(out.meta.read_text())["hash"] == experiment.hash
    assert out.graph.read_bytes().startswith(b"\x89PNG")


def test_two_runs_of_one_file_give_the_same_row_count(tmp_path: Path) -> None:
    """The done-when's check, when no call fails."""
    experiment = loaded(tmp_path)
    first = outputs(experiment, tmp_path / "first")
    second = outputs(experiment, tmp_path / "second")
    for out in (first, second):
        out.csv.parent.mkdir(parents=True)
    one, _ = run_experiment(experiment, first, FakeCluster(), ticking_clock())
    other, _ = run_experiment(experiment, second, FakeCluster(), ticking_clock())
    assert len(one) == len(other)


def test_pods_to_warm_are_the_ready_ones() -> None:
    pods = {
        "items": [
            {
                "metadata": {"name": "ready"},
                "status": {
                    "phase": "Running",
                    "podIP": "10.0.0.1",
                    "conditions": [{"type": "Ready", "status": "True"}],
                },
            },
            {
                "metadata": {"name": "loading"},
                "status": {
                    "phase": "Running",
                    "podIP": "10.0.0.2",
                    "conditions": [{"type": "Ready", "status": "False"}],
                },
            },
            {
                "metadata": {"name": "left-failed"},
                "status": {"phase": "Failed", "conditions": []},
            },
        ]
    }
    assert pods_to_warm(pods, replicas=1) == {"ready": "10.0.0.1"}


def test_fewer_ready_pods_than_the_profile_has_are_refused() -> None:
    with pytest.raises(ExperimentError, match="2"):
        pods_to_warm({"items": []}, replicas=2)


def test_a_warm_up_call_goes_to_that_pod_and_its_times_come_back() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        chunks = [
            {"choices": [{"delta": {"content": "x"}}]},
            {
                "choices": [],
                "system_fingerprint": "the-pod",
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": body["max_tokens"],
                    "prompt_tokens_details": {"cached_tokens": 0},
                },
            },
        ]
        text = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, content=text.encode())

    async def warm() -> list[int]:
        async with httpx.AsyncClient(
            base_url="http://10.0.0.1:8000", transport=httpx.MockTransport(handler)
        ) as client:
            return await warm_pod(client, "the-model", warmup_run(1, 3, 10, 3), 10.0)

    times = asyncio.run(warm())
    assert len(times) == 3
    assert all(time >= 0 for time in times)
    assert {request.url.host for request in seen} == {"10.0.0.1"}
    contents = [json.loads(request.content)["messages"][0]["content"] for request in seen]
    assert len(set(contents)) == 3


def test_a_warm_up_call_that_fails_stops_the_experiment() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async def warm() -> list[int]:
        async with httpx.AsyncClient(
            base_url="http://10.0.0.1:8000", transport=httpx.MockTransport(handler)
        ) as client:
            return await warm_pod(client, "the-model", warmup_run(1, 2, 10, 3), 10.0)

    with pytest.raises(ExperimentError, match="warm"):
        asyncio.run(warm())


def completed(code: int, out: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["journalctl"], code, stdout=out)


def test_clock_jumps_are_the_lines_the_journal_finds(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[list[str]] = []

    def run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        asked.append(command)
        return completed(0, "one Clock change detected\ntwo Clock change detected\n")

    monkeypatch.setattr(subprocess, "run", run)
    assert count_clock_jumps(1000.4, 2000.6) == 2
    assert "@1000" in asked[0]
    assert "@2001" in asked[0]


def test_no_clock_jumps_is_journalctls_exit_1_and_counts_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *_, **__: completed(1, ""))
    assert count_clock_jumps(1000, 2000) == 0


def test_a_journal_that_cannot_be_read_is_an_error_not_zero_jumps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *_, **__: completed(2, ""))
    with pytest.raises(ExperimentError, match="journalctl"):
        count_clock_jumps(1000, 2000)


def test_a_journal_that_shows_nothing_stops_the_experiment_before_it_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the rights to read, journalctl prints nothing, which would read as no clock jumps."""
    monkeypatch.setattr(subprocess, "run", lambda *_, **__: completed(0, ""))
    with pytest.raises(ExperimentError, match="journalctl"):
        check_journal_readable()


def test_a_journal_with_lines_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *_, **__: completed(0, "a line\n"))
    check_journal_readable()


def test_the_report_gives_each_variants_numbers_and_the_kv_split(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    out = outputs(experiment, tmp_path)
    rows, meta = run_experiment(experiment, out, FakeCluster(), ticking_clock())
    report = "\n".join(report_lines(rows, meta))
    assert "round-robin: 1 of 1 runs finished, 1 calls, 0 failed" in report
    assert "default: 1 of 1 runs finished" in report
    assert "hit rate 0.320" in report
    assert "clock jumps 3" in report
    assert "largest gap deviation 1 ms" in report
    assert "under the KV limit, all: 1 calls, TTFT p50 0.01 s" in report
    assert "over the KV limit, moved: 0 calls" in report


def call_row(run: str, step: int, prompt: int, cached: int | None, moved: bool | None) -> CallRow:
    return CallRow(
        variant="affinity",
        run=run,
        task_type="csv",
        step=step,
        pod="pod-a",
        ttft_ms=10,
        duration_ms=100,
        gap_before_ms=1,
        prompt_tokens=prompt,
        completion_tokens=5,
        cached_tokens=cached,
        moved=moved,
        tokens_on_pod=prompt + 5,
        over_kv_limit=False,
    )


@pytest.mark.parametrize(
    ("later", "short", "checked"),
    [
        # 24 less than the previous prompt of 100: the replay's 9 template tokens and a block's 15.
        (call_row("r", 2, 150, 76, False), 0, 1),
        (call_row("r", 2, 150, 75, False), 1, 1),
        (call_row("r", 2, 150, 100, False), 0, 1),
        # A moved step finds nothing of its run on its new pod: that is the cost of moving.
        (call_row("r", 2, 150, 0, True), 0, 0),
        # A reply with no cached count says nothing.
        (call_row("r", 2, 150, None, False), 0, 0),
    ],
    ids=["exactly 24 short", "25 short", "all cached", "moved", "no cached count"],
)
def test_a_stayed_step_is_short_of_cache_below_its_previous_prompt_less_24(
    later: CallRow, short: int, checked: int
) -> None:
    rows = [call_row("r", 1, 100, 0, None), later]
    assert short_of_cache(rows) == (short, checked)


def test_short_of_cache_reads_each_runs_own_previous_step() -> None:
    rows = [
        call_row("r1", 1, 100, 0, None),
        call_row("r2", 1, 400, 0, None),
        call_row("r1", 2, 150, 80, False),
        call_row("r2", 2, 450, 80, False),
        call_row("r1", 3, 200, 130, False),
    ]
    assert short_of_cache(rows) == (1, 3)


def test_the_report_counts_the_stayed_steps_short_of_cache(tmp_path: Path) -> None:
    lines = [
        {**line, "run_id": "a-2", "step": step, "prompt_tokens": prompt, "cached_tokens": cached}
        for step, prompt, cached in ((1, 50, 0), (2, 100, 10))
        for line in replay_lines()[1:]
    ]
    experiment = loaded(tmp_path)
    out = outputs(experiment, tmp_path)
    rows, meta = run_experiment(
        experiment, out, FakeCluster(replays={"default": lines}), ticking_clock()
    )
    report = "\n".join(report_lines(rows, meta))
    assert "stayed later steps short of their previous prompt in cache: 1 of 1" in report


def test_warmed_time_leaves_out_each_pods_first_call() -> None:
    """A pod's first call is slow whatever the host's speed (P2.4), so it would hide that speed."""
    assert warmed_ttft_ms({"a": [17_000, 300, 500], "b": [4000, 400]}) == pytest.approx(400.0)


def test_with_only_first_calls_there_is_no_warmed_time() -> None:
    assert warmed_ttft_ms({"a": [17_000]}) is None


def test_the_report_shows_the_hosts_speed(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    out = outputs(experiment, tmp_path)
    rows, meta = run_experiment(experiment, out, FakeCluster(), ticking_clock())
    report = "\n".join(report_lines(rows, meta))
    # (100 - 10) / 4 tokens; the fake's warm-up calls after each pod's first are 100 and 90 ms.
    assert "generation 22.5 ms per token, warm-up calls 95.0 ms to first token" in report


def test_the_running_config_file_is_read_from_the_pickers_arguments() -> None:
    args = ["--pool-name", "p", "--config-file", "/config/round-robin.yaml", "--v=3"]
    assert served_config_file(json.dumps(args)) == "round-robin.yaml"


def test_a_picker_with_no_config_argument_is_refused() -> None:
    with pytest.raises(ExperimentError, match="config-file"):
        served_config_file('["--pool-name", "p"]')


def test_a_warm_up_run_has_the_calls_asked_for_each_with_its_own_text() -> None:
    run = warmup_run(2, 3, 10, 4)
    assert run.run_id == "warmup-2"
    assert run.start_seconds == 0
    assert [step.step for step in run.steps] == [1, 2, 3]
    assert all(step.completion_tokens == 4 for step in run.steps)
    assert all(len(step.content.split()) == 10 for step in run.steps)
    assert len({step.content for step in run.steps}) == 3


def test_the_pods_get_different_warm_up_text() -> None:
    one, other = warmup_run(1, 1, 10, 4), warmup_run(2, 1, 10, 4)
    assert one.steps[0].content != other.steps[0].content


def committed(path: Path) -> Experiment:
    spec = yaml.safe_load(path.read_text())
    profile = yaml.safe_load((REPO_ROOT / "config" / f"{spec['profile']}.yaml").read_text())
    return load_experiment(path, load_replay_config(profile["replay"]).patterns)


@pytest.mark.parametrize("path", [BASELINE, BASELINE_CHECK], ids=lambda path: path.stem)
def test_the_committed_experiment_files_load(path: Path) -> None:
    experiment = committed(path)
    assert [variant.name for variant in experiment.variants] == ["round-robin", "default"]
    assert experiment.variants[0].values == ROUND_ROBIN_VALUES.resolve()
    assert experiment.variants[1].values is None
    load_trace(experiment.trace)


def test_the_baseline_runs_the_decided_load() -> None:
    experiment = committed(BASELINE)
    assert experiment.trace.name == f"{APB_TRACE}.jsonl"
    assert experiment.pattern == Pattern(40, 0.02, None, None)
    assert experiment.seed == 6
    assert experiment.warmup.runs_discarded == 5


def test_the_reversed_baseline_differs_from_the_baseline_only_in_the_order() -> None:
    """Otherwise a difference between the two could come from something other than the order."""
    baseline = yaml.safe_load(BASELINE.read_text())
    reversed_baseline = yaml.safe_load(BASELINE_REVERSED.read_text())
    assert reversed_baseline["variants"] == list(reversed(baseline["variants"]))
    del baseline["variants"], reversed_baseline["variants"]
    assert reversed_baseline == baseline


def test_the_reversed_baseline_loads_and_keeps_its_own_results() -> None:
    experiment = committed(BASELINE_REVERSED)
    assert [variant.name for variant in experiment.variants] == ["default", "round-robin"]
    assert outputs(experiment, REPO_ROOT).csv != outputs(committed(BASELINE), REPO_ROOT).csv


def test_the_baselines_first_20_runs_are_p24s_replay() -> None:
    """The profiles' comment says the load is known because of this."""
    experiment = committed(BASELINE)
    runs = load_trace(experiment.trace)
    measured = Pattern(20, 0.02, None, None)
    assert start_times(experiment.pattern, 6)[:20] == start_times(measured, 6)
    first = [run.run_id for run in choose_runs(runs, 40, 6)][:20]
    assert first == [run.run_id for run in choose_runs(runs, 20, 6)]


def test_the_kv_capacity_is_what_the_real_profiles_cache_size_gives() -> None:
    """vLLM logs 4,384 tokens for 480 MiB of KV cache on this model (P2.4). A new size in the
    profile must be measured again, since the split over and under the limit rests on it."""
    profile = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    assert profile["model"]["kv_cache_memory_bytes"] == 503316480
    assert committed(BASELINE).kv_capacity_tokens_per_pod == 4384


def test_the_round_robin_variant_is_the_default_without_its_scorers() -> None:
    """Keeps the metrics scrape, so the picker's other load does not change with the variant."""
    router = yaml.safe_load(ROUND_ROBIN_VALUES.read_text())["router"]["epp"]
    assert router["pluginsConfigFile"] == "round-robin.yaml"
    config = yaml.safe_load(router["pluginsCustomConfig"]["round-robin.yaml"])
    assert {plugin["type"] for plugin in config["plugins"]} == {
        "metrics-data-source",
        "core-metrics-extractor",
        "max-score-picker",
    }
    assert [
        plugin["pluginRef"] for profile in config["schedulingProfiles"]
        for plugin in profile["plugins"]
    ] == ["max-score-picker"]  # fmt: skip


def test_the_trace_the_experiment_names_is_committed() -> None:
    assert (TRACE_DIR / f"{APB_TRACE}.jsonl").exists()


# P2.9: variants that name their own profile and replica count.
PROFILE_VARIANTS = [
    {
        "name": "real-1pod",
        "profile": "real",
        "replicas": 1,
        "values": None,
        "epp_config": "default-plugins.yaml",
    },
    {
        "name": "sim-1pod",
        "profile": "sim",
        "replicas": 1,
        "values": None,
        "epp_config": "default-plugins.yaml",
    },
    {
        "name": "sim-2pods",
        "profile": "sim",
        "replicas": 2,
        "values": None,
        "epp_config": "default-plugins.yaml",
    },
]
VALIDATION = {"tolerance": 0.15, "pairs": [{"real": "real-1pod", "sim": "sim-1pod"}]}


def by_profile(directory: Path, **changes: Any) -> Experiment:
    return loaded(directory, profile=None, variants=PROFILE_VARIANTS, **changes)


def test_a_variant_can_name_its_own_profile_and_replicas(tmp_path: Path) -> None:
    experiment = by_profile(tmp_path)
    assert experiment.profile is None
    assert [(v.profile, v.replicas) for v in experiment.variants] == [
        ("real", 1),
        ("sim", 1),
        ("sim", 2),
    ]


def test_without_one_a_variant_takes_the_files_profile_and_its_replicas(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    assert [(v.profile, v.replicas) for v in experiment.variants] == [
        ("real", None),
        ("real", None),
    ]


def test_a_variant_with_no_profile_of_its_own_or_the_files_is_refused(tmp_path: Path) -> None:
    variants = [dict(PROFILE_VARIANTS[0]), {"name": "x", "values": None, "epp_config": "y.yaml"}]
    with pytest.raises(ExperimentError, match="x"):
        loaded(tmp_path, profile=None, variants=variants)


def test_replicas_under_one_are_refused(tmp_path: Path) -> None:
    variants = [dict(PROFILE_VARIANTS[0], replicas=0)]
    with pytest.raises(ExperimentError, match="replicas"):
        loaded(tmp_path, profile=None, variants=variants)


def test_a_file_without_validation_has_none(tmp_path: Path) -> None:
    assert loaded(tmp_path).validation is None


def test_the_validation_section_is_read_with_the_files_variants(tmp_path: Path) -> None:
    validation = by_profile(tmp_path, validation=VALIDATION).validation
    assert validation is not None
    assert validation.tolerance == 0.15
    assert [(pair.real, pair.sim) for pair in validation.pairs] == [("real-1pod", "sim-1pod")]


def profiles_with(**replay_changes: Any) -> dict[str, dict[str, Any]]:
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    sim = yaml.safe_load((REPO_ROOT / "config" / "sim.yaml").read_text())
    sim["replay"].update(replay_changes)
    return {"real": real, "sim": sim}


def test_real_and_sim_replay_the_baseline_pattern_alike() -> None:
    """P2.9's premise, on the committed profiles."""
    check_same_replay(profiles_with(), "baseline")


def test_a_pattern_that_differs_between_profiles_is_refused() -> None:
    profiles = profiles_with()
    profiles["sim"]["replay"]["patterns"]["baseline"]["runs"] = 41
    with pytest.raises(ExperimentError, match="sim"):
        check_same_replay(profiles, "baseline")


def test_a_pattern_one_profile_lacks_is_refused() -> None:
    profiles = profiles_with()
    del profiles["sim"]["replay"]["patterns"]["baseline"]
    with pytest.raises(ExperimentError, match="baseline"):
        check_same_replay(profiles, "baseline")


def test_shared_prefixes_that_differ_are_refused() -> None:
    profiles = profiles_with(shared_prefix_tokens={"all_types": 16, "by_type": {}})
    with pytest.raises(ExperimentError, match="shared_prefix_tokens"):
        check_same_replay(profiles, "baseline")


def test_a_call_deadline_that_differs_is_refused() -> None:
    """The deadline decides which calls fail, so it is part of the replay."""
    profiles = profiles_with()
    profiles["sim"]["agent"]["llm_timeout_seconds"] = 60
    with pytest.raises(ExperimentError, match="llm_timeout_seconds"):
        check_same_replay(profiles, "baseline")


def test_a_profile_is_deployed_when_it_changes_and_scaled_before_the_restart(
    tmp_path: Path,
) -> None:
    experiment = by_profile(tmp_path)
    cluster = FakeCluster()
    out = outputs(experiment, tmp_path)
    run_experiment(experiment, out, cluster, ticking_clock())
    stem = f"exp-{experiment.hash}"
    variant_steps = ["restart", "routing default", "config", "warm"]
    assert cluster.asked == [
        "check",
        "deploy real",
        "scale 1",
        *variant_steps,
        f"replay {stem}-real-1pod.jsonl",
        "jumps 101 102",
        "deploy sim",
        "scale 1",
        *variant_steps,
        f"replay {stem}-sim-1pod.jsonl",
        "jumps 103 104",
        "scale 2",
        *variant_steps,
        f"replay {stem}-sim-2pods.jsonl",
        "jumps 105 106",
        "scale profile",
    ]


def test_a_failed_variant_still_scales_the_profile_back(tmp_path: Path) -> None:
    experiment = by_profile(tmp_path)
    cluster = FakeCluster(fail_replay_of="sim-1pod")
    with pytest.raises(RuntimeError, match="replay broke"):
        run_experiment(experiment, outputs(experiment, tmp_path), cluster, ticking_clock())
    assert cluster.asked[-1] == "scale profile"


def test_a_deploy_that_breaks_does_not_scale_what_it_was_replacing(tmp_path: Path) -> None:
    class BreaksOnSim(FakeCluster):
        def deploy(self, profile: str) -> None:
            super().deploy(profile)
            if profile == "sim":
                raise RuntimeError("the deploy broke")

    experiment = by_profile(tmp_path)
    cluster = BreaksOnSim()
    with pytest.raises(RuntimeError, match="deploy broke"):
        run_experiment(experiment, outputs(experiment, tmp_path), cluster, ticking_clock())
    assert cluster.asked[-1] == "deploy sim"


def test_each_variants_meta_names_its_profile_replicas_and_stamp(tmp_path: Path) -> None:
    experiment = by_profile(tmp_path)
    _, meta = run_experiment(
        experiment, outputs(experiment, tmp_path), FakeCluster(), ticking_clock()
    )
    sim_sha256 = hashlib.sha256((REPO_ROOT / "config" / "sim.yaml").read_bytes()).hexdigest()
    assert meta["profile"] is None
    assert meta["profile_sha256"] is None
    sim = meta["variants"]["sim-1pod"]
    assert (sim["profile"], sim["replicas"], sim["profile_sha256"]) == ("sim", 1, sim_sha256)
    assert meta["variants"]["real-1pod"]["profile"] == "real"


def test_the_validation_table_is_an_output_and_a_leftover(tmp_path: Path) -> None:
    experiment = by_profile(tmp_path, validation=VALIDATION)
    out = outputs(experiment, tmp_path)
    assert out.validation == tmp_path / "sim" / f"validation-{experiment.hash}.csv"
    out.validation.parent.mkdir()
    out.validation.write_text("kept")
    with pytest.raises(ExperimentError, match=experiment.hash):
        refuse_leftovers(experiment, out)


def test_finish_writes_the_results_and_the_table_and_prints_both(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    experiment = by_profile(tmp_path, validation=VALIDATION)
    out = outputs(experiment, tmp_path)
    rows, meta = run_experiment(experiment, out, FakeCluster(), ticking_clock())
    finish(experiment, out, rows, meta)
    with out.validation.open() as file:
        assert len(list(csv.DictReader(file))) == 7
    assert out.csv.exists()
    assert out.graph.exists()
    printed = capsys.readouterr().out
    assert "runs finished" in printed
    assert "validation real-1pod vs sim-1pod: 7 of 7 metrics within 0.15" in printed


def test_finish_writes_no_table_without_validation(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    out = outputs(experiment, tmp_path)
    rows, meta = run_experiment(experiment, out, FakeCluster(), ticking_clock())
    finish(experiment, out, rows, meta)
    assert out.csv.exists()
    assert not out.validation.exists()


VALIDATE_SIM = REPO_ROOT / "experiments" / "validate-sim.yaml"


def test_validate_sim_replays_the_baseline_load_on_both_profiles_at_1_and_2_pods() -> None:
    """P2.9's file against the committed profiles: the decided load, each real variant next to its
    simulated one, and the card's tolerance."""
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    experiment = load_experiment(VALIDATE_SIM, load_replay_config(real["replay"]).patterns)
    assert experiment.trace.name == f"{APB_TRACE}.jsonl"
    assert experiment.pattern == Pattern(40, 0.02, None, None)
    assert experiment.seed == 6
    assert experiment.warmup == committed(BASELINE).warmup
    assert [(v.name, v.profile, v.replicas) for v in experiment.variants] == [
        ("real-1pod", "real", 1),
        ("sim-1pod", "sim", 1),
        ("real-2pods", "real", 2),
        ("sim-2pods", "sim", 2),
    ]
    assert experiment.validation is not None
    assert experiment.validation.tolerance == 0.15
    assert [(pair.real, pair.sim) for pair in experiment.validation.pairs] == [
        ("real-1pod", "sim-1pod"),
        ("real-2pods", "sim-2pods"),
    ]
    load_trace(experiment.trace)


# P3.7: the GPU log, the picker's log of a plugin variant, the not-worse tolerance, the burst files.

ROUTING_BURST = REPO_ROOT / "experiments" / "routing-burst.yaml"
ROUTING_BURST_REVERSED = REPO_ROOT / "experiments" / "routing-burst-reversed.yaml"
WHOLE_PLUGIN = {"plugin": True, "affinity": True, "overload_guard": True, "parallel_spill": True}


def test_a_real_profile_checks_the_gpu_first_and_logs_it_around_the_variants(
    tmp_path: Path,
) -> None:
    experiment = loaded(tmp_path)
    cluster = FakeCluster()
    run_experiment(experiment, outputs(experiment, tmp_path), cluster, ticking_clock())
    stem = f"exp-{experiment.hash}"
    # After the readiness check and before the first restart; stopped after the last replay.
    assert cluster.gpu == [
        ("gpu check", 1),
        (f"gpu log {stem}-gpu.csv", 1),
        ("gpu stop", len(cluster.asked)),
    ]
    assert cluster.asked[:2] == ["check", "restart"]
    assert cluster.asked[-1].startswith("jumps")


def test_a_sim_only_experiment_has_no_gpu_log(tmp_path: Path) -> None:
    experiment = loaded(tmp_path, profile="sim")
    cluster = FakeCluster()
    _, meta = run_experiment(experiment, outputs(experiment, tmp_path), cluster, ticking_clock())
    assert cluster.gpu == []
    assert meta["gpu_log"] is None


def test_a_gpu_that_cannot_be_read_stops_the_experiment_before_any_restart(
    tmp_path: Path,
) -> None:
    experiment = loaded(tmp_path)
    cluster = FakeCluster(fail_gpu_check=True)
    with pytest.raises(ExperimentError, match="nvidia-smi"):
        run_experiment(experiment, outputs(experiment, tmp_path), cluster, ticking_clock())
    assert cluster.asked == ["check"]
    assert cluster.gpu == [("gpu check", 1)]


def test_a_replay_that_breaks_still_stops_the_gpu_log_before_the_default_comes_back(
    tmp_path: Path,
) -> None:
    experiment = loaded(tmp_path)
    cluster = FakeCluster(fail_replay_of="round-robin")
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="replay broke"):
        run_experiment(experiment, out, cluster, ticking_clock())
    stops = [steps for event, steps in cluster.gpu if event == "gpu stop"]
    assert stops == [len(cluster.asked) - 1]
    assert cluster.asked[-1] == "routing default"


def test_the_meta_names_the_gpu_log_and_whether_nvidia_smi_ended_early(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    out = outputs(experiment, tmp_path)
    _, running = run_experiment(experiment, out, FakeCluster(), ticking_clock())
    _, ended = run_experiment(experiment, out, FakeCluster(gpu_exit=6), ticking_clock())
    assert running["gpu_log"] == out.gpu.name
    assert running["gpu_log_ended_early_with"] is None
    assert ended["gpu_log_ended_early_with"] == 6


def test_finish_says_when_the_gpu_log_has_a_gap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    experiment = loaded(tmp_path)
    out = outputs(experiment, tmp_path)
    rows, meta = run_experiment(experiment, out, FakeCluster(gpu_exit=6), ticking_clock())
    finish(experiment, out, rows, meta)
    printed = capsys.readouterr().out
    assert f"GPU log {out.gpu}" in printed
    assert "nvidia-smi ended early with exit 6, so the GPU log stops before the run did" in printed


def test_the_gpu_log_is_an_output_and_a_leftover(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    out = outputs(experiment, tmp_path)
    assert out.gpu == out.csv.with_name(f"exp-{experiment.hash}-gpu.csv")
    out.gpu.parent.mkdir(parents=True)
    out.gpu.write_text("")
    with pytest.raises(ExperimentError, match="exist"):
        refuse_leftovers(experiment, out)


def test_a_plugin_variants_picker_log_is_kept_before_the_next_switch(tmp_path: Path) -> None:
    """Switching back to the default restarts the picker, and its log goes with it."""
    experiment = loaded(tmp_path, variants=[ROUTER_VARIANT, DEFAULT_VARIANT])
    cluster = FakeCluster()
    out = outputs(experiment, tmp_path)
    _, meta = run_experiment(experiment, out, cluster, ticking_clock())
    picked = cluster.asked.index("picker log")
    assert cluster.asked[picked - 1].startswith("jumps")
    assert cluster.asked[picked + 1] == "restart"
    assert cluster.asked.count("picker log") == 1
    assert out.picker("run-router").read_text() == "the picker's log\n"
    assert meta["variants"]["run-router"]["picker_log"] == out.picker("run-router").name
    assert meta["variants"]["default"]["picker_log"] is None


def test_a_picker_log_is_kept_when_the_plugin_variant_is_last(tmp_path: Path) -> None:
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, ROUTER_VARIANT])
    cluster = FakeCluster()
    out = outputs(experiment, tmp_path)
    run_experiment(experiment, out, cluster, ticking_clock())
    assert cluster.asked[cluster.asked.index("picker log") + 1 :] == ["routing default"]
    assert out.picker("run-router").exists()


def test_the_tolerance_and_the_reference_variant_go_into_the_meta(tmp_path: Path) -> None:
    experiment = loaded(
        tmp_path, variants=[DEFAULT_VARIANT, ROUTER_VARIANT], not_worse_tolerance=0.05
    )
    assert experiment.not_worse_tolerance == 0.05
    assert experiment.reference_variant == "default"
    _, meta = run_experiment(
        experiment, outputs(experiment, tmp_path), FakeCluster(), ticking_clock()
    )
    assert meta["not_worse_tolerance"] == 0.05
    assert meta["reference_variant"] == "default"


def test_without_a_tolerance_the_meta_has_none(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    _, meta = run_experiment(
        experiment, outputs(experiment, tmp_path), FakeCluster(), ticking_clock()
    )
    assert meta["not_worse_tolerance"] is None
    assert meta["reference_variant"] is None


@pytest.mark.parametrize("tolerance", [-0.01, True, "0.05"])
def test_a_tolerance_that_is_not_a_number_at_or_over_zero_is_refused(
    tmp_path: Path, tolerance: object
) -> None:
    with pytest.raises(ExperimentError, match="not_worse_tolerance"):
        loaded(tmp_path, variants=[DEFAULT_VARIANT, ROUTER_VARIANT], not_worse_tolerance=tolerance)


def test_a_tolerance_with_no_default_variant_to_hold_to_is_refused(tmp_path: Path) -> None:
    other = {**ROUTER_VARIANT, "name": "other"}
    with pytest.raises(ExperimentError, match="one variant on the default picker"):
        loaded(tmp_path, variants=[ROUTER_VARIANT, other], not_worse_tolerance=0.05)


def test_a_tolerance_over_other_than_two_variants_is_refused(tmp_path: Path) -> None:
    other = {**ROUTER_VARIANT, "name": "other"}
    with pytest.raises(ExperimentError, match="two variants"):
        loaded(
            tmp_path,
            variants=[DEFAULT_VARIANT, ROUTER_VARIANT, other],
            not_worse_tolerance=0.05,
        )


def test_the_live_cluster_checks_the_gpu_with_the_fields_it_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment = loaded(tmp_path)
    handed: list[list[str]] = []

    def run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        handed.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="2026/11/12 12:00:00.000, 43\n")

    monkeypatch.setattr("load_generator.experiment.subprocess.run", run)
    LiveCluster(experiment, {}).check_gpu()
    ((args,),) = [handed]
    assert args[0] == "nvidia-smi"
    assert args[1] == f"--query-gpu={GPU_FIELDS}"


@pytest.mark.parametrize(("code", "stdout"), [(2, ""), (0, "")], ids=["exits 2", "prints nothing"])
def test_a_gpu_check_that_fails_or_prints_nothing_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int, stdout: str
) -> None:
    experiment = loaded(tmp_path)

    def run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, code, stdout=stdout)

    monkeypatch.setattr("load_generator.experiment.subprocess.run", run)
    with pytest.raises(ExperimentError, match="nvidia-smi"):
        LiveCluster(experiment, {}).check_gpu()


class FakeProcess:
    def __init__(self, args: list[str], exited: int | None) -> None:
        self.args = args
        self.exited = exited
        self.terminated = False

    def poll(self) -> int | None:
        return self.exited

    def terminate(self) -> None:
        self.terminated = True

    def wait(self) -> int:
        return 0


def test_the_live_gpu_log_samples_at_the_profiles_scrape_interval_into_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment = loaded(tmp_path)
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    started: list[FakeProcess] = []

    def popen(args: list[str]) -> FakeProcess:
        started.append(FakeProcess(args, None))
        return started[-1]

    monkeypatch.setattr("load_generator.experiment.subprocess.Popen", popen)
    cluster = LiveCluster(experiment, {"real": real})
    cluster.start_gpu_log(tmp_path / "gpu.csv")
    assert cluster.stop_gpu_log() is None
    (process,) = started
    interval = str(real["monitoring"]["scrape_interval_seconds"])
    assert process.args == [
        "nvidia-smi",
        f"--query-gpu={GPU_FIELDS}",
        "--format=csv",
        "-l",
        interval,
        "-f",
        str(tmp_path / "gpu.csv"),
    ]
    assert process.terminated


def test_a_live_gpu_log_that_ended_early_gives_its_exit_and_is_not_terminated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment = loaded(tmp_path)
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    started: list[FakeProcess] = []

    def popen(args: list[str]) -> FakeProcess:
        started.append(FakeProcess(args, 6))
        return started[-1]

    monkeypatch.setattr("load_generator.experiment.subprocess.Popen", popen)
    cluster = LiveCluster(experiment, {"real": real})
    cluster.start_gpu_log(tmp_path / "gpu.csv")
    assert cluster.stop_gpu_log() == 6
    assert not started[0].terminated


def test_the_live_picker_log_is_the_pickers_whole_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment = loaded(tmp_path)
    asked: list[tuple[str, ...]] = []

    def kubectl(*args: str) -> str:
        asked.append(args)
        return "lines\n"

    monkeypatch.setattr("load_generator.experiment.kubectl", kubectl)
    assert LiveCluster(experiment, {}).picker_log() == "lines\n"
    assert asked == [("logs", "deployment/vllm-pool-epp")]


@pytest.mark.parametrize("path", [ROUTING_BURST, ROUTING_BURST_REVERSED], ids=lambda p: p.stem)
def test_the_routing_burst_files_load_and_pass_the_routing_checks(path: Path) -> None:
    experiment = committed(path)
    profiles = {"real": yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())}
    check_routing(experiment, profiles)
    assert experiment.profile == "real"
    assert experiment.trace.name == f"{APB_TRACE}.jsonl"
    assert experiment.pattern_name == "burst-fan-out"
    assert experiment.seed == 6
    assert experiment.not_worse_tolerance == 0.05
    assert experiment.reference_variant == "default"
    plugin = next(variant for variant in experiment.variants if not variant.default)
    assert plugin.routing == WHOLE_PLUGIN
    assert plugin.epp_config == "thesis-plugins.yaml"


def test_routing_burst_discards_the_runs_before_the_first_burst() -> None:
    """They start on empty caches and empty queues, before the load the file is about."""
    experiment = committed(ROUTING_BURST)
    burst = experiment.pattern.burst
    assert burst is not None
    starts = start_times(experiment.pattern, experiment.seed)
    assert experiment.warmup.runs_discarded == sum(start < burst.every_seconds for start in starts)


def test_routing_burst_reversed_differs_only_in_the_order() -> None:
    """Otherwise a difference between the two could come from something other than the order."""
    forward = yaml.safe_load(ROUTING_BURST.read_text())
    backward = yaml.safe_load(ROUTING_BURST_REVERSED.read_text())
    assert [variant["name"] for variant in forward["variants"]] == ["default", "plugin"]
    assert backward["variants"] == list(reversed(forward["variants"]))
    del forward["variants"], backward["variants"]
    assert backward == forward
    assert (
        outputs(committed(ROUTING_BURST), REPO_ROOT).csv
        != outputs(committed(ROUTING_BURST_REVERSED), REPO_ROOT).csv
    )


# P3.7, after the first run: a vLLM pod that made no tokens while it had calls, a host stall that
# hit one variant and not the other, so the replay is refused rather than compared.

STALL = PodStall(pod="pod-a", start=150.0, seconds=65)


def test_with_pod_stall_seconds_each_replay_is_checked_after_its_logs(tmp_path: Path) -> None:
    experiment = loaded(tmp_path, variants=[ROUTER_VARIANT, DEFAULT_VARIANT], pod_stall_seconds=30)
    assert experiment.pod_stall_seconds == 30
    cluster = FakeCluster()
    _, meta = run_experiment(experiment, outputs(experiment, tmp_path), cluster, ticking_clock())
    checks = [step for step in cluster.asked if step.startswith("stalls")]
    # The replay's own clock readings, and the file's seconds.
    assert checks == ["stalls 101 102 30", "stalls 103 104 30"]
    first = cluster.asked.index(checks[0])
    assert cluster.asked[first - 2].startswith("jumps")
    assert cluster.asked[first - 1] == "picker log"
    assert cluster.asked[first + 1] == "restart"
    assert meta["pod_stall_seconds"] == 30


def test_without_pod_stall_seconds_no_replay_is_checked(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    assert experiment.pod_stall_seconds is None
    cluster = FakeCluster()
    _, meta = run_experiment(experiment, outputs(experiment, tmp_path), cluster, ticking_clock())
    assert not any(step.startswith("stalls") for step in cluster.asked)
    assert meta["pod_stall_seconds"] is None


def test_a_stall_stops_the_experiment_before_the_next_variant_and_says_where(
    tmp_path: Path,
) -> None:
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, ROUTER_VARIANT], pod_stall_seconds=30)
    cluster = FakeCluster(stalls={"default": [STALL]})
    out = outputs(experiment, tmp_path)
    handed: list[dict[str, Any]] = []
    with pytest.raises(ExperimentError) as stopped:
        run_experiment(
            experiment, out, cluster, ticking_clock(), lambda _, meta: handed.append(meta)
        )
    # started is 101 on the ticking clock, so the stall began 49 s into the replay.
    assert str(stopped.value) == (
        "variant default: pod-a made no tokens with calls running or waiting for 65 s from 49 s "
        "into the replay. Such stalls hit both pickers on 2026-11-12 (P3.7, experiments/log.md), "
        "so this replay is not comparable; its replay file stays, move this file's results away "
        "to run it again"
    )
    assert cluster.asked.count("restart") == 1
    assert handed == []
    assert out.replay("default").exists()
    stops = [steps for event, steps in cluster.gpu if event == "gpu stop"]
    assert stops == [len(cluster.asked)]


def test_a_stall_in_a_plugin_variant_keeps_its_picker_log_and_puts_the_default_back(
    tmp_path: Path,
) -> None:
    experiment = loaded(tmp_path, variants=[ROUTER_VARIANT, DEFAULT_VARIANT], pod_stall_seconds=30)
    cluster = FakeCluster(stalls={"router": [STALL, PodStall("pod-b", 160.0, 30)]})
    out = outputs(experiment, tmp_path)
    with pytest.raises(ExperimentError, match=r"pod-a .* 65 s from 49 s.*pod-b .* 30 s from 59 s"):
        run_experiment(experiment, out, cluster, ticking_clock())
    assert out.picker("run-router").exists()
    assert cluster.asked[-1] == "routing default"
    assert cluster.asked.count("restart") == 1


@pytest.mark.parametrize("seconds", [0, -5, True, 2.5, "30"])
def test_pod_stall_seconds_that_are_not_a_whole_number_over_zero_are_refused(
    tmp_path: Path, seconds: object
) -> None:
    with pytest.raises(ExperimentError, match="pod_stall_seconds"):
        loaded(tmp_path, pod_stall_seconds=seconds)


def test_a_stall_window_under_two_scrapes_is_refused_before_the_cluster_is_touched(
    tmp_path: Path,
) -> None:
    """Prometheus needs two samples in the window to tell how many tokens came."""
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    interval = real["monitoring"]["scrape_interval_seconds"]
    check_stall_window(loaded(tmp_path, pod_stall_seconds=2 * interval), {"real": real})
    with pytest.raises(ExperimentError, match="pod_stall_seconds"):
        check_stall_window(loaded(tmp_path, pod_stall_seconds=2 * interval - 1), {"real": real})
    check_stall_window(loaded(tmp_path), {"real": real})


def series(pod: str, values: Mapping[float, float]) -> dict[str, Any]:
    """One pod's series in a Prometheus range answer, whose values are text."""
    return {"metric": {"pod": pod}, "values": [[t, str(v)] for t, v in values.items()]}


def test_a_stall_is_a_run_of_windows_with_calls_and_no_tokens() -> None:
    times = [100.0 + 5 * k for k in range(8)]
    busy = [series("pod-a", dict.fromkeys(times, 2.0)), series("pod-b", dict.fromkeys(times, 0.0))]
    tokens = [
        # pod-a: no tokens in the windows ending at 110 to 120, and again at 135.
        series("pod-a", {t: 0.0 if t in (110.0, 115.0, 120.0, 135.0) else 40.0 for t in times}),
        # pod-b made none, but it had no calls: idle, not stalled.
        series("pod-b", dict.fromkeys(times, 0.0)),
    ]
    assert pod_stalls_from(busy, tokens, window=30, step=5) == [
        # Each window reaches 30 s back from its end.
        PodStall("pod-a", start=80.0, seconds=40),
        PodStall("pod-a", start=105.0, seconds=30),
    ]


def test_a_window_with_no_token_reading_is_not_a_stall() -> None:
    busy = [series("pod-a", {100.0: 3.0, 105.0: 3.0})]
    tokens = [series("pod-a", {100.0: 50.0})]
    assert pod_stalls_from(busy, tokens, window=30, step=5) == []


def test_not_a_number_is_not_a_stall() -> None:
    busy = [{"metric": {"pod": "pod-a"}, "values": [[100.0, "NaN"]]}]
    tokens = [series("pod-a", {100.0: 0.0})]
    assert pod_stalls_from(busy, tokens, window=30, step=5) == []


def prometheus_answer(result: list[dict[str, Any]]) -> str:
    return json.dumps({"status": "success", "data": {"resultType": "matrix", "result": result}})


def test_the_live_cluster_asks_prometheus_through_the_api_server_for_the_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment = loaded(tmp_path, pod_stall_seconds=30)
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    asked: list[tuple[str, ...]] = []
    answers = [
        prometheus_answer([series("pod-a", {200.0: 1.0, 205.0: 1.0})]),
        prometheus_answer([series("pod-a", {200.0: 0.0, 205.0: 7.0})]),
    ]

    def kubectl(*args: str) -> str:
        asked.append(args)
        return answers[len(asked) - 1]

    monkeypatch.setattr("load_generator.experiment.kubectl", kubectl)
    found = LiveCluster(experiment, {"real": real}).pod_stalls(170.5, 400.25, 30)
    assert found == [PodStall("pod-a", start=170.0, seconds=30)]
    assert [args[:2] for args in asked] == [("get", "--raw"), ("get", "--raw")]
    paths = [urllib.parse.urlsplit(args[2]) for args in asked]
    assert {path.path for path in paths} == {
        "/api/v1/namespaces/monitoring/services/monitoring-kube-prometheus-prometheus:9090"
        "/proxy/api/v1/query_range"
    }
    busy, made = (urllib.parse.parse_qs(path.query) for path in paths)
    interval = str(real["monitoring"]["scrape_interval_seconds"])
    for query in (busy, made):
        assert query["start"] == ["170.500"]
        assert query["end"] == ["400.250"]
        assert query["step"] == [interval]
    assert busy["query"] == [
        "sum by (pod) (min_over_time(vllm:num_requests_running[30s]) "
        "+ min_over_time(vllm:num_requests_waiting[30s]))"
    ]
    assert made["query"] == [
        "sum by (pod) (increase(vllm:prompt_tokens_total[30s]) "
        "+ increase(vllm:generation_tokens_total[30s]))"
    ]


@pytest.mark.parametrize(
    "answer",
    [
        json.dumps({"status": "error", "error": "bad query"}),
        prometheus_answer([]),
    ],
    ids=["an error", "no pods"],
)
def test_a_prometheus_answer_with_no_pods_is_an_error_not_no_stalls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    experiment = loaded(tmp_path, pod_stall_seconds=30)
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    monkeypatch.setattr("load_generator.experiment.kubectl", lambda *_: answer)
    with pytest.raises(ExperimentError, match="Prometheus"):
        LiveCluster(experiment, {"real": real}).pod_stalls(170.0, 400.0, 30)


@pytest.mark.parametrize("path", [ROUTING_BURST, ROUTING_BURST_REVERSED], ids=lambda p: p.stem)
def test_the_routing_burst_files_check_each_replay_for_a_stalled_pod(path: Path) -> None:
    experiment = committed(path)
    profiles = {"real": yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())}
    assert experiment.pod_stall_seconds == 30
    check_stall_window(experiment, profiles)


def test_main_refuses_a_stall_window_under_two_scrapes_before_the_cluster_is_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    profile["replay"]["patterns"]["baseline"] = {"runs": 2, "rate_per_second": 0.02}
    interval = profile["monitoring"]["scrape_interval_seconds"]
    path = experiment_file(tmp_path, pod_stall_seconds=2 * interval - 1)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "real.yaml").write_text(yaml.safe_dump(profile))
    monkeypatch.setattr("load_generator.experiment.REPO_ROOT", tmp_path)

    def no_cluster(_experiment: Experiment, _profiles: Any) -> FakeCluster:
        raise AssertionError("the cluster was touched")

    monkeypatch.setattr("load_generator.experiment.LiveCluster", no_cluster)
    monkeypatch.setattr("sys.argv", ["experiment", str(path)])
    with pytest.raises(ExperimentError, match="pod_stall_seconds"):
        main()


def test_a_pod_with_calls_and_no_token_count_is_an_error_not_no_stall() -> None:
    """A renamed counter, or a pod whose tokens Prometheus did not read, would otherwise pass every
    replay as stall-free."""
    busy = [series("pod-a", {100.0: 2.0}), series("pod-b", {100.0: 2.0})]
    tokens = [series("pod-a", {100.0: 40.0})]
    with pytest.raises(ExperimentError, match="pod-b"):
        pod_stalls_from(busy, tokens, window=30, step=5)


def test_a_live_token_answer_with_no_pods_is_an_error_not_no_stalls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment = loaded(tmp_path, pod_stall_seconds=30)
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    answers = [prometheus_answer([series("pod-a", {200.0: 1.0})]), prometheus_answer([])]
    asked: list[tuple[str, ...]] = []

    def kubectl(*args: str) -> str:
        asked.append(args)
        return answers[len(asked) - 1]

    monkeypatch.setattr("load_generator.experiment.kubectl", kubectl)
    with pytest.raises(ExperimentError, match="pod-a"):
        LiveCluster(experiment, {"real": real}).pod_stalls(170.0, 400.0, 30)


# P3.7b: the least-work rule, and the files that hold the plugin with it to faster runs.


@pytest.mark.parametrize(
    ("profile_rules", "variant_rules"),
    [
        ({}, {"plugin": True, "least_work_first": True}),
        ({"plugin": True, "least_work_first": True}, {"priority": False}),
        ({"plugin": True, "affinity": True, "least_work_first": True}, {"affinity": False}),
    ],
)
def test_least_work_first_with_affinity_off_is_refused(
    tmp_path: Path, profile_rules: dict[str, bool], variant_rules: dict[str, bool]
) -> None:
    """The least-work rule adds up the work of pinned runs, and only affinity keeps pins: with
    affinity off it would do nothing, and the variant would read as its result."""
    variant = {**ROUTER_VARIANT, "routing": variant_rules}
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, variant])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    real["routing"].update(profile_rules)
    with pytest.raises(ExperimentError, match="least_work_first on, but affinity is off"):
        check_routing(experiment, {"real": real})


def test_least_work_first_with_affinity_on_is_accepted(tmp_path: Path) -> None:
    variant = {
        **ROUTER_VARIANT,
        "routing": {"plugin": True, "affinity": True, "least_work_first": True},
    }
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, variant])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    check_routing(experiment, {"real": real})


def test_a_variant_may_not_set_the_profiles_least_work_slack(tmp_path: Path) -> None:
    """The slack is the profile's, as the parallel threshold is (P3.7b). True passes the loader's
    check that rules are true or false, so the refusal is check_routing's."""
    variant = {**ROUTER_VARIANT, "routing": {"plugin": True, "least_work_slack": True}}
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, variant])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    with pytest.raises(ExperimentError, match="least_work_slack"):
        check_routing(experiment, {"real": real})


def test_a_variant_may_not_set_the_profiles_priority_aging(tmp_path: Path) -> None:
    """The aging is the profile's, as the least-work slack is (P4.1)."""
    variant = {**ROUTER_VARIANT, "routing": {"plugin": True, "priority_aging_seconds": True}}
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, variant])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    with pytest.raises(ExperimentError, match="priority_aging_seconds"):
        check_routing(experiment, {"real": real})


@pytest.mark.parametrize("key", ["warmup_seconds", "warmup_queue_threshold"])
def test_a_variant_may_not_set_the_profiles_warmup_limits(tmp_path: Path, key: str) -> None:
    """The warm-up limits are the profile's, as the priority aging is (P4.2)."""
    variant = {**ROUTER_VARIANT, "routing": {"plugin": True, key: True}}
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, variant])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    with pytest.raises(ExperimentError, match=f"variant run-router: {key}: the profile's"):
        check_routing(experiment, {"real": real})


def test_warmup_needs_no_affinity(tmp_path: Path) -> None:
    """Warm-up judges a run's first call by its x-step, with or without pins (P4.2)."""
    variant = {**ROUTER_VARIANT, "routing": {"plugin": True, "warmup": True}}
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, variant])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    check_routing(experiment, {"real": real})


def test_priority_on_a_profile_whose_pods_run_priority_scheduling_is_accepted(
    tmp_path: Path,
) -> None:
    variant = {**ROUTER_VARIANT, "routing": {"plugin": True, "priority": True}}
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, variant])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    assert real["model"]["scheduling_policy"] == "priority"
    check_routing(experiment, {"real": real})


@pytest.mark.parametrize("policy", ["fcfs", None])
def test_priority_on_a_profile_without_priority_scheduling_is_refused(
    tmp_path: Path, policy: str | None
) -> None:
    """vLLM answers 400 to a priority other than 0 under fcfs, so every call of the variant would
    fail; infra/routing-values.sh refuses it too, but only at the variant's switch (P4.1)."""
    variant = {**ROUTER_VARIANT, "routing": {"plugin": True, "priority": True}}
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, variant])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    if policy is None:
        del real["model"]["scheduling_policy"]
    else:
        real["model"]["scheduling_policy"] = policy
    with pytest.raises(ExperimentError, match="scheduling_policy"):
        check_routing(experiment, {"real": real})


def test_faster_by_and_the_reference_variant_go_into_the_meta(tmp_path: Path) -> None:
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, ROUTER_VARIANT], faster_by=0.05)
    assert experiment.faster_by == 0.05
    assert experiment.not_worse_tolerance is None
    assert experiment.reference_variant == "default"
    _, meta = run_experiment(
        experiment, outputs(experiment, tmp_path), FakeCluster(), ticking_clock()
    )
    assert meta["faster_by"] == 0.05
    assert meta["reference_variant"] == "default"


def test_without_faster_by_the_meta_has_none(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    _, meta = run_experiment(
        experiment, outputs(experiment, tmp_path), FakeCluster(), ticking_clock()
    )
    assert meta["faster_by"] is None


@pytest.mark.parametrize("share", [-0.01, 1, 1.5, True, "0.05"])
def test_faster_by_that_is_not_a_share_under_one_is_refused(tmp_path: Path, share: object) -> None:
    with pytest.raises(ExperimentError, match="faster_by"):
        loaded(tmp_path, variants=[DEFAULT_VARIANT, ROUTER_VARIANT], faster_by=share)


def test_faster_by_with_no_default_variant_to_hold_to_is_refused(tmp_path: Path) -> None:
    other = {**ROUTER_VARIANT, "name": "other"}
    with pytest.raises(ExperimentError, match="faster_by needs one variant on the default picker"):
        loaded(tmp_path, variants=[ROUTER_VARIANT, other], faster_by=0.05)


def test_faster_by_over_other_than_two_variants_is_refused(tmp_path: Path) -> None:
    other = {**ROUTER_VARIANT, "name": "other"}
    with pytest.raises(ExperimentError, match="faster_by compares two variants"):
        loaded(tmp_path, variants=[DEFAULT_VARIANT, ROUTER_VARIANT, other], faster_by=0.05)


ROUTING_FASTER = REPO_ROOT / "experiments" / "routing-faster.yaml"
ROUTING_FASTER_REVERSED = REPO_ROOT / "experiments" / "routing-faster-reversed.yaml"


@pytest.mark.parametrize(
    "path", [ROUTING_FASTER, ROUTING_FASTER_REVERSED], ids=lambda path: path.stem
)
def test_the_routing_faster_files_are_routing_burst_with_the_least_work_rule(path: Path) -> None:
    """The same replay, checks and variants as P3.7's, so the only change is the rule; the claim is
    faster runs, by 5% of default's p95 averaged over both orders."""
    experiment = committed(path)
    profiles = {"real": yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())}
    check_routing(experiment, profiles)
    check_stall_window(experiment, profiles)
    assert experiment.faster_by == 0.05
    assert experiment.reference_variant == "default"
    faster = yaml.safe_load(path.read_text())
    burst = yaml.safe_load(
        (ROUTING_BURST if path == ROUTING_FASTER else ROUTING_BURST_REVERSED).read_text()
    )
    plugin = next(variant for variant in faster["variants"] if variant["name"] == "plugin")
    assert plugin["routing"] == {**WHOLE_PLUGIN, "least_work_first": True}
    plugin["routing"] = WHOLE_PLUGIN
    assert faster.pop("faster_by") == 0.05
    assert faster == burst


def test_routing_faster_reversed_differs_only_in_the_order() -> None:
    forward = yaml.safe_load(ROUTING_FASTER.read_text())
    backward = yaml.safe_load(ROUTING_FASTER_REVERSED.read_text())
    assert [variant["name"] for variant in forward["variants"]] == ["default", "plugin"]
    assert backward["variants"] == list(reversed(forward["variants"]))


# P3.7b's second try: the least-context rule.


@pytest.mark.parametrize(
    ("profile_rules", "variant_rules", "says"),
    [
        ({}, {"plugin": True, "least_context_first": True}, "least_context_first on, but affinity"),
        (
            {},
            {
                "plugin": True,
                "affinity": True,
                "least_work_first": True,
                "least_context_first": True,
            },
            "least_work_first and least_context_first",
        ),
    ],
)
def test_least_context_first_without_affinity_or_beside_least_work_is_refused(
    tmp_path: Path, profile_rules: dict[str, bool], variant_rules: dict[str, bool], says: str
) -> None:
    variant = {**ROUTER_VARIANT, "routing": variant_rules}
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, variant])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    real["routing"].update(profile_rules)
    with pytest.raises(ExperimentError, match=says):
        check_routing(experiment, {"real": real})


def test_least_context_first_with_affinity_on_is_accepted(tmp_path: Path) -> None:
    variant = {
        **ROUTER_VARIANT,
        "routing": {"plugin": True, "affinity": True, "least_context_first": True},
    }
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, variant])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    check_routing(experiment, {"real": real})


ROUTING_CONTEXT = REPO_ROOT / "experiments" / "routing-context.yaml"
ROUTING_CONTEXT_REVERSED = REPO_ROOT / "experiments" / "routing-context-reversed.yaml"


@pytest.mark.parametrize(
    "path", [ROUTING_CONTEXT, ROUTING_CONTEXT_REVERSED], ids=lambda path: path.stem
)
def test_the_routing_context_files_are_routing_faster_with_the_other_first_call_rule(
    path: Path,
) -> None:
    """The same replay, checks, claim and variants as routing-faster's, so the only change is
    which first-call rule is on."""
    experiment = committed(path)
    profiles = {"real": yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())}
    check_routing(experiment, profiles)
    check_stall_window(experiment, profiles)
    assert experiment.faster_by == 0.05
    context = yaml.safe_load(path.read_text())
    faster = yaml.safe_load(
        (ROUTING_FASTER if path == ROUTING_CONTEXT else ROUTING_FASTER_REVERSED).read_text()
    )
    plugin = next(variant for variant in context["variants"] if variant["name"] == "plugin")
    assert plugin["routing"] == {**WHOLE_PLUGIN, "least_context_first": True}
    plugin["routing"] = {**WHOLE_PLUGIN, "least_work_first": True}
    assert context == faster


# P3.7c: both vLLM pods' logs, kept per variant, so a pod stall comes with the engine's own numbers.


def test_each_real_variant_keeps_both_vllm_pods_logs_after_its_replay(tmp_path: Path) -> None:
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, ROUTER_VARIANT])
    cluster = FakeCluster()
    out = outputs(experiment, tmp_path)
    _, meta = run_experiment(experiment, out, cluster, ticking_clock())
    # One take per variant, each after that variant's replay and before the next restart wipes
    # its pods.
    replays = [index for index, step in enumerate(cluster.asked) if step.startswith("replay")]
    restarts = [index for index, step in enumerate(cluster.asked) if step == "restart"]
    takes = [steps for _, steps in cluster.vllm]
    assert len(takes) == 2
    assert replays[0] < takes[0] <= restarts[1] < replays[1] < takes[1]
    for variant in ("default", "run-router"):
        names = meta["variants"][variant]["vllm_logs"]
        assert names == [
            out.vllm(variant, "vllm-x-pod-a").name,
            out.vllm(variant, "vllm-x-pod-b").name,
        ]
        assert out.vllm(variant, "vllm-x-pod-a").read_text() == "pod a's log\n"
        assert out.vllm(variant, "vllm-x-pod-b").read_text() == "pod b's log\n"


def test_a_stalled_replay_keeps_its_vllm_logs(tmp_path: Path) -> None:
    experiment = loaded(tmp_path, variants=[DEFAULT_VARIANT, ROUTER_VARIANT], pod_stall_seconds=30)
    cluster = FakeCluster(stalls={"default": [STALL]})
    out = outputs(experiment, tmp_path)
    with pytest.raises(ExperimentError):
        run_experiment(experiment, out, cluster, ticking_clock())
    assert out.vllm("default", "vllm-x-pod-a").exists()
    assert out.vllm("default", "vllm-x-pod-b").exists()
    ((variant, steps),) = cluster.vllm
    assert variant == "default"
    assert cluster.asked[steps].startswith("stalls")


def test_a_sim_variant_keeps_no_vllm_logs(tmp_path: Path) -> None:
    """Simulated pods share no GPU, which is what the logs are kept for."""
    experiment = loaded(tmp_path, profile="sim")
    cluster = FakeCluster()
    _, meta = run_experiment(experiment, outputs(experiment, tmp_path), cluster, ticking_clock())
    assert cluster.vllm == []
    assert all(variant["vllm_logs"] is None for variant in meta["variants"].values())


def test_the_live_cluster_takes_each_vllm_pods_log_with_timestamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment = loaded(tmp_path)
    asked: list[tuple[str, ...]] = []

    def kubectl(*args: str) -> str:
        asked.append(args)
        if args[:2] == ("get", "pods"):
            return "vllm-x-pod-a vllm-x-pod-b"
        return f"log of {args[-1]}\n"

    monkeypatch.setattr("load_generator.experiment.kubectl", kubectl)
    assert LiveCluster(experiment, {}).vllm_logs() == {
        "vllm-x-pod-a": "log of vllm-x-pod-a\n",
        "vllm-x-pod-b": "log of vllm-x-pod-b\n",
    }
    assert asked[0][:4] == ("get", "pods", "-l", "app.kubernetes.io/name=vllm")
    assert asked[1:] == [
        ("logs", "--timestamps", "vllm-x-pod-a"),
        ("logs", "--timestamps", "vllm-x-pod-b"),
    ]


# P3.9b: a variant scaled by KEDA on remaining work.
AUTOSCALED_VARIANT = {
    "name": "remaining",
    "values": None,
    "epp_config": "default-plugins.yaml",
    "autoscaler": "remaining",
}
AUTOSCALE_REMAINING = REPO_ROOT / "experiments" / "autoscale-remaining.yaml"


def test_a_variant_may_be_scaled_by_an_autoscaler(tmp_path: Path) -> None:
    experiment = loaded(tmp_path, variants=[AUTOSCALED_VARIANT, DEFAULT_VARIANT])
    assert [variant.autoscaler for variant in experiment.variants] == ["remaining", None]


def test_an_autoscaler_with_no_object_is_refused(tmp_path: Path) -> None:
    """P3.10 made cpu a kind, so an unknown name is now memory (spec change, experiments/log.md)."""
    with pytest.raises(ExperimentError, match="memory"):
        loaded(tmp_path, variants=[{**AUTOSCALED_VARIANT, "autoscaler": "memory"}])


def test_an_autoscaler_with_a_replica_count_of_its_own_is_refused(tmp_path: Path) -> None:
    """It starts at the profile's min_replicas; a count of its own would say otherwise."""
    with pytest.raises(ExperimentError, match="replicas"):
        loaded(tmp_path, variants=[{**AUTOSCALED_VARIANT, "replicas": 2}])


def test_an_autoscaler_on_a_profile_without_autoscaling_is_refused(tmp_path: Path) -> None:
    experiment = loaded(tmp_path, variants=[AUTOSCALED_VARIANT])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    del real["autoscaling"]
    with pytest.raises(ExperimentError, match="autoscaling"):
        check_autoscaling(experiment, {"real": real})


def test_an_autoscaler_on_a_profile_whose_section_could_not_scale_is_refused(
    tmp_path: Path,
) -> None:
    experiment = loaded(tmp_path, variants=[AUTOSCALED_VARIANT])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    real["autoscaling"]["max_replicas"] = real["autoscaling"]["min_replicas"]
    with pytest.raises(ExperimentError, match="max_replicas"):
        check_autoscaling(experiment, {"real": real})


def test_without_an_autoscaler_a_profile_needs_no_section(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    demo = yaml.safe_load((REPO_ROOT / "config" / "demo.yaml").read_text())
    check_autoscaling(experiment, {"real": demo})


def test_main_refuses_an_autoscaler_the_profile_has_no_section_for_before_the_cluster_is_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = experiment_file(tmp_path, variants=[AUTOSCALED_VARIANT])
    profile = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    profile["replay"]["patterns"]["baseline"] = {"runs": 2, "rate_per_second": 0.02}
    del profile["autoscaling"]
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "real.yaml").write_text(yaml.safe_dump(profile))
    monkeypatch.setattr("load_generator.experiment.REPO_ROOT", tmp_path)

    def no_cluster(_experiment: Experiment, _profiles: Any) -> FakeCluster:
        raise AssertionError("the cluster was touched")

    monkeypatch.setattr("load_generator.experiment.LiveCluster", no_cluster)
    monkeypatch.setattr("sys.argv", ["experiment", str(path)])
    with pytest.raises(ExperimentError, match="autoscaling"):
        main()


def test_an_autoscaled_variant_starts_at_the_floor_before_its_restart_and_ends_scaled_back(
    tmp_path: Path,
) -> None:
    """The floor before the restart, so its pods are the ones warmed; the autoscaler after the
    warm-up, whose calls go straight to the pods and scaled a CPU HPA before the replay on
    2026-11-17 (P3.10's review); and it is gone before the count is restored, which it would
    otherwise undo."""
    experiment = loaded(tmp_path, variants=[AUTOSCALED_VARIANT])
    cluster = FakeCluster()
    run_experiment(experiment, outputs(experiment, tmp_path), cluster, ticking_clock())
    stem = f"exp-{experiment.hash}"
    assert cluster.asked == [
        "check",
        "floor remaining",
        "restart",
        "routing default",
        "config",
        "warm",
        "autoscaler on",
        f"replay {stem}-remaining.jsonl",
        "jumps 101 102",
        "autoscale series 101 102",
        "autoscaler off",
        "scale profile",
    ]


def test_the_variant_after_an_autoscaled_one_starts_at_the_profiles_count(tmp_path: Path) -> None:
    experiment = loaded(tmp_path, variants=[AUTOSCALED_VARIANT, DEFAULT_VARIANT])
    cluster = FakeCluster()
    run_experiment(experiment, outputs(experiment, tmp_path), cluster, ticking_clock())
    second_restart = len(cluster.asked) - 1 - cluster.asked[::-1].index("restart")
    assert cluster.asked[second_restart - 2 : second_restart] == ["autoscaler off", "scale profile"]
    assert cluster.asked.count("autoscaler off") == 1
    assert cluster.asked.count("scale profile") == 1


def test_a_replay_that_breaks_still_removes_the_autoscaler_and_scales_back(tmp_path: Path) -> None:
    experiment = loaded(tmp_path, variants=[AUTOSCALED_VARIANT])
    cluster = FakeCluster(fail_replay_of="remaining")
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="replay broke"):
        run_experiment(experiment, out, cluster, ticking_clock())
    assert cluster.asked[-2:] == ["autoscaler off", "scale profile"]


def test_a_start_that_breaks_halfway_is_removed_and_scaled_back_too(tmp_path: Path) -> None:
    """It may have scaled the pods, or installed the object, before it failed."""
    experiment = loaded(tmp_path, variants=[AUTOSCALED_VARIANT])
    cluster = FakeCluster(fail_autoscaler_start=True)
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="autoscaler start broke"):
        run_experiment(experiment, out, cluster, ticking_clock())
    assert cluster.asked[-3:] == ["autoscaler on", "autoscaler off", "scale profile"]


def test_a_variant_without_an_autoscaler_never_touches_one(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    cluster = FakeCluster()
    run_experiment(experiment, outputs(experiment, tmp_path), cluster, ticking_clock())
    assert not any(step.startswith("autoscale") for step in cluster.asked)


def test_an_autoscaled_variant_writes_its_series_and_the_meta_holds_the_crossings(
    tmp_path: Path,
) -> None:
    experiment = loaded(tmp_path, variants=[AUTOSCALED_VARIANT, DEFAULT_VARIANT])
    out = outputs(experiment, tmp_path)
    _, meta = run_experiment(experiment, out, FakeCluster(), ticking_clock())
    assert out.autoscale("remaining") == out.csv.with_name(
        f"exp-{experiment.hash}-remaining-autoscale.csv"
    )
    with out.autoscale("remaining").open() as file:
        rows = list(csv.DictReader(file))
    assert [row["seconds"] for row in rows] == ["0.0", "5.0", "10.0", "15.0"]
    assert meta["variants"]["remaining"]["autoscale"] == {
        "autoscaler": "remaining",
        "section": AUTOSCALING,
        "csv": out.autoscale("remaining").name,
        "crossings": {
            "remaining_seconds": 5.0,
            "scale_up_seconds": 10.0,
            "queue_seconds": 15.0,
            "scaled_before_replay": False,
        },
        "scale_up_before_queue": True,
        # P4.12 (a spec change): the threshold from the profile, and the windows of its calls.
        "context_threshold": REAL_CONTEXT_THRESHOLD,
        "scale_up_windows": scale_up_windows(
            AUTOSCALE_ROWS, replay_lines(), 101.0, 1, AUTOSCALING, REAL_CONTEXT_THRESHOLD
        ).as_meta(),
    }
    assert meta["variants"]["default"]["autoscale"] is None
    assert not out.autoscale("default").exists()


def test_the_report_gives_an_autoscaled_variants_crossings(tmp_path: Path) -> None:
    experiment = loaded(tmp_path, variants=[AUTOSCALED_VARIANT])
    rows, meta = run_experiment(
        experiment, outputs(experiment, tmp_path), FakeCluster(), ticking_clock()
    )
    assert crossing_text("remaining", AUTOSCALING, Crossings(5.0, 10.0, 15.0)) in report_lines(
        rows, meta
    )


def later_step(run: str, step: int, wall_ms: int, ttft_ms: int) -> dict[str, Any]:
    """A call ts_start'ed wall_ms after the replay's start, 101 s on the ticking clock."""
    return {
        **replay_lines()[0],
        "run_id": run,
        "step": step,
        "ts_start": 101_000 + wall_ms,
        "ttft_ms": ttft_ms,
    }


def test_every_autoscaled_variant_reports_its_windows_from_the_kept_runs_by_the_wall(
    tmp_path: Path,
) -> None:
    """P4.12, for every kind so that P4.5 compares all four alike: run 1 is discarded; the
    windows count from the replay's start; the fake's spec rises at 10 s, and its second pod is
    never Ready. The last call is sent at the rise, so a window counted from another clock would
    move it."""
    experiment = loaded(tmp_path, variants=[AUTOSCALED_VARIANT])
    lines = [
        later_step("a-1", 1, 0, 50),
        later_step("a-1", 2, 1_000, 50),
        later_step("a-2", 1, 500, 50),
        later_step("a-2", 2, 2_000, 300),
        later_step("a-2", 3, 9_000, 400),
        later_step("a-2", 4, 10_000, 900),
    ]
    cluster = FakeCluster(replays={"remaining": lines})
    rows, meta = run_experiment(experiment, outputs(experiment, tmp_path), cluster, ticking_clock())
    windows = ScaleUpWindows.from_meta(
        meta["variants"]["remaining"]["autoscale"]["scale_up_windows"]
    )
    assert windows == ScaleUpWindows(
        context_seconds=5.0,
        scale_up_seconds=10.0,
        ready_seconds=None,
        scale_down_seconds=None,
        scaled_before_replay=False,
        before=Window(2, 300),
        rising=Window(1, 900),
        after=Window(0, None),
        restarts_during_load=0,
        restarts=1,
    )
    assert scale_up_text(REAL_CONTEXT_THRESHOLD, windows) in report_lines(rows, meta)


def test_the_live_cluster_asks_prometheus_for_the_four_series_at_the_scrape_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment = loaded(tmp_path, variants=[AUTOSCALED_VARIANT])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    asked: list[str] = []

    def kubectl(*args: str) -> str:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(args[2]).query)
        assert query["start"] == ["100.000"]
        assert query["end"] == ["110.000"]
        assert query["step"] == [str(real["monitoring"]["scrape_interval_seconds"])]
        asked.append(query["query"][0])
        return prometheus_answer([{"metric": {}, "values": [[100.0, "1"], [105.0, "2"]]}])

    monkeypatch.setattr("load_generator.experiment.kubectl", kubectl)
    rows = LiveCluster(experiment, {"real": real}).autoscale_series(100.0, 110.0)
    assert rows == [AutoscaleRow(0.0, 1, 1, 1, 1, 1, 1), AutoscaleRow(5.0, 2, 2, 2, 2, 2, 2)]
    # P4.12 added the last two (spec change).
    assert asked == [
        'max(kube_deployment_spec_replicas{namespace="default",deployment="vllm"})',
        'max(kube_deployment_status_replicas_ready{namespace="default",deployment="vllm"})',
        "sum(vllm:num_requests_waiting)",
        "sum(thesis_remaining_work)",
        "sum(thesis_context_tokens)",
        'sum(kube_pod_container_status_restarts_total{namespace="keda",container="keda-operator"})',
    ]


def test_the_live_cluster_scales_to_the_floor_then_installs_the_profiles_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment = loaded(tmp_path, variants=[AUTOSCALED_VARIANT])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    steps: list[Any] = []

    def run(args: list[str], input: str, text: bool, check: bool) -> None:
        assert text and check
        steps.append((args, yaml.safe_load(input)))

    monkeypatch.setattr("load_generator.experiment.subprocess.run", run)
    monkeypatch.setattr("load_generator.experiment.kubectl", lambda *args: steps.append(args))
    cluster = LiveCluster(experiment, {"real": real})
    monkeypatch.setattr(cluster, "scale", lambda replicas: steps.append(("scale", replicas)))
    section = cluster.floor_autoscaler("remaining")
    assert section == real["autoscaling"]
    assert steps == [("scale", real["autoscaling"]["min_replicas"])]
    cluster.start_autoscaler()
    args, values = steps[1]
    assert args[:4] == ["helm", "upgrade", "--install", "autoscaling"]
    assert args[4] == str(REPO_ROOT / "infra" / "charts" / "autoscaling")
    assert values["query"] == "sum(thesis_remaining_work)"
    assert values["threshold"] == real["autoscaling"]["remaining_work_target"]
    assert steps[2][:3] == ("wait", "scaledobject/vllm", "--for=condition=Ready")


def test_the_live_cluster_installs_the_pressure_object_at_one_pods_kv_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment = loaded(tmp_path, variants=[PRESSURE_VARIANT])
    real = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())
    real["autoscaling"]["pressure_target"] = 0.75
    installed: list[Any] = []

    def run(args: list[str], input: str, text: bool, check: bool) -> None:
        installed.append(yaml.safe_load(input))

    monkeypatch.setattr("load_generator.experiment.subprocess.run", run)
    monkeypatch.setattr("load_generator.experiment.kubectl", lambda *args: "")
    cluster = LiveCluster(experiment, {"real": real})
    monkeypatch.setattr(cluster, "scale", lambda replicas: None)
    cluster.floor_autoscaler("pressure")
    cluster.start_autoscaler()
    [values] = installed
    assert values["query"] == "sum(thesis_context_tokens)"
    assert values["threshold"] == round(real["model"]["kv_cache_tokens"] * 0.75)


def test_the_live_cluster_starts_no_autoscaler_before_its_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The floor names the kind; without it there is nothing to install."""
    experiment = loaded(tmp_path, variants=[AUTOSCALED_VARIANT])
    monkeypatch.setattr("load_generator.experiment.subprocess.run", pytest.fail)
    with pytest.raises(ExperimentError, match="floor"):
        LiveCluster(experiment, {"real": profile_file("real")}).start_autoscaler()


def test_the_live_cluster_removes_the_object_and_waits_for_its_hpa_to_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The HPA KEDA made would otherwise set the count back after it is restored."""
    experiment = loaded(tmp_path, variants=[AUTOSCALED_VARIANT])
    steps: list[Any] = []

    def run(args: list[str], check: bool) -> None:
        assert check
        steps.append(args)

    monkeypatch.setattr("load_generator.experiment.subprocess.run", run)
    monkeypatch.setattr("load_generator.experiment.kubectl", lambda *args: steps.append(args))
    LiveCluster(experiment, {}).stop_autoscaler()
    assert steps[0][:4] == ["helm", "uninstall", "autoscaling", "--ignore-not-found"]
    assert [step[:3] for step in steps[1:]] == [
        ("wait", "scaledobject/vllm", "--for=delete"),
        ("wait", "hpa/keda-hpa-vllm", "--for=delete"),
    ]


def test_the_autoscale_file_scales_sim_pods_on_remaining_work_through_the_burst() -> None:
    experiment = committed(AUTOSCALE_REMAINING)
    sim = yaml.safe_load((REPO_ROOT / "config" / "sim.yaml").read_text())
    check_routing(experiment, {"sim": sim})
    check_autoscaling(experiment, {"sim": sim})
    assert experiment.profile == "sim"
    assert experiment.trace.name == f"{APB_TRACE}.jsonl"
    assert experiment.pattern_name == "burst"
    assert experiment.seed == 6
    [variant] = experiment.variants
    assert (variant.name, variant.autoscaler, variant.default) == ("remaining", "remaining", True)
    assert variant.epp_config == "default-plugins.yaml"
    load_trace(experiment.trace)


def test_the_autoscale_file_discards_the_runs_before_the_first_burst() -> None:
    """As routing-burst's: the runs that start on empty caches and an empty queue."""
    experiment = committed(AUTOSCALE_REMAINING)
    burst = experiment.pattern.burst
    assert burst is not None
    starts = start_times(experiment.pattern, experiment.seed)
    assert experiment.warmup.runs_discarded == sum(start < burst.every_seconds for start in starts)


def test_the_autoscale_files_kv_capacity_is_the_simulated_pods_cache() -> None:
    simulator = yaml.safe_load((REPO_ROOT / "config" / "sim.yaml").read_text())["simulator"]
    experiment = committed(AUTOSCALE_REMAINING)
    assert experiment.kv_capacity_tokens_per_pod == (
        simulator["kv_cache_blocks"] * simulator["block_size"]
    )


# P3.10: a plain HPA on CPU (real pods only, D5), each autoscaled variant's summary, and the files.
# 2026-11-17T16:41:00Z: the warm-up's end in the LiveCluster's quiet-CPU tests (P3.10's review).
WARMED = 1794933660.0
# metrics-server's samples of one vLLM pod: wholly after the warm-up and idle, and one whose 32 s
# window reaches back into the warm-up, at its CPU.
QUIET_SAMPLE = {"timestamp": "2026-11-17T16:41:30Z", "window": "15s", "cpu": "140m"}
LOUD_SAMPLE = {"timestamp": "2026-11-17T16:41:30Z", "window": "32s", "cpu": "1150m"}


def metrics_answer(sample: Mapping[str, str]) -> str:
    return json.dumps(
        {
            "kind": "PodMetricsList",
            "items": [
                {
                    "metadata": {"name": "vllm-a"},
                    "timestamp": sample["timestamp"],
                    "window": sample["window"],
                    "containers": [{"name": "vllm", "usage": {"cpu": sample["cpu"]}}],
                }
            ],
        }
    )


def answering_kubectl(steps: list[Any], metrics: list[str]) -> Callable[..., str]:
    """Records each call; the metrics API gets the next of metrics, anything else ''."""

    def kubectl(*args: str) -> str:
        steps.append(args)
        if args[:2] == ("get", "--raw"):
            return metrics.pop(0)
        return ""

    return kubectl


def quiet_kubectl(steps: list[Any]) -> Callable[..., str]:
    return answering_kubectl(steps, [metrics_answer(QUIET_SAMPLE)])


CPU_VARIANT = {
    "name": "cpu",
    "values": None,
    "epp_config": "default-plugins.yaml",
    "autoscaler": "cpu",
}
CPU_QUERY = (
    'max(kube_horizontalpodautoscaler_status_target_metric{namespace="default",'
    'horizontalpodautoscaler="vllm",metric_name="cpu",metric_target_type="utilization"})'
)
AUTOSCALE_SIM = REPO_ROOT / "experiments" / "autoscale-sim.yaml"
AUTOSCALE_SIM_FAN_OUT = REPO_ROOT / "experiments" / "autoscale-sim-fan-out.yaml"
AUTOSCALE_REAL = REPO_ROOT / "experiments" / "autoscale-real.yaml"
AUTOSCALE_PRESSURE = REPO_ROOT / "experiments" / "autoscale-pressure.yaml"


def profile_file(name: str) -> dict[str, Any]:
    loaded_profile: dict[str, Any] = yaml.safe_load(
        (REPO_ROOT / "config" / f"{name}.yaml").read_text()
    )
    return loaded_profile


def test_a_variant_may_be_scaled_by_a_plain_hpa_on_cpu(tmp_path: Path) -> None:
    experiment = loaded(tmp_path, variants=[CPU_VARIANT])
    check_autoscaling(experiment, {"real": profile_file("real")})
    assert [variant.autoscaler for variant in experiment.variants] == ["cpu"]


PRESSURE_VARIANT = {
    "name": "pressure",
    "values": None,
    "epp_config": "default-plugins.yaml",
    "autoscaler": "pressure",
}


def test_a_variant_may_be_scaled_on_the_context_its_runs_hold(tmp_path: Path) -> None:
    experiment = loaded(tmp_path, variants=[PRESSURE_VARIANT])
    check_autoscaling(experiment, {"real": profile_file("real")})
    assert [variant.autoscaler for variant in experiment.variants] == ["pressure"]


def test_pressure_on_simulated_pods_is_refused(tmp_path: Path) -> None:
    """The simulator admits by max_num_seqs, not by KV blocks (P3.7b)."""
    experiment = loaded(tmp_path, profile="sim", variants=[PRESSURE_VARIANT])
    with pytest.raises(ExperimentError, match=r"P3\.7b"):
        check_autoscaling(experiment, {"sim": profile_file("sim")})


@pytest.mark.parametrize(
    "variant", [AUTOSCALED_VARIANT, PRESSURE_VARIANT], ids=["remaining", "pressure"]
)
def test_an_autoscaler_on_a_profile_with_no_kv_cache_size_is_refused(
    tmp_path: Path, variant: dict[str, Any]
) -> None:
    """Every autoscaled variant reports the context crossing, so each needs the threshold."""
    experiment = loaded(tmp_path, variants=[variant])
    real = profile_file("real")
    del real["model"]["kv_cache_tokens"]
    with pytest.raises(ExperimentError, match="kv_cache_tokens"):
        check_autoscaling(experiment, {"real": real})


def test_cpu_on_simulated_pods_is_refused(tmp_path: Path) -> None:
    """A simulated pod's CPU says nothing of its load (D5)."""
    experiment = loaded(tmp_path, profile="sim", variants=[CPU_VARIANT])
    with pytest.raises(ExperimentError, match="D5"):
        check_autoscaling(experiment, {"sim": profile_file("sim")})


def test_cpu_on_a_profile_with_no_cpu_target_is_refused(tmp_path: Path) -> None:
    experiment = loaded(tmp_path, variants=[CPU_VARIANT])
    real = profile_file("real")
    del real["autoscaling"]["cpu_target_percent"]
    with pytest.raises(ExperimentError, match="cpu_target_percent"):
        check_autoscaling(experiment, {"real": real})


def test_keda_on_a_profile_with_no_cpu_target_is_not_refused(tmp_path: Path) -> None:
    experiment = loaded(tmp_path, variants=[AUTOSCALED_VARIANT])
    real = profile_file("real")
    del real["autoscaling"]["cpu_target_percent"]
    check_autoscaling(experiment, {"real": real})


def test_a_cpu_variant_writes_the_hpas_reading_beside_its_series(tmp_path: Path) -> None:
    experiment = loaded(tmp_path, variants=[CPU_VARIANT, AUTOSCALED_VARIANT])
    out = outputs(experiment, tmp_path)
    cluster = FakeCluster()
    _, meta = run_experiment(experiment, out, cluster, ticking_clock())
    assert out.cpu_hpa("cpu") == out.csv.with_name(f"exp-{experiment.hash}-cpu-cpu-hpa.csv")
    with out.cpu_hpa("cpu").open() as file:
        assert list(csv.DictReader(file)) == [
            {"seconds": "0.0", "cpu_utilization_percent": "12"},
            {"seconds": "5.0", "cpu_utilization_percent": "97.5"},
        ]
    assert meta["variants"]["cpu"]["cpu_hpa"] == out.cpu_hpa("cpu").name
    assert meta["variants"]["remaining"]["cpu_hpa"] is None
    assert not out.cpu_hpa("remaining").exists()
    assert sum(step.startswith("cpu series") for step in cluster.asked) == 1


def test_an_autoscaled_variant_has_its_summary_in_the_meta(tmp_path: Path) -> None:
    """Run 1 is discarded; run 2, the kept one, is one call of 1 ms gap and 100 ms, timed from its
    planned start. On the VM's clock the replay starts at 101 s (the ticking clock's first
    reading in the variant), run 1's call runs 1-101 ms in and run 2's 9.95-10.05 s in, so the one
    Ready pod is busy at the 10 s sample and idle at the other 3. Spec replicas rise to 2 at 10 s
    and Ready never follows, so capacity never came."""
    experiment = loaded(tmp_path, variants=[AUTOSCALED_VARIANT, DEFAULT_VARIANT])
    lines = replay_lines()
    lines[0]["ts_start"] = 101_001
    lines[1]["ts_start"] = 110_950
    cluster = FakeCluster(replays={"remaining": lines})
    _, meta = run_experiment(experiment, outputs(experiment, tmp_path), cluster, ticking_clock())
    assert meta["variants"]["remaining"]["autoscale_summary"] == {
        "runs": 1,
        "finished_runs": 1,
        "failed_runs": 0,
        "interrupted_runs": 0,
        "completion_p50_ms": 101,
        "completion_p95_ms": 101,
        "completion_total_ms": 101,
        "makespan_ms": 101,
        "runs_per_minute": pytest.approx(60_000 / 101),
        "scaled_before_replay": False,
        "time_to_capacity_seconds": None,
        "idle_pod_minutes": pytest.approx(15 / 60),
    }
    assert meta["variants"]["default"]["autoscale_summary"] is None


def test_the_results_have_a_summary_csv_with_a_row_per_autoscaled_variant(tmp_path: Path) -> None:
    experiment = loaded(tmp_path, variants=[AUTOSCALED_VARIANT, DEFAULT_VARIANT])
    out = outputs(experiment, tmp_path)
    rows, meta = run_experiment(experiment, out, FakeCluster(), ticking_clock())
    write_results(out, rows, meta)
    assert out.autoscale_summary == out.csv.with_name(
        f"exp-{experiment.hash}-autoscale-summary.csv"
    )
    with out.autoscale_summary.open() as file:
        summary_rows = list(csv.DictReader(file))
    assert [(row["variant"], row["autoscaler"]) for row in summary_rows] == [
        ("remaining", "remaining")
    ]
    assert summary_rows[0]["makespan_ms"] == "101"


def test_results_with_no_autoscaled_variant_have_no_summary_csv(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    out = outputs(experiment, tmp_path)
    rows, meta = run_experiment(experiment, out, FakeCluster(), ticking_clock())
    write_results(out, rows, meta)
    assert not out.autoscale_summary.exists()


def test_the_report_gives_an_autoscaled_variants_summary(tmp_path: Path) -> None:
    experiment = loaded(tmp_path, variants=[AUTOSCALED_VARIANT])
    rows, meta = run_experiment(
        experiment, outputs(experiment, tmp_path), FakeCluster(), ticking_clock()
    )
    summary = AutoscaleSummary(**meta["variants"]["remaining"]["autoscale_summary"])
    assert summary_text(summary) in report_lines(rows, meta)


def test_the_live_cluster_installs_a_plain_hpa_for_cpu_and_waits_until_it_reads_the_cpu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ScalingActive: the HPA could compute a count from the pods' CPU."""
    experiment = loaded(tmp_path, variants=[CPU_VARIANT])
    real = profile_file("real")
    steps: list[Any] = []

    def run(args: list[str], input: str, text: bool, check: bool) -> None:
        assert text and check
        steps.append((args, yaml.safe_load(input)))

    monkeypatch.setattr("load_generator.experiment.subprocess.run", run)
    monkeypatch.setattr("load_generator.experiment.kubectl", quiet_kubectl(steps))
    cluster = LiveCluster(experiment, {"real": real})
    cluster.warmed_at = WARMED
    monkeypatch.setattr(cluster, "scale", lambda replicas: steps.append(("scale", replicas)))
    assert cluster.floor_autoscaler("cpu") == real["autoscaling"]
    cluster.start_autoscaler()
    assert steps[0] == ("scale", real["autoscaling"]["min_replicas"])
    assert steps[1][:2] == ("get", "--raw")
    args, values = steps[2]
    assert args[:4] == ["helm", "upgrade", "--install", "autoscaling"]
    assert values == helm_values(real["autoscaling"], "cpu", REAL_CONTEXT_THRESHOLD)
    assert steps[3][:3] == ("wait", "hpa/vllm", "--for=condition=ScalingActive")


def test_the_live_cluster_installs_the_cpu_hpa_once_the_pods_cpu_is_quiet_after_the_warm_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its first reading is metrics-server's latest sample, whose window reached into the warm-up
    on 2026-11-17 and scaled it (P3.10's review). Asked again each poll until it is quiet."""
    experiment = loaded(tmp_path, variants=[CPU_VARIANT])
    steps: list[Any] = []
    answers = [metrics_answer(LOUD_SAMPLE), metrics_answer(QUIET_SAMPLE)]
    monkeypatch.setattr(
        "load_generator.experiment.subprocess.run",
        lambda args, **_: steps.append(args[:2]),
    )
    monkeypatch.setattr("load_generator.experiment.kubectl", answering_kubectl(steps, answers))
    monkeypatch.setattr(
        "load_generator.experiment.time.sleep", lambda s: steps.append(("sleep", s))
    )
    cluster = LiveCluster(experiment, {"real": profile_file("real")})
    cluster.warmed_at = WARMED
    monkeypatch.setattr(cluster, "scale", lambda replicas: None)
    cluster.floor_autoscaler("cpu")
    cluster.start_autoscaler()
    metrics = (
        "get",
        "--raw",
        "/apis/metrics.k8s.io/v1beta1/namespaces/default/pods"
        "?labelSelector=app.kubernetes.io%2Fname%3Dvllm",
    )
    assert steps[:4] == [metrics, ("sleep", 5), metrics, ["helm", "upgrade"]]


def test_the_live_cluster_gives_up_on_cpu_that_stays_busy_after_the_warm_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment = loaded(tmp_path, variants=[CPU_VARIANT])
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr("load_generator.experiment.subprocess.run", pytest.fail)
    monkeypatch.setattr(
        "load_generator.experiment.kubectl", lambda *args: metrics_answer(LOUD_SAMPLE)
    )
    monkeypatch.setattr("load_generator.experiment.time.sleep", sleep)
    monkeypatch.setattr("load_generator.experiment.time.monotonic", lambda: now[0])
    cluster = LiveCluster(experiment, {"real": profile_file("real")})
    cluster.warmed_at = WARMED
    monkeypatch.setattr(cluster, "scale", lambda replicas: None)
    cluster.floor_autoscaler("cpu")
    with pytest.raises(ExperimentError, match="not quiet 120 s after the warm-up"):
        cluster.start_autoscaler()
    assert now[0] == 120


def test_the_live_cluster_installs_no_cpu_hpa_without_a_warm_up_to_wait_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment = loaded(tmp_path, variants=[CPU_VARIANT])
    monkeypatch.setattr("load_generator.experiment.subprocess.run", pytest.fail)
    cluster = LiveCluster(experiment, {"real": profile_file("real")})
    monkeypatch.setattr(cluster, "scale", lambda replicas: None)
    cluster.floor_autoscaler("cpu")
    with pytest.raises(ExperimentError, match="warm"):
        cluster.start_autoscaler()


def test_the_live_cluster_keeps_when_its_warm_up_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment = loaded(tmp_path, variants=[CPU_VARIANT])
    cluster = LiveCluster(experiment, {"real": profile_file("real")})

    async def warm(pods: Mapping[str, str]) -> dict[str, list[int]]:
        return {name: [1] for name in pods}

    monkeypatch.setattr(cluster, "_ready_pods", lambda: {"vllm-a": "10.0.0.1"})
    monkeypatch.setattr(cluster, "_warm", warm)
    monkeypatch.setattr("load_generator.experiment.time.time", lambda: WARMED)
    assert cluster.warm_pods() == {"vllm-a": [1]}
    assert cluster.warmed_at == WARMED


def test_after_a_cpu_start_the_live_cluster_waits_for_the_plain_hpa_to_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No KEDA object was made, so it has none to wait for; the plain HPA would set the count
    back after it is restored."""
    experiment = loaded(tmp_path, variants=[CPU_VARIANT])
    steps: list[Any] = []

    def run(args: list[str], check: bool, **_: Any) -> None:
        steps.append(args)

    monkeypatch.setattr("load_generator.experiment.subprocess.run", run)
    monkeypatch.setattr("load_generator.experiment.kubectl", quiet_kubectl(steps))
    cluster = LiveCluster(experiment, {"real": profile_file("real")})
    cluster.warmed_at = WARMED
    monkeypatch.setattr(cluster, "scale", lambda replicas: None)
    cluster.floor_autoscaler("cpu")
    cluster.start_autoscaler()
    steps.clear()
    cluster.stop_autoscaler()
    assert steps[0][:4] == ["helm", "uninstall", "autoscaling", "--ignore-not-found"]
    assert [step[:3] for step in steps[1:]] == [("wait", "hpa/vllm", "--for=delete")]


def test_the_live_cluster_asks_prometheus_for_the_hpas_cpu_at_the_scrape_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment = loaded(tmp_path, variants=[CPU_VARIANT])
    real = profile_file("real")
    asked: list[str] = []

    def kubectl(*args: str) -> str:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(args[2]).query)
        assert (query["start"], query["end"]) == (["100.000"], ["110.000"])
        assert query["step"] == [str(real["monitoring"]["scrape_interval_seconds"])]
        asked.append(query["query"][0])
        return prometheus_answer([{"metric": {}, "values": [[100.0, "12"], [105.0, "97.5"]]}])

    monkeypatch.setattr("load_generator.experiment.kubectl", kubectl)
    rows = LiveCluster(experiment, {"real": real}).cpu_series(100.0, 110.0)
    assert rows == [CpuRow(0.0, 12), CpuRow(5.0, 97.5)]
    assert asked == [CPU_QUERY]


@pytest.mark.parametrize(
    ("path", "pattern"),
    [(AUTOSCALE_SIM, "burst"), (AUTOSCALE_SIM_FAN_OUT, "burst-fan-out")],
    ids=["burst", "burst-fan-out"],
)
def test_the_sim_autoscale_files_run_remaining_then_queue_on_one_replay(
    path: Path, pattern: str
) -> None:
    experiment = committed(path)
    sim = profile_file("sim")
    check_routing(experiment, {"sim": sim})
    check_autoscaling(experiment, {"sim": sim})
    assert experiment.profile == "sim"
    assert experiment.trace.name == f"{APB_TRACE}.jsonl"
    assert experiment.pattern_name == pattern
    assert experiment.seed == 6
    assert [(v.name, v.autoscaler, v.default) for v in experiment.variants] == [
        ("remaining", "remaining", True),
        ("queue", "queue", True),
    ]
    burst = experiment.pattern.burst
    assert burst is not None
    starts = start_times(experiment.pattern, experiment.seed)
    assert experiment.warmup.runs_discarded == sum(start < burst.every_seconds for start in starts)
    simulator = sim["simulator"]
    assert experiment.kv_capacity_tokens_per_pod == (
        simulator["kv_cache_blocks"] * simulator["block_size"]
    )
    load_trace(experiment.trace)


def test_the_sim_autoscale_files_differ_only_in_the_pattern() -> None:
    burst = yaml.safe_load(AUTOSCALE_SIM.read_text())
    fan_out = yaml.safe_load(AUTOSCALE_SIM_FAN_OUT.read_text())
    assert {**burst, "pattern": fan_out["pattern"]} == fan_out


def test_the_real_autoscale_file_runs_cpu_on_real_pods_through_the_burst() -> None:
    experiment = committed(AUTOSCALE_REAL)
    real = profile_file("real")
    check_routing(experiment, {"real": real})
    check_autoscaling(experiment, {"real": real})
    check_stall_window(experiment, {"real": real})
    assert experiment.profile == "real"
    assert experiment.trace.name == f"{APB_TRACE}.jsonl"
    assert (experiment.pattern_name, experiment.seed) == ("burst", 6)
    [variant] = experiment.variants
    assert (variant.name, variant.autoscaler, variant.default) == ("cpu", "cpu", True)
    assert experiment.pod_stall_seconds == 30
    assert (
        experiment.kv_capacity_tokens_per_pod
        == committed(ROUTING_FASTER).kv_capacity_tokens_per_pod
    )
    burst = experiment.pattern.burst
    assert burst is not None
    starts = start_times(experiment.pattern, experiment.seed)
    assert experiment.warmup.runs_discarded == sum(start < burst.every_seconds for start in starts)


def test_the_pressure_file_runs_as_the_cpu_one_with_the_pressure_autoscaler() -> None:
    """P4.12: the same replay as autoscale-real.yaml, so P4.5 can set the two side by side."""
    experiment = committed(AUTOSCALE_PRESSURE)
    cpu = committed(AUTOSCALE_REAL)
    real = profile_file("real")
    check_routing(experiment, {"real": real})
    check_autoscaling(experiment, {"real": real})
    check_stall_window(experiment, {"real": real})
    assert experiment.profile == "real"
    assert (experiment.trace, experiment.pattern_name, experiment.seed) == (
        cpu.trace,
        cpu.pattern_name,
        cpu.seed,
    )
    assert experiment.warmup == cpu.warmup
    assert experiment.pod_stall_seconds == cpu.pod_stall_seconds
    assert experiment.kv_capacity_tokens_per_pod == real["model"]["kv_cache_tokens"]
    [variant] = experiment.variants
    assert (variant.name, variant.autoscaler, variant.default) == ("pressure", "pressure", True)
    assert variant.epp_config == "default-plugins.yaml"


# P4.5 (card 61): the four autoscalers in a balanced Latin square (Williams), A cpu, B pressure,
# C queue, D remaining.
REAL_1TO2 = {
    letter: REPO_ROOT / "experiments" / f"autoscale-real-1to2-{letter}.yaml" for letter in "abcd"
}
REAL_1TO2_ORDERS = {
    "a": ["cpu", "pressure", "remaining", "queue"],
    "b": ["pressure", "queue", "cpu", "remaining"],
    "c": ["queue", "remaining", "pressure", "cpu"],
    "d": ["remaining", "cpu", "queue", "pressure"],
}


def test_the_real_1to2_files_run_their_orders_on_the_cpu_files_replay() -> None:
    cpu = committed(AUTOSCALE_REAL)
    real = profile_file("real")
    for letter, path in REAL_1TO2.items():
        experiment = committed(path)
        check_routing(experiment, {"real": real})
        check_autoscaling(experiment, {"real": real})
        check_stall_window(experiment, {"real": real})
        assert experiment.profile == "real"
        assert (experiment.trace, experiment.pattern_name, experiment.seed) == (
            cpu.trace,
            cpu.pattern_name,
            cpu.seed,
        )
        assert experiment.warmup == cpu.warmup
        assert experiment.pod_stall_seconds == cpu.pod_stall_seconds
        assert experiment.kv_capacity_tokens_per_pod == cpu.kv_capacity_tokens_per_pod
        assert [variant.name for variant in experiment.variants] == REAL_1TO2_ORDERS[letter]
        for variant in experiment.variants:
            assert (variant.autoscaler, variant.default) == (variant.name, True)
            assert variant.epp_config == "default-plugins.yaml"


def test_the_real_1to2_files_differ_only_in_their_order() -> None:
    first, *others = [yaml.safe_load(path.read_text()) for path in REAL_1TO2.values()]
    for other in others:
        by_name = {variant["name"]: variant for variant in other["variants"]}
        assert {**other, "variants": [by_name[v["name"]] for v in first["variants"]]} == first


def test_the_real_1to2_orders_are_a_williams_square() -> None:
    """Each variant once in each position, each right after every other once; a/c and b/d are
    reverse pairs, so every sitting runs both orders."""
    orders = list(REAL_1TO2_ORDERS.values())
    names = set(orders[0])
    for position in range(len(names)):
        assert {order[position] for order in orders} == names
    followed = [pair for order in orders for pair in itertools.pairwise(order)]
    assert sorted(followed) == sorted(
        (first, second) for first in names for second in names if first != second
    )
    assert REAL_1TO2_ORDERS["c"] == REAL_1TO2_ORDERS["a"][::-1]
    assert REAL_1TO2_ORDERS["d"] == REAL_1TO2_ORDERS["b"][::-1]


WARMUP = REPO_ROOT / "experiments" / "warmup.yaml"


def test_the_warmup_file_replays_p3_9bs_burst_with_warmup_off_then_on() -> None:
    """P4.2 (card 58): P3.9b's sim replay under KEDA on remaining work, the plugin with affinity
    in both variants, warm-up the only difference; every variant runs the plugin, whose log has the
    pods' Ready times the report reads."""
    experiment = committed(WARMUP)
    remaining = committed(AUTOSCALE_REMAINING)
    sim = profile_file("sim")
    check_routing(experiment, {"sim": sim})
    check_autoscaling(experiment, {"sim": sim})
    assert experiment.profile == "sim"
    assert (experiment.trace, experiment.pattern_name, experiment.seed) == (
        remaining.trace,
        remaining.pattern_name,
        remaining.seed,
    )
    assert experiment.warmup == remaining.warmup
    assert experiment.kv_capacity_tokens_per_pod == remaining.kv_capacity_tokens_per_pod
    assert [
        (v.name, v.routing, v.epp_config, v.autoscaler, v.values) for v in experiment.variants
    ] == [
        (
            "warmup-off",
            {"plugin": True, "affinity": True, "warmup": False},
            "thesis-plugins.yaml",
            "remaining",
            None,
        ),
        (
            "warmup-on",
            {"plugin": True, "affinity": True, "warmup": True},
            "thesis-plugins.yaml",
            "remaining",
            None,
        ),
    ]


# P4.13c (docs/task-cards/68-P4.13c-two-pod-stall.md): how a variant's pods start, and Windows'
# own reading of the GPU's memory beside nvidia-smi's.

TWO_PODS = {"name": "two", "values": None, "epp_config": "default-plugins.yaml", "replicas": 2}
TWO_POD_STALL = REPO_ROOT / "experiments" / "two-pod-stall.yaml"


def test_a_variant_starts_its_pods_together_unless_it_says_otherwise(tmp_path: Path) -> None:
    experiment = loaded(tmp_path, variants=[TWO_PODS])
    assert [variant.start for variant in experiment.variants] == ["together"]


def test_a_fixed_count_of_two_or_more_may_start_one_at_a_time(tmp_path: Path) -> None:
    experiment = loaded(tmp_path, variants=[{**TWO_PODS, "start": "one_at_a_time"}])
    assert [variant.start for variant in experiment.variants] == ["one_at_a_time"]


@pytest.mark.parametrize(
    ("changes", "says"),
    [
        ({"replicas": 1}, "replicas"),
        ({"replicas": None}, "replicas"),
        ({"replicas": None, "autoscaler": "remaining"}, "autoscaler"),
        ({"start": "staggered"}, "staggered"),
    ],
    ids=["one pod", "the profile's count", "an autoscaler", "an unknown start"],
)
def test_a_start_that_cannot_be_kept_is_refused_naming_the_variant(
    tmp_path: Path, changes: dict[str, Any], says: str
) -> None:
    section = {**TWO_PODS, "start": "one_at_a_time", **changes}
    with pytest.raises(ExperimentError, match=f"variant two: .*{says}"):
        loaded(tmp_path, variants=[section])


@pytest.mark.parametrize(
    ("start", "steps"),
    [
        ("together", ["scale 2", "restart", "routing default"]),
        ("one_at_a_time", ["scale 1", "restart", "scale 2", "routing default"]),
    ],
)
def test_one_at_a_time_restarts_one_pod_and_adds_the_others_before_the_warm_up(
    tmp_path: Path, start: str, steps: list[str]
) -> None:
    """Every pod after the first starts beside a Ready one, as autoscaling adds them; together is
    the restart as before, which recreates every pod at once."""
    experiment = loaded(tmp_path, variants=[{**TWO_PODS, "start": start}])
    cluster = FakeCluster()
    run_experiment(experiment, outputs(experiment, tmp_path), cluster, ticking_clock())
    assert cluster.asked[1 : 1 + len(steps)] == steps
    assert cluster.asked.index("warm") > len(steps)


def test_each_variants_meta_says_how_its_pods_started(tmp_path: Path) -> None:
    experiment = loaded(
        tmp_path, variants=[{**TWO_PODS, "start": "one_at_a_time"}, DEFAULT_VARIANT]
    )
    _, meta = run_experiment(
        experiment, outputs(experiment, tmp_path), FakeCluster(), ticking_clock()
    )
    assert meta["variants"]["two"]["start"] == "one_at_a_time"
    assert meta["variants"]["default"]["start"] == "together"


def test_a_real_profile_checks_windows_gpu_memory_first_and_logs_it_around_the_variants(
    tmp_path: Path,
) -> None:
    experiment = loaded(tmp_path)
    cluster = FakeCluster()
    run_experiment(experiment, outputs(experiment, tmp_path), cluster, ticking_clock())
    stem = f"exp-{experiment.hash}"
    assert cluster.windows_gpu == [
        ("check", 1),
        (f"log {stem}-windows-gpu.csv", 1),
        ("stop", len(cluster.asked)),
    ]


def test_a_sim_only_experiment_has_no_windows_gpu_log(tmp_path: Path) -> None:
    experiment = loaded(tmp_path, profile="sim")
    cluster = FakeCluster()
    _, meta = run_experiment(experiment, outputs(experiment, tmp_path), cluster, ticking_clock())
    assert cluster.windows_gpu == []
    assert meta["windows_gpu_log"] is None


def test_windows_gpu_memory_that_cannot_be_read_stops_the_experiment_before_any_restart(
    tmp_path: Path,
) -> None:
    experiment = loaded(tmp_path)
    cluster = FakeCluster(fail_windows_gpu_check=True)
    with pytest.raises(ExperimentError, match="typeperf"):
        run_experiment(experiment, outputs(experiment, tmp_path), cluster, ticking_clock())
    assert cluster.asked == ["check"]
    assert cluster.gpu == [("gpu check", 1)]


def test_a_replay_that_breaks_still_stops_the_windows_gpu_log_before_the_default_comes_back(
    tmp_path: Path,
) -> None:
    experiment = loaded(tmp_path)
    cluster = FakeCluster(fail_replay_of="round-robin")
    out = outputs(experiment, tmp_path)
    out.csv.parent.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="replay broke"):
        run_experiment(experiment, out, cluster, ticking_clock())
    stops = [steps for event, steps in cluster.windows_gpu if event == "stop"]
    assert stops == [len(cluster.asked) - 1]
    assert cluster.asked[-1] == "routing default"


def test_the_meta_names_the_windows_gpu_log_and_whether_typeperf_ended_early(
    tmp_path: Path,
) -> None:
    experiment = loaded(tmp_path)
    out = outputs(experiment, tmp_path)
    _, running = run_experiment(experiment, out, FakeCluster(), ticking_clock())
    _, ended = run_experiment(experiment, out, FakeCluster(windows_gpu_exit=2), ticking_clock())
    assert running["windows_gpu_log"] == out.windows_gpu.name
    assert running["windows_gpu_log_ended_early_with"] is None
    assert ended["windows_gpu_log_ended_early_with"] == 2


def test_finish_names_the_windows_gpu_log_and_says_when_it_has_a_gap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    experiment = loaded(tmp_path)
    out = outputs(experiment, tmp_path)
    rows, meta = run_experiment(experiment, out, FakeCluster(windows_gpu_exit=2), ticking_clock())
    finish(experiment, out, rows, meta)
    printed = capsys.readouterr().out
    assert f"Windows GPU memory log {out.windows_gpu}" in printed
    assert (
        "typeperf ended early with exit 2, so the Windows GPU memory log stops before the run did"
        in printed
    )


def test_the_windows_gpu_log_is_an_output_and_a_leftover(tmp_path: Path) -> None:
    experiment = loaded(tmp_path)
    out = outputs(experiment, tmp_path)
    assert out.windows_gpu == out.csv.with_name(f"exp-{experiment.hash}-windows-gpu.csv")
    out.windows_gpu.parent.mkdir(parents=True)
    out.windows_gpu.write_text("")
    with pytest.raises(ExperimentError, match="exist"):
        refuse_leftovers(experiment, out)


# typeperf's output as this laptop's Windows wrote it (2026-11-23), one adapter kept and the
# computer's name replaced: a blank line, the header, each row ended by \r with its newline written
# before the next, and on exit two lines of its own.
TYPEPERF_RAW = (
    "\r\n"
    '"(PDH-CSV 4.0)","\\\\HOST-1\\GPU Adapter Memory(luid_0x0_0x11B7F_phys_0)\\Dedicated Usage",'
    '"\\\\HOST-1\\GPU Adapter Memory(luid_0x0_0x11B7F_phys_0)\\Shared Usage"\r\n'
    '"11/23/2026 12:27:32.972","412594176.000000","4587520.000000"\r\n'
    "Exiting, please wait...                         \r\n"
    "The command completed successfully.\r\n"
    "\r\r"
)
TYPEPERF_CSV = (
    '"(PDH-CSV 4.0)","\\GPU Adapter Memory(luid_0x0_0x11B7F_phys_0)\\Dedicated Usage",'
    '"\\GPU Adapter Memory(luid_0x0_0x11B7F_phys_0)\\Shared Usage"\n'
    '"11/23/2026 12:27:32.972","412594176.000000","4587520.000000"\n'
)


def test_typeperfs_output_becomes_its_csv_lines_without_the_computers_name() -> None:
    """The name would otherwise go into the committed results."""
    assert windows_gpu_csv(TYPEPERF_RAW) == TYPEPERF_CSV


def test_a_typeperf_log_stopped_mid_run_keeps_its_last_row() -> None:
    """A row's line end comes with the next row, so the last one has none when typeperf stops."""
    stopped = TYPEPERF_RAW.split("Exiting")[0].rstrip("\r\n")
    assert windows_gpu_csv(stopped) == TYPEPERF_CSV


def test_the_live_cluster_checks_windows_gpu_memory_with_the_counters_it_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment = loaded(tmp_path)
    handed: list[list[str]] = []

    def run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        handed.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=TYPEPERF_RAW)

    monkeypatch.setattr("load_generator.experiment.subprocess.run", run)
    LiveCluster(experiment, {}).check_windows_gpu()
    assert handed == [["typeperf.exe", *WINDOWS_GPU_COUNTERS, "-sc", "1"]]


@pytest.mark.parametrize(
    ("code", "stdout"),
    [(2, "Error: No valid counters.\n"), (0, TYPEPERF_RAW.split('"11/')[0])],
    ids=["exits 2", "samples nothing"],
)
def test_a_windows_gpu_check_that_fails_or_samples_nothing_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int, stdout: str
) -> None:
    experiment = loaded(tmp_path)

    def run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, code, stdout=stdout)

    monkeypatch.setattr("load_generator.experiment.subprocess.run", run)
    with pytest.raises(ExperimentError, match="typeperf"):
        LiveCluster(experiment, {}).check_windows_gpu()


def windows_gpu_popen(started: list[FakeProcess], exited: int | None) -> Callable[..., Any]:
    """Writes typeperf's output into the file it is handed, as typeperf.exe does."""

    def popen(args: list[str], stdout: Any) -> FakeProcess:
        stdout.write(TYPEPERF_RAW.encode())
        started.append(FakeProcess(args, exited))
        return started[-1]

    return popen


def test_the_live_windows_gpu_log_samples_both_counters_at_the_scrape_interval_into_its_csv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment = loaded(tmp_path)
    real = profile_file("real")
    started: list[FakeProcess] = []
    monkeypatch.setattr(
        "load_generator.experiment.subprocess.Popen", windows_gpu_popen(started, None)
    )
    cluster = LiveCluster(experiment, {"real": real})
    path = tmp_path / "windows-gpu.csv"
    cluster.start_windows_gpu_log(path)
    assert cluster.stop_windows_gpu_log() is None
    (process,) = started
    assert process.args == [
        "typeperf.exe",
        "\\GPU Adapter Memory(*)\\Dedicated Usage",
        "\\GPU Adapter Memory(*)\\Shared Usage",
        "-si",
        str(real["monitoring"]["scrape_interval_seconds"]),
    ]
    assert process.terminated
    assert path.read_text() == TYPEPERF_CSV


def test_a_live_windows_gpu_log_that_ended_early_gives_its_exit_and_is_still_its_csv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    experiment = loaded(tmp_path)
    started: list[FakeProcess] = []
    monkeypatch.setattr("load_generator.experiment.subprocess.Popen", windows_gpu_popen(started, 2))
    cluster = LiveCluster(experiment, {"real": profile_file("real")})
    path = tmp_path / "windows-gpu.csv"
    cluster.start_windows_gpu_log(path)
    assert cluster.stop_windows_gpu_log() == 2
    assert not started[0].terminated
    assert path.read_text() == TYPEPERF_CSV


def test_two_pod_stall_replays_validate_sims_load_on_two_real_pods_started_both_ways() -> None:
    """P4.13c's diagnostic: the replay that stalled in P4.13b, once with the pods started as it
    started them and once one at a time. A stalled replay is the result, so none is refused."""
    real = profile_file("real")
    experiment = committed(TWO_POD_STALL)
    validate = load_experiment(VALIDATE_SIM, load_replay_config(real["replay"]).patterns)
    assert experiment.profile == "real"
    assert (experiment.trace, experiment.pattern_name, experiment.pattern, experiment.seed) == (
        validate.trace,
        validate.pattern_name,
        validate.pattern,
        validate.seed,
    )
    assert experiment.warmup == validate.warmup
    assert experiment.kv_capacity_tokens_per_pod == validate.kv_capacity_tokens_per_pod
    assert [(v.name, v.profile, v.replicas, v.start, v.default) for v in experiment.variants] == [
        ("together", "real", 2, "together", True),
        ("one-at-a-time", "real", 2, "one_at_a_time", True),
    ]
    assert experiment.validation is None
    assert experiment.pod_stall_seconds is None


VALIDATE_SIM_2PODS = REPO_ROOT / "experiments" / "validate-sim-2pods.yaml"


def test_validate_sim_2pods_is_validate_sims_two_pod_pair_alone() -> None:
    """P4.13c's proof (the user, 2026-11-23): validate-sim's 2-pod pair with its load, tolerance and
    starts as they were, since two-pod-stall did not reproduce the stall. A stalled replay is
    evidence, so none is refused."""
    real = profile_file("real")
    patterns = load_replay_config(real["replay"]).patterns
    experiment = load_experiment(VALIDATE_SIM_2PODS, patterns)
    validate = load_experiment(VALIDATE_SIM, patterns)
    assert (experiment.trace, experiment.pattern_name, experiment.pattern, experiment.seed) == (
        validate.trace,
        validate.pattern_name,
        validate.pattern,
        validate.seed,
    )
    assert experiment.warmup == validate.warmup
    assert experiment.kv_capacity_tokens_per_pod == validate.kv_capacity_tokens_per_pod
    assert [(v.name, v.profile, v.replicas, v.start) for v in experiment.variants] == [
        ("real-2pods", "real", 2, "together"),
        ("sim-2pods", "sim", 2, "together"),
    ]
    assert experiment.validation is not None
    assert validate.validation is not None
    assert experiment.validation.tolerance == validate.validation.tolerance
    assert [(pair.real, pair.sim) for pair in experiment.validation.pairs] == [
        ("real-2pods", "sim-2pods")
    ]
    assert experiment.pod_stall_seconds is None


# P5.1a: the spike on compiled real pods (card 72), and upstream's session-affinity-filter.

P5_1A_SPIKE = REPO_ROOT / "experiments" / "p5-1a-spike.yaml"
SESSION_AFFINITY_VALUES = REPO_ROOT / "infra" / "helm" / "router-session-affinity-values.yaml"


def test_the_p5_1a_spike_runs_routing_bursts_load() -> None:
    """The spike answers whether P5.1's squares can run P3.7's load, so it must be that load."""
    spike = committed(P5_1A_SPIKE)
    burst = committed(ROUTING_BURST)
    assert (spike.profile, spike.trace, spike.pattern_name, spike.seed) == (
        burst.profile,
        burst.trace,
        burst.pattern_name,
        burst.seed,
    )
    assert spike.warmup == burst.warmup
    assert spike.pod_stall_seconds == burst.pod_stall_seconds
    assert spike.not_worse_tolerance is None
    assert spike.faster_by is None


def test_the_p5_1a_spike_runs_default_twice_then_upstreams_filter() -> None:
    """Two default variants alike in all but the name give the host's spread on one picker."""
    spike = committed(P5_1A_SPIKE)
    assert [variant.name for variant in spike.variants] == [
        "default",
        "default-again",
        "session-affinity",
    ]
    first, again, upstream = spike.variants
    assert first.default and again.default
    assert (first.values, first.epp_config) == (again.values, again.epp_config)
    assert upstream.routing is None
    assert upstream.values == SESSION_AFFINITY_VALUES.resolve()


def test_upstreams_filter_binds_runs_by_their_header_as_long_as_the_plugins_pin() -> None:
    """The baseline for task affinity keys on the same header and holds a quiet run as long as
    thesis-run-router does, so the two differ in what they do with a run, not in which run."""
    epp = yaml.safe_load(SESSION_AFFINITY_VALUES.read_text())["router"]["epp"]
    config_file = epp["pluginsConfigFile"]
    assert config_file == committed(P5_1A_SPIKE).variants[2].epp_config
    config = yaml.safe_load(epp["pluginsCustomConfig"][config_file])
    plugins = {plugin["type"]: plugin.get("parameters") for plugin in config["plugins"]}
    session = plugins["session-affinity-filter"]
    assert session["strategy"] == "session_id"
    assert session["sessionIdConfig"]["sources"] == [{"header": "x-run-id"}]
    real = profile_file("real")
    assert session["sessionIdConfig"]["evictionTtlSeconds"] == real["routing"]["run_ttl_seconds"]
    assert epp["flags"]["allow-experimental-plugins"] == "true"
    (profile,) = config["schedulingProfiles"]
    refs = [plugin["pluginRef"] for plugin in profile["plugins"]]
    assert refs[0] == "session-affinity-filter"
    weights = {plugin["pluginRef"]: plugin.get("weight") for plugin in profile["plugins"][1:]}
    assert weights == {
        "queue-scorer": 2,
        "kv-cache-utilization-scorer": 2,
        "prefix-cache-scorer": 3,
    }


P5_1A_HEAVY = REPO_ROOT / "experiments" / "p5-1a-heavy.yaml"


def test_the_heavy_check_runs_the_spikes_load_but_its_pattern() -> None:
    """P5.1a-2 answers whether twice the burst overloads the pods the spike ran on, so all but the
    pattern is the spike's, and its warm-up drops the runs before the first burst."""
    heavy = committed(P5_1A_HEAVY)
    spike = committed(P5_1A_SPIKE)
    assert heavy.pattern_name == "burst-fan-out-heavy"
    assert (heavy.profile, heavy.trace, heavy.seed) == (spike.profile, spike.trace, spike.seed)
    assert heavy.warmup == spike.warmup
    assert heavy.pod_stall_seconds == spike.pod_stall_seconds
    burst = heavy.pattern.burst
    assert burst is not None
    starts = start_times(heavy.pattern, heavy.seed)
    assert heavy.warmup.runs_discarded == sum(start < burst.every_seconds for start in starts)
    assert [variant.name for variant in heavy.variants] == ["default"]
    assert heavy.variants[0].default
