"""make experiment (P2.5): the experiment file, the order of the steps, and what a run writes.

The cluster is a fake that records what it is asked, so these tests say what must happen around a
replay and in which order. The live path (helm, kubectl, the gateway) is what make experiment's
done-when proves.
"""

import asyncio
import csv
import hashlib
import json
import subprocess
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from load_generator.experiment import (
    GPU_FIELDS,
    Experiment,
    ExperimentError,
    LiveCluster,
    PodStall,
    Warmup,
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
    ) -> None:
        self.asked: list[str] = []
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

    def picker_log(self) -> str:
        self.asked.append("picker log")
        return "the picker's log\n"

    def vllm_logs(self) -> dict[str, str]:
        self.vllm.append((self.replayed_variant, len(self.asked)))
        return {"vllm-x-pod-a": "pod a's log\n", "vllm-x-pod-b": "pod b's log\n"}

    def pod_stalls(self, since: float, until: float, seconds: int) -> list[PodStall]:
        self.asked.append(f"stalls {since:.0f} {until:.0f} {seconds}")
        return list(self.stalls.get(self.replayed_variant or "", []))


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
