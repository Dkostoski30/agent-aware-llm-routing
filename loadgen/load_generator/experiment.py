"""make experiment (P2.5): one experiment file's variants, as one CSV, .meta.json and graph.

For each variant, in the file's order: restart the vLLM pods, put the picker on the variant, warm
each pod, replay. Both variants send identical prompts at one seed, so without the restart the
second would find the first's text cached. A failed call is a result, not a stop: a replay runs
under overload on purpose. After any failure the default picker is put back, since a variant left
behind would silently change every later run; if even that fails, the run ends with that error.

A variant may name its own profile and replica count (P2.9): the vLLM release is swapped for the
profile's whenever it changes, with no make down, since make up asks for sudo, and the pods are
scaled before the restart. The deployed profile is scaled back to its own count at the end.

A variant may instead turn routing rules on (P3.2): routing: {plugin: true, affinity: true} is laid
over its profile's routing section by infra/routing-values.sh, and the picker runs
thesis-plugins.yaml. With the plugin on, the replay sends each run's end to the picker (P3.3).

On real pods, nvidia-smi samples the GPU into a file beside the results for the whole run, and is
checked before anything restarts (P3.7, PLAN.md Phase 5's real-pod comparisons). A plugin variant's
picker log is kept before the next switch restarts the picker. A file may name a
not_worse_tolerance, which goes into the .meta.json for make experiment-compare's verdict. A file
may name pod_stall_seconds: after each replay Prometheus is asked whether a pod made no tokens for
that long while it had calls, and such a replay stops the run (P3.7, experiments/log.md).

Usage: python -m load_generator.experiment EXPERIMENT
"""

import argparse
import asyncio
import hashlib
import json
import subprocess
import tempfile
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
import yaml

from load_generator.compare import max_gap_deviation
from load_generator.graph import render
from load_generator.plan import (
    Pattern,
    PlannedRun,
    load_replay_config,
    served_model,
    start_times,
    warmup_run,
)
from load_generator.record import REPO_ROOT, experiment_hash, kubectl
from load_generator.replay import VLLM_PORT, failed_calls, replay_runs, replay_to_file
from load_generator.results import (
    CallRow,
    call_rows,
    calls_per_pod,
    percentile,
    pod_switch_share,
    summarise,
    ttft_stats,
    variant_rows,
    write_csv,
)
from load_generator.validation import (
    Validation,
    load_validation,
    table,
    table_path,
    write_table,
)
from load_generator.validation import report_lines as validation_lines

VLLM_SELECTOR = "app.kubernetes.io/name=vllm"
EPP_DEPLOYMENT = "vllm-pool-epp"
CLOCK_JUMP_LINE = "Clock change detected"
# journalctl's exit codes with --grep: 0 found lines, 1 found none. Anything else is an error.
JOURNAL_FOUND_OR_NONE = (0, 1)
VLLM_DEPLOYMENT = "deployment/vllm"
# make up's rule: this profile's pods come from make sim-deploy, any other's from make vllm-deploy.
SIM_PROFILE = "sim"
# As infra/down.sh: the pods' 30 s grace period plus the API calls, with a wide margin.
UNINSTALL_TIMEOUT = "120s"
# Pods loading the model on one GPU and disk, as VLLM_DEPLOY_TIMEOUT in the Makefile.
SCALE_TIMEOUT = "900s"
# The picker's file when a variant runs the plugin (infra/routing-values.sh), whose log has a line
# per call with the rule that picked it (P3.5, P3.6).
PLUGIN_CONFIG = "thesis-plugins.yaml"
# What the GPU log samples (P3.7). As P2.5's log beside the baseline, plus the memory clock and
# load and the memory used: temperature, clocks, power and the reasons the clocks were held down
# (clocks_event_reasons, which this driver also names in the file's header) tell a slowed GPU from a
# routing difference.
GPU_FIELDS = (
    "timestamp,temperature.gpu,utilization.gpu,utilization.memory,clocks.gr,clocks.mem,"
    "power.draw,memory.used,clocks_event_reasons.active"
)
# Prometheus's HTTP API through the API server's service proxy, so no port-forward is needed:
# kube-prometheus-stack's service under make up's release name, monitoring
# (infra/install-monitoring.sh).
PROMETHEUS_RANGE_API = (
    "/api/v1/namespaces/monitoring/services/monitoring-kube-prometheus-prometheus:9090"
    "/proxy/api/v1/query_range"
)
# Per pod, at each sample: the fewest calls it had running plus the fewest waiting over the window
# before it, and the prompt and generation tokens it made in that window. Both sides of each +
# carry the same labels, so they add pod by pod. A pod over 0 on the first and at 0 on the second
# had calls all through the window and made nothing: on 2026-11-12 such a pod sat 30-135 s at a
# time while the other pod on the GPU went on (P3.7).
BUSY_QUERY = (
    "sum by (pod) (min_over_time(vllm:num_requests_running[{window}s]) "
    "+ min_over_time(vllm:num_requests_waiting[{window}s]))"
)
TOKENS_QUERY = (
    "sum by (pod) (increase(vllm:prompt_tokens_total[{window}s]) "
    "+ increase(vllm:generation_tokens_total[{window}s]))"
)
# The profile's routing keys that are no rules but its own numbers, the pin TTL (P3.3), the
# overload limits (P3.5) and the parallel threshold (P3.6): a variant sets rules only
# (infra/routing-values.sh).
PROFILE_ONLY_KEYS = frozenset(
    {
        "run_ttl_seconds",
        "overload_queue_threshold",
        "overload_sustained_seconds",
        "parallel_queue_threshold",
    }
)
# A later step that stayed on its pod finds its run's previous prompt cached, less up to this many
# tokens: the 9 a replay's chat template adds after it, and up to 15 of a partly filled 16-token
# block (P2.4, experiments/log.md). Derived, not tuned: a step short by more lost part of its run's
# history on its own pod, a capacity effect the hit rate alone would hide.
CACHE_SHORTFALL_TOKENS = 24


class ExperimentError(Exception):
    """The experiment must not start or go on: its results would not mean what they say."""


@dataclass(frozen=True)
class Warmup:
    runs_discarded: int
    calls_per_pod: int
    words: int
    completion_tokens: int


@dataclass(frozen=True)
class PodStall:
    pod: str
    # When it stopped making tokens, in seconds since the epoch.
    start: float
    seconds: int


@dataclass(frozen=True)
class Variant:
    name: str
    # On top of infra/helm/router-values.yaml; None is the default picker.
    values: Path | None
    # The file the running picker must name.
    epp_config: str
    # The variant's own or the file's.
    profile: str
    # None is the profile's own count.
    replicas: int | None
    # Rules laid over the profile's routing section; None is the profile's own. Never with values.
    routing: Mapping[str, bool] | None

    @property
    def default(self) -> bool:
        """The picker as make up installs it for the profile."""
        return self.values is None and self.routing is None


@dataclass(frozen=True)
class Experiment:
    # The file's stem, which names its results.
    name: str
    hash: str
    # None when every variant names its own.
    profile: str | None
    trace: Path
    pattern_name: str
    pattern: Pattern
    seed: int
    kv_capacity_tokens_per_pod: int
    warmup: Warmup
    variants: list[Variant]
    validation: Validation | None
    # How much slower than the reference variant's p95 still counts as not worse (P3.7); None
    # when the file makes no such claim.
    not_worse_tolerance: float | None
    # The variant on the default picker, which the other is held to; None with no tolerance.
    reference_variant: str | None
    # How long a pod with calls may make no tokens before its replay is refused (P3.7); None when
    # replays are not checked.
    pod_stall_seconds: int | None

    def profiles(self) -> list[str]:
        """Each profile once, in the variants' order."""
        return list(dict.fromkeys(variant.profile for variant in self.variants))

    @property
    def gpu_logged(self) -> bool:
        """Whether a variant runs real pods, which share the one GPU."""
        return any(variant.profile != SIM_PROFILE for variant in self.variants)


@dataclass(frozen=True)
class Outputs:
    csv: Path
    meta: Path
    graph: Path
    validation: Path

    def replay(self, variant: str) -> Path:
        return self.csv.with_name(f"{self.csv.stem}-{variant}.jsonl")

    @property
    def gpu(self) -> Path:
        return self.csv.with_name(f"{self.csv.stem}-gpu.csv")

    def picker(self, variant: str) -> Path:
        return self.csv.with_name(f"{self.csv.stem}-{variant}-picker.log")


class Cluster(Protocol):
    """What an experiment asks of the cluster; LiveCluster is the real one."""

    def check_ready(self) -> None: ...

    def deploy(self, profile: str) -> None: ...

    # None is the deployed profile's own count.
    def scale(self, replicas: int | None) -> None: ...

    def restart_pods(self) -> None: ...

    # Both None is the default picker; at most one is given.
    def set_routing(self, values: Path | None, routing: Mapping[str, bool] | None) -> None: ...

    def epp_config_file(self) -> str: ...

    def warm_pods(self) -> dict[str, list[int]]: ...

    # routing is the variant's rules, None the profile's own.
    def replay(self, out: Path, routing: Mapping[str, bool] | None) -> list[dict[str, Any]]: ...

    def clock_jumps(self, since: float, until: float) -> int: ...

    # Raises ExperimentError if nvidia-smi cannot read the GPU.
    def check_gpu(self) -> None: ...

    def start_gpu_log(self, path: Path) -> None: ...

    # nvidia-smi's exit code if it had ended before it was stopped, else None.
    def stop_gpu_log(self) -> int | None: ...

    # The running picker's whole log.
    def picker_log(self) -> str: ...

    # Each stretch from since to until in which a pod had calls and made no tokens for seconds.
    def pod_stalls(self, since: float, until: float, seconds: int) -> list[PodStall]: ...


def variant_profile(spec: Mapping[str, Any], section: Mapping[str, Any]) -> str:
    profile: str | None = section.get("profile", spec.get("profile"))
    if profile is None:
        raise ExperimentError(f"variant {section['name']} has no profile, nor has the file")
    return profile


def _variant(directory: Path, spec: Mapping[str, Any], section: Mapping[str, Any]) -> Variant:
    values = None
    if section["values"] is not None:
        values = (directory / section["values"]).resolve()
        if not values.exists():
            raise ExperimentError(f"variant {section['name']}: no file {section['values']}")
    replicas = section.get("replicas")
    if replicas is not None and replicas < 1:
        raise ExperimentError(f"variant {section['name']}: replicas {replicas}, at least 1")
    routing = section.get("routing")
    if routing is not None:
        if values is not None:
            raise ExperimentError(
                f"variant {section['name']}: a values file and routing rules, give one"
            )
        if (
            not isinstance(routing, dict)
            or not routing
            or not all(isinstance(on, bool) for on in routing.values())
        ):
            raise ExperimentError(
                f"variant {section['name']}: routing must map rules to true or false, got {routing}"
            )
    return Variant(
        section["name"],
        values,
        section["epp_config"],
        variant_profile(spec, section),
        replicas,
        routing,
    )


def check_routing(experiment: Experiment, profiles: Mapping[str, Mapping[str, Any]]) -> None:
    """Every rule a variant sets is one its profile has, no rule is on while the plugin is off, and
    neither the overload guard nor the parallel rule is on while affinity is off: each would do
    nothing and the variant would read as its result. infra/routing-values.sh refuses all of these
    too, but only when the variant's switch runs, which may be hours into the run."""
    for variant in experiment.variants:
        routing = profiles[variant.profile]["routing"]
        known = {rule: on for rule, on in routing.items() if rule not in PROFILE_ONLY_KEYS}
        set_here = sorted(PROFILE_ONLY_KEYS & set(variant.routing or {}))
        if set_here:
            raise ExperimentError(
                f"variant {variant.name}: {', '.join(set_here)}: the profile's, "
                "a variant sets only rules"
            )
        unknown = sorted(set(variant.routing or {}) - set(known))
        if unknown:
            raise ExperimentError(
                f"variant {variant.name}: no rule {', '.join(unknown)} in profile "
                f"{variant.profile}, has {', '.join(known)}"
            )
        rules = {**known, **(variant.routing or {})}
        on = [rule for rule, value in rules.items() if rule != "plugin" and value]
        if on and not rules["plugin"]:
            raise ExperimentError(f"variant {variant.name}: {', '.join(on)} on, but plugin is off")
        if rules["overload_guard"] and not rules["affinity"]:
            raise ExperimentError(
                f"variant {variant.name}: overload_guard on, but affinity is off: the guard moves "
                "pinned runs only"
            )
        if rules["parallel_spill"] and not rules["affinity"]:
            raise ExperimentError(
                f"variant {variant.name}: parallel_spill on, but affinity is off: a group stays "
                "on its run's pinned pod"
            )


def plugin_on(
    profile_routing: Mapping[str, Any], variant_routing: Mapping[str, bool] | None
) -> bool:
    """Whether the picker runs the plugin for a variant: its rules laid over its profile's, as
    infra/routing-values.sh lays them."""
    on: bool = {**profile_routing, **(variant_routing or {})}["plugin"]
    return on


def check_same_replay(profiles: Mapping[str, Mapping[str, Any]], pattern: str) -> None:
    """Variants on different profiles compare only if those replay alike: the pattern, the shared
    prefixes and the call deadline, which decides which calls fail. The seed is the file's."""

    def replay(profile: Mapping[str, Any]) -> dict[str, Any]:
        return {
            f"replay.patterns.{pattern}": profile["replay"]["patterns"].get(pattern),
            "replay.shared_prefix_tokens": profile["replay"]["shared_prefix_tokens"],
            "agent.llm_timeout_seconds": profile["agent"]["llm_timeout_seconds"],
        }

    (first, first_profile), *others = profiles.items()
    expected = replay(first_profile)
    for name, profile in others:
        found = replay(profile)
        for key, value in found.items():
            if value != expected[key]:
                raise ExperimentError(f"{key} differs: {first} has {expected[key]}, {name} {value}")


def _not_worse(
    spec: Mapping[str, Any], variants: Sequence[Variant]
) -> tuple[float | None, str | None]:
    """The tolerance and the variant it holds the other to, the one on the default picker."""
    tolerance = spec.get("not_worse_tolerance")
    if tolerance is None:
        return None, None
    if isinstance(tolerance, bool) or not isinstance(tolerance, int | float) or tolerance < 0:
        raise ExperimentError(f"not_worse_tolerance must be a number at or over 0, got {tolerance}")
    if len(variants) != 2:
        raise ExperimentError(
            f"not_worse_tolerance compares two variants, the file has {len(variants)}"
        )
    defaults = [variant.name for variant in variants if variant.default]
    if len(defaults) != 1:
        raise ExperimentError(
            "not_worse_tolerance needs one variant on the default picker to hold the other to, "
            f"found {defaults}"
        )
    return float(tolerance), defaults[0]


def _pod_stall_seconds(spec: Mapping[str, Any]) -> int | None:
    seconds = spec.get("pod_stall_seconds")
    if seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, int)):
        raise ExperimentError(f"pod_stall_seconds must be a whole number of seconds, got {seconds}")
    if seconds is not None and seconds <= 0:
        raise ExperimentError(f"pod_stall_seconds must be over 0, got {seconds}")
    return seconds


def check_stall_window(experiment: Experiment, profiles: Mapping[str, Mapping[str, Any]]) -> None:
    """Prometheus tells how many tokens a pod made in a window only from two samples in it, so the
    window must hold two of each profile's scrapes. Before the run, which takes hours."""
    if experiment.pod_stall_seconds is None:
        return
    for name in experiment.profiles():
        interval = profiles[name]["monitoring"]["scrape_interval_seconds"]
        if experiment.pod_stall_seconds < 2 * interval:
            raise ExperimentError(
                f"pod_stall_seconds {experiment.pod_stall_seconds} holds fewer than two of profile "
                f"{name}'s scrapes, {interval} s apart"
            )


def load_experiment(path: Path, patterns: Mapping[str, Pattern]) -> Experiment:
    """patterns are those of the profile the file names. A missing key raises KeyError."""
    spec = yaml.safe_load(path.read_text())
    if spec["pattern"] not in patterns:
        raise ExperimentError(
            f"no pattern {spec['pattern']} in the profile, has {sorted(patterns)}"
        )
    pattern = patterns[spec["pattern"]]
    warmup = Warmup(**spec["warmup"])
    if warmup.runs_discarded >= pattern.runs:
        raise ExperimentError(
            f"warmup.runs_discarded {warmup.runs_discarded} leaves none of the "
            f"{pattern.runs} runs of pattern {spec['pattern']}"
        )
    trace = (path.parent / spec["trace"]).resolve()
    if not trace.exists():
        raise ExperimentError(f"no trace {spec['trace']}")
    variants = [_variant(path.parent, spec, section) for section in spec["variants"]]
    names = [variant.name for variant in variants]
    for name in names:
        if names.count(name) > 1:
            raise ExperimentError(f"two variants named {name}: their results would mix")
    validation = None
    if spec.get("validation") is not None:
        validation = load_validation(spec["validation"], names)
    tolerance, reference = _not_worse(spec, variants)
    return Experiment(
        name=path.stem,
        hash=experiment_hash(path),
        profile=spec.get("profile"),
        trace=trace,
        pattern_name=spec["pattern"],
        pattern=pattern,
        seed=spec["seed"],
        kv_capacity_tokens_per_pod=spec["kv_capacity_tokens_per_pod"],
        warmup=warmup,
        variants=variants,
        validation=validation,
        not_worse_tolerance=tolerance,
        reference_variant=reference,
        pod_stall_seconds=_pod_stall_seconds(spec),
    )


def outputs(experiment: Experiment, root: Path) -> Outputs:
    stem = f"{experiment.name}-{experiment.hash}"
    results = root / "experiments" / "results"
    return Outputs(
        csv=results / f"{stem}.csv",
        meta=results / f"{stem}.meta.json",
        graph=root / "experiments" / "graphs" / f"{stem}.png",
        validation=table_path(root, experiment.hash),
    )


def refuse_leftovers(experiment: Experiment, out: Outputs) -> None:
    """Before anything is restarted: every file a run of this experiment file would write, a replay
    that stopped included. A long run must not start only to fail at its first write."""
    stem = f"{experiment.name}-{experiment.hash}"
    found = sorted(out.csv.parent.glob(f"{stem}*")) + sorted(out.graph.parent.glob(f"{stem}*"))
    found += [out.validation] if out.validation.exists() else []
    if found:
        names = ", ".join(str(path) for path in found)
        raise ExperimentError(f"results of this file exist, delete them to run it again: {names}")


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _trace_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(text) for text in path.read_text().splitlines()]


def _variant_meta(
    variant: Variant,
    profile_sha256: str,
    warmup_ttft: Mapping[str, list[int]],
    lines: Sequence[Mapping[str, Any]],
    trace: Sequence[Mapping[str, Any]],
    starts: Sequence[float],
    times: tuple[float, float],
    clock_jumps: int,
    picker_log: str | None,
) -> dict[str, Any]:
    return {
        "epp_config": variant.epp_config,
        # The file's hash covers it too; here it is beside the result it explains.
        "routing": variant.routing,
        "profile": variant.profile,
        "replicas": variant.replicas,
        "profile_sha256": profile_sha256,
        "warmup_ttft_ms": warmup_ttft,
        "started": times[0],
        "ended": times[1],
        # A slowed host once looked like a routing difference (P2.3).
        "clock_jumps": clock_jumps,
        "calls": len(lines),
        "failed_calls": failed_calls(lines),
        "calls_per_pod": calls_per_pod(lines),
        "pod_switch_share": pod_switch_share(lines, starts),
        "max_gap_deviation_ms": max_gap_deviation(trace, lines),
        # The file name beside the results, None for a variant without the plugin.
        "picker_log": picker_log,
    }


def stall_text(variant: str, stalls: Sequence[PodStall], started: float) -> str:
    found = ", ".join(
        f"{stall.pod} made no tokens with calls running or waiting for {stall.seconds} s from "
        f"{stall.start - started:.0f} s into the replay"
        for stall in stalls
    )
    return (
        f"variant {variant}: {found}. Such stalls hit both pickers on 2026-11-12 (P3.7, "
        "experiments/log.md), so this replay is not comparable; its replay file stays, move this "
        "file's results away to run it again"
    )


def _run_variant(
    experiment: Experiment,
    variant: Variant,
    out: Outputs,
    cluster: Cluster,
    clock: Callable[[], float],
    profile_sha256: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    running = cluster.epp_config_file()
    if running != variant.epp_config:
        raise ExperimentError(
            f"variant {variant.name} wants the picker on {variant.epp_config}, it runs {running}"
        )
    warmup_ttft = cluster.warm_pods()
    started = clock()
    lines = cluster.replay(out.replay(variant.name), variant.routing)
    ended = clock()
    jumps = cluster.clock_jumps(started, ended)
    picker_log = None
    if variant.epp_config == PLUGIN_CONFIG:
        out.picker(variant.name).write_text(cluster.picker_log())
        picker_log = out.picker(variant.name).name
    if experiment.pod_stall_seconds is not None:
        stalls = cluster.pod_stalls(started, ended, experiment.pod_stall_seconds)
        if stalls:
            raise ExperimentError(stall_text(variant.name, stalls, started))
    meta = _variant_meta(
        variant,
        profile_sha256,
        warmup_ttft,
        lines,
        _trace_lines(experiment.trace),
        start_times(experiment.pattern, experiment.seed),
        (started, ended),
        jumps,
        picker_log,
    )
    return lines, meta


def run_experiment(
    experiment: Experiment,
    out: Outputs,
    cluster: Cluster,
    clock: Callable[[], float],
    finished: Callable[[list[CallRow], dict[str, Any]], None] | None = None,
) -> tuple[list[CallRow], dict[str, Any]]:
    """Every variant's rows, and the .meta.json's content. Each replay's file is written as it ends,
    so a later failure keeps the earlier variants' raw lines. finished gets the results before the
    default picker is put back, so a switch that fails after the last variant does not lose them."""
    out.csv.parent.mkdir(parents=True, exist_ok=True)
    cluster.check_ready()
    if experiment.gpu_logged:
        cluster.check_gpu()
    # Now and not at the end: the profiles were loaded just before, and a run takes hours.
    profile_sha256 = {
        name: _file_sha256(REPO_ROOT / "config" / f"{name}.yaml") for name in experiment.profiles()
    }
    starts = start_times(experiment.pattern, experiment.seed)
    rows: list[CallRow] = []
    variants_meta: dict[str, Any] = {}
    # True from a switch away from the default until a switch back has finished: a switch that fails
    # halfway may have changed the picker.
    off_default = False
    # The profile up now: the file's is up before the run, a variant's is deployed by the run.
    deployed = experiment.profile
    # True once the deployed profile runs another count than its own.
    scaled = False
    # True from the GPU log's start until it is stopped.
    gpu_logging = False
    try:
        if experiment.gpu_logged:
            cluster.start_gpu_log(out.gpu)
            gpu_logging = True
        for variant in experiment.variants:
            if variant.profile != deployed:
                # The release it scaled is being replaced, so there is nothing to scale back.
                scaled = False
                cluster.deploy(variant.profile)
                deployed = variant.profile
            if variant.replicas is not None:
                cluster.scale(variant.replicas)
                scaled = True
            cluster.restart_pods()
            off_default = off_default or not variant.default
            cluster.set_routing(variant.values, variant.routing)
            off_default = not variant.default
            lines, variant_meta = _run_variant(
                experiment, variant, out, cluster, clock, profile_sha256[variant.profile]
            )
            rows += call_rows(
                variant.name,
                lines,
                starts,
                experiment.kv_capacity_tokens_per_pod,
                experiment.warmup.runs_discarded,
            )
            variants_meta[variant.name] = variant_meta
        gpu_ended_early_with = None
        if gpu_logging:
            gpu_logging = False
            gpu_ended_early_with = cluster.stop_gpu_log()
        meta = {
            "experiment": experiment.name,
            "hash": experiment.hash,
            "profile": experiment.profile,
            # The hash covers the experiment file only, and its pattern and prompts come from here.
            # Each variant's own is in its meta.
            "profile_sha256": (
                None if experiment.profile is None else profile_sha256[experiment.profile]
            ),
            "trace": experiment.trace.name,
            "pattern": experiment.pattern_name,
            "seed": experiment.seed,
            "runs_discarded": experiment.warmup.runs_discarded,
            "kv_capacity_tokens_per_pod": experiment.kv_capacity_tokens_per_pod,
            "gpu_log": out.gpu.name if experiment.gpu_logged else None,
            # A gap at the end of the GPU log, which its timestamps alone would not explain.
            "gpu_log_ended_early_with": gpu_ended_early_with,
            "not_worse_tolerance": experiment.not_worse_tolerance,
            "reference_variant": experiment.reference_variant,
            # Every replay was checked and had no pod stalled this long; None when not checked.
            "pod_stall_seconds": experiment.pod_stall_seconds,
            "variants": variants_meta,
        }
        if finished is not None:
            finished(rows, meta)
    finally:
        if gpu_logging:
            cluster.stop_gpu_log()
        if off_default:
            cluster.set_routing(None, None)
        if scaled:
            cluster.scale(None)
    return rows, meta


def write_results(out: Outputs, rows: Sequence[CallRow], meta: Mapping[str, Any]) -> None:
    write_csv(out.csv, rows)
    with out.meta.open("x") as file:
        json.dump(meta, file, indent=2)
        file.write("\n")
    render(out.csv, out.graph)


def pods_to_warm(pods: Mapping[str, Any], replicas: int) -> dict[str, str]:
    """Pod name -> IP of the Ready pods from kubectl get pods -o json. Pods left Failed after a
    reboot are not among them (P2.11). Fewer than the profile's replicas would be another load."""
    ready = {
        pod["metadata"]["name"]: pod["status"]["podIP"]
        for pod in pods["items"]
        if any(
            condition["type"] == "Ready" and condition["status"] == "True"
            for condition in pod["status"].get("conditions", [])
        )
    }
    if len(ready) != replicas:
        raise ExperimentError(f"{sorted(ready)} are Ready, the profile has {replicas} replicas")
    return ready


async def warm_pod(
    client: httpx.AsyncClient, model: str, run: PlannedRun, deadline_seconds: float
) -> list[int]:
    """A pod's warm-up calls, one at a time, and each one's time to first token."""
    lines = await replay_runs(client, model, [run], deadline_seconds, lambda _: None)
    answered = [line["ttft_ms"] for line in lines if "ttft_ms" in line]
    if len(answered) != len(run.steps):
        raise ExperimentError(f"{run.run_id}: a warm-up call got no full reply")
    return answered


def served_config_file(arguments: str) -> str:
    """The file a picker's --config-file names, from its Deployment's args as JSON."""
    args: list[str] = json.loads(arguments)
    if "--config-file" not in args:
        raise ExperimentError(f"the picker has no --config-file argument: {args}")
    return Path(args[args.index("--config-file") + 1]).name


def _journal(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["journalctl", "-b", "0", "-q", *args], stdout=subprocess.PIPE, text=True, check=False
    )


def check_journal_readable() -> None:
    """journalctl prints nothing and exits 0 or 1 without the rights to read, which would read as
    no clock jumps at all, so this is checked before a long run relies on it."""
    if not _journal("-n", "1").stdout.strip():
        raise ExperimentError("journalctl shows nothing, so clock jumps could not be counted")


def count_clock_jumps(since: float, until: float) -> int:
    """Whole seconds, widened outward, so a jump at the edge of the window counts."""
    done = _journal("-S", f"@{int(since)}", "-U", f"@{int(until) + 1}", "-g", CLOCK_JUMP_LINE)
    if done.returncode not in JOURNAL_FOUND_OR_NONE:
        raise ExperimentError(f"journalctl exited with {done.returncode}")
    return sum(CLOCK_JUMP_LINE in line for line in done.stdout.splitlines())


def pod_stalls_from(
    busy: Sequence[Mapping[str, Any]],
    tokens: Sequence[Mapping[str, Any]],
    window: int,
    step: int,
) -> list[PodStall]:
    """The stalls in BUSY_QUERY's and TOKENS_QUERY's range answers. A pod with calls and no token
    series at all is an error, not a pod without stalls: a renamed counter would pass every replay.
    A single sample with no token reading beside it, or not a number, is no evidence of a stall.
    Stalled samples one step apart are one stall, which began a window before its first sample."""
    counted = {series["metric"]["pod"] for series in tokens}
    unread = sorted({series["metric"]["pod"] for series in busy} - counted)
    if unread:
        raise ExperimentError(
            f"Prometheus has no token count for {', '.join(unread)}, so a stalled pod could not be "
            "ruled out"
        )
    made = {
        (series["metric"]["pod"], float(at)): float(value)
        for series in tokens
        for at, value in series["values"]
    }
    found = []
    for series in busy:
        pod = series["metric"]["pod"]
        stalled = [
            float(at)
            for at, value in series["values"]
            if float(value) > 0 and made.get((pod, float(at))) == 0
        ]
        runs: list[list[float]] = []
        for at in stalled:
            # Under two steps: the next sample, whatever the rounding of its timestamp.
            if runs and at - runs[-1][-1] < 2 * step:
                runs[-1].append(at)
            else:
                runs.append([at])
        found += [PodStall(pod, run[0] - window, round(run[-1] - run[0]) + window) for run in runs]
    return found


def set_routing(profile: str, values: Path | None, routing: Mapping[str, bool] | None) -> None:
    """The picker on the profile's routing, with Helm values or rules laid over it; both None is
    the profile's own. set-routing.sh takes one file: Helm values, or rules under a top-level
    routing key. make pod-removed switches the picker this way too (P3.4)."""
    script = str(REPO_ROOT / "infra" / "set-routing.sh")
    if routing is None:
        extra = [] if values is None else [str(values)]
        subprocess.run(["bash", script, profile, *extra], check=True)
        return
    with tempfile.NamedTemporaryFile("w", suffix=".yaml") as rules:
        yaml.safe_dump({"routing": dict(routing)}, rules)
        rules.flush()
        subprocess.run(["bash", script, profile, rules.name], check=True)


class LiveCluster:
    def __init__(self, experiment: Experiment, profiles: Mapping[str, Mapping[str, Any]]) -> None:
        self.experiment = experiment
        self.profiles = profiles
        # The profile up now; None until the first deploy of a file whose variants name their own.
        self.current = experiment.profile
        # None is the profile's own count.
        self.replicas: int | None = None
        # nvidia-smi while it samples the GPU.
        self.gpu: subprocess.Popen[bytes] | None = None

    @property
    def profile(self) -> Mapping[str, Any]:
        if self.current is None:
            raise ExperimentError("no profile is deployed yet")
        return self.profiles[self.current]

    def _ready_pods(self) -> dict[str, str]:
        pods = json.loads(kubectl("get", "pods", "-l", VLLM_SELECTOR, "-o", "json"))
        return pods_to_warm(pods, self.replicas or self.profile["replicas"])

    def check_ready(self) -> None:
        """Before a first deploy, whatever runs is about to be replaced, so only the journal."""
        if self.current is not None:
            self._ready_pods()
        check_journal_readable()

    def deploy(self, profile: str) -> None:
        """The vLLM release only: the gateway, picker and monitoring stay, and set_routing points
        the picker's probe at the new profile's model. The old Deployment must be gone before the
        install: helm 4's uninstall --wait returned while it was still being deleted, and the
        install then applied over it and failed on kubectl scale's .spec.replicas (P2.9's rerun).
        Foreground, so it goes only after its pods: they carry the label the picker selects by, and
        a real one holds the GPU. kubectl wait --for=delete returns at once when it is gone."""
        subprocess.run(
            [
                "helm",
                "uninstall",
                "vllm",
                "--ignore-not-found",
                "--cascade",
                "foreground",
                "--wait",
                "--timeout",
                UNINSTALL_TIMEOUT,
            ],
            check=True,
        )
        kubectl("wait", VLLM_DEPLOYMENT, "--for=delete", f"--timeout={UNINSTALL_TIMEOUT}")
        target = ["sim-deploy"] if profile == SIM_PROFILE else ["vllm-deploy", f"PROFILE={profile}"]
        subprocess.run(["make", *target], cwd=REPO_ROOT, check=True)
        self.current = profile
        self.replicas = None

    def scale(self, replicas: int | None) -> None:
        """As make calibrate's: status.replicas leaves out pods being deleted, so once it is the
        count the ReplicaSet has chosen which to delete; they are waited for, since a real pod holds
        the GPU until gone."""
        count = replicas or self.profile["replicas"]
        timeout = f"--timeout={SCALE_TIMEOUT}"
        kubectl("scale", VLLM_DEPLOYMENT, f"--replicas={count}")
        kubectl("wait", VLLM_DEPLOYMENT, f"--for=jsonpath={{.status.replicas}}={count}", timeout)
        pods = json.loads(kubectl("get", "pods", "-l", VLLM_SELECTOR, "-o", "json"))
        for pod in pods["items"]:
            if "deletionTimestamp" in pod["metadata"]:
                kubectl("wait", f"pod/{pod['metadata']['name']}", "--for=delete", timeout)
        kubectl("rollout", "status", VLLM_DEPLOYMENT, timeout)
        self.replicas = replicas

    def restart_pods(self) -> None:
        subprocess.run(["bash", str(REPO_ROOT / "infra" / "restart-vllm.sh")], check=True)

    def set_routing(self, values: Path | None, routing: Mapping[str, bool] | None) -> None:
        if self.current is None:
            raise ExperimentError("no profile is deployed yet")
        set_routing(self.current, values, routing)

    def epp_config_file(self) -> str:
        return served_config_file(
            kubectl(
                "get",
                "deployment",
                EPP_DEPLOYMENT,
                "-o",
                "jsonpath={.spec.template.spec.containers[0].args}",
            )
        )

    def warm_pods(self) -> dict[str, list[int]]:
        return asyncio.run(self._warm(self._ready_pods()))

    async def _warm(self, pods: Mapping[str, str]) -> dict[str, list[int]]:
        warmup = self.experiment.warmup
        times = {}
        for number, (name, ip) in enumerate(sorted(pods.items()), start=1):
            run = warmup_run(number, warmup.calls_per_pod, warmup.words, warmup.completion_tokens)
            # Each call has its own deadline, which covers connecting too.
            async with httpx.AsyncClient(
                base_url=f"http://{ip}:{VLLM_PORT}", timeout=None
            ) as client:
                times[name] = await warm_pod(
                    client,
                    served_model(self.profile["model"]),
                    run,
                    self.profile["agent"]["llm_timeout_seconds"],
                )
            print(f"experiment: warmed {name}, ms to first token {times[name]}", flush=True)
        return times

    def replay(self, out: Path, routing: Mapping[str, bool] | None) -> list[dict[str, Any]]:
        return replay_to_file(
            self.profile,
            self.experiment.trace,
            self.experiment.pattern_name,
            self.experiment.seed,
            out,
            plugin_on(self.profile["routing"], routing),
        )

    def clock_jumps(self, since: float, until: float) -> int:
        return count_clock_jumps(since, until)

    def check_gpu(self) -> None:
        """One sample of the fields the log takes: a field this driver does not know, or no GPU,
        fails here and not hours later in an empty log."""
        done = subprocess.run(
            ["nvidia-smi", f"--query-gpu={GPU_FIELDS}", "--format=csv,noheader"],
            stdout=subprocess.PIPE,
            text=True,
            check=False,
        )
        if done.returncode != 0 or not done.stdout.strip():
            raise ExperimentError(
                f"nvidia-smi exited with {done.returncode} and printed {done.stdout!r}, so the GPU "
                "could not be logged"
            )

    def start_gpu_log(self, path: Path) -> None:
        """As often as Prometheus reads the pods, so a sample lines up with a queue reading. -f has
        nvidia-smi write the file itself, a whole line per sample, so stopping it loses none."""
        real = next(name for name in self.experiment.profiles() if name != SIM_PROFILE)
        interval = self.profiles[real]["monitoring"]["scrape_interval_seconds"]
        self.gpu = subprocess.Popen(
            [
                "nvidia-smi",
                f"--query-gpu={GPU_FIELDS}",
                "--format=csv",
                "-l",
                str(interval),
                "-f",
                str(path),
            ]
        )

    def stop_gpu_log(self) -> int | None:
        if self.gpu is None:
            raise ExperimentError("the GPU log was not started")
        ended_with = self.gpu.poll()
        if ended_with is None:
            self.gpu.terminate()
            self.gpu.wait()
        self.gpu = None
        return ended_with

    def picker_log(self) -> str:
        """Since the picker's start, which the variant's switch was."""
        return kubectl("logs", f"deployment/{EPP_DEPLOYMENT}")

    def _prometheus(self, query: str, since: float, until: float, step: int) -> list[Any]:
        arguments = urllib.parse.urlencode(
            {"query": query, "start": f"{since:.3f}", "end": f"{until:.3f}", "step": step}
        )
        answer = json.loads(kubectl("get", "--raw", f"{PROMETHEUS_RANGE_API}?{arguments}"))
        if answer["status"] != "success":
            raise ExperimentError(f"Prometheus answered {answer.get('error')} to {query}")
        result: list[Any] = answer["data"]["result"]
        return result

    def pod_stalls(self, since: float, until: float, seconds: int) -> list[PodStall]:
        """At the profile's scrape interval, so each sample is a reading. No pod at all means
        Prometheus did not read them, which must not pass as no stall."""
        step = self.profile["monitoring"]["scrape_interval_seconds"]
        busy = self._prometheus(BUSY_QUERY.format(window=seconds), since, until, step)
        if not busy:
            raise ExperimentError(
                "Prometheus has no vLLM pod for the replay, so a stalled pod could not be ruled out"
            )
        tokens = self._prometheus(TOKENS_QUERY.format(window=seconds), since, until, step)
        return pod_stalls_from(busy, tokens, seconds, step)


def _seconds_text(milliseconds: int | None) -> str:
    return "-" if milliseconds is None else f"{milliseconds / 1000:.2f} s"


def warmed_ttft_ms(warmup_ttft: Mapping[str, Sequence[int]]) -> float | None:
    """The mean time to first token of the warm-up calls after each pod's first. They go straight to
    the pods, so they show the host's speed with no picker involved."""
    times = [time for pod_times in warmup_ttft.values() for time in pod_times[1:]]
    return sum(times) / len(times) if times else None


def _number_text(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def short_of_cache(rows: Sequence[CallRow]) -> tuple[int, int]:
    """Of a variant's later steps that stayed on their pod and have a cached count, how many found
    less than their run's previous prompt less CACHE_SHORTFALL_TOKENS cached, and how many there
    are. The calls of a fanned-out step send one prompt, so the step's first row has it."""
    previous: dict[tuple[str, int], int] = {}
    for row in rows:
        previous.setdefault((row.run, row.step), row.prompt_tokens)
    checked = [
        row
        for row in rows
        if row.moved is False
        and row.cached_tokens is not None
        and (row.run, row.step - 1) in previous
    ]
    short = sum(
        row.cached_tokens is not None
        and row.cached_tokens < previous[(row.run, row.step - 1)] - CACHE_SHORTFALL_TOKENS
        for row in checked
    )
    return short, len(checked)


def report_lines(rows: Sequence[CallRow], meta: Mapping[str, Any]) -> list[str]:
    """The numbers to read first. Moved and stayed sit inside the KV split, as P2.4 asks: over the
    limit a call is slow wherever it goes, which must not read as the cost of moving."""
    report = []
    summaries = summarise(rows)
    for variant, calls in variant_rows(rows).items():
        summary = summaries[variant]
        extra = meta["variants"][variant]
        times = summary.completions_ms
        middle = percentile(times, 0.5) if times else None
        hit_rate = "-" if summary.hit_rate is None else f"{summary.hit_rate:.3f}"
        report.append(
            f"{variant}: {summary.runs - summary.failed_runs} of {summary.runs} runs finished, "
            f"{summary.calls} calls, {summary.failed_calls} failed; run time median "
            f"{_seconds_text(middle)}; hit rate {hit_rate}; moved {summary.moved} of "
            f"{summary.later_steps} later steps"
        )
        report.append(
            f"  picks {extra['calls_per_pod']}, share that switched pod "
            f"{extra['pod_switch_share']}; clock jumps {extra['clock_jumps']}; largest gap "
            f"deviation {extra['max_gap_deviation_ms']} ms"
        )
        generation = _number_text(summary.generation_ms_per_token)
        warmed = _number_text(warmed_ttft_ms(extra["warmup_ttft_ms"]))
        report.append(
            f"  speed, mostly the host's: generation {generation} ms per token, warm-up calls "
            f"{warmed} ms to first token"
        )
        short, checked = short_of_cache(calls)
        report.append(
            f"  stayed later steps short of their previous prompt in cache: {short} of {checked}"
        )
        for limit, over in (("under", False), ("over", True)):
            for label, moved in (("all", None), ("stayed", False), ("moved", True)):
                stats = ttft_stats(calls, over, moved)
                report.append(
                    f"  {limit} the KV limit, {label}: {stats.calls} calls, TTFT p50 "
                    f"{_seconds_text(stats.p50_ms)}, p95 {_seconds_text(stats.p95_ms)}"
                )
    return report


def finish(
    experiment: Experiment, out: Outputs, rows: Sequence[CallRow], meta: Mapping[str, Any]
) -> None:
    """The results, the validation table if the file has one, and the numbers to read first."""
    write_results(out, rows, meta)
    for line in report_lines(rows, meta):
        print(line)
    print(f"experiment: wrote {out.csv}, {out.meta} and {out.graph}")
    if meta["gpu_log"] is not None:
        print(f"experiment: GPU log {out.gpu}")
    if meta["gpu_log_ended_early_with"] is not None:
        print(
            f"experiment: nvidia-smi ended early with exit {meta['gpu_log_ended_early_with']}, so "
            "the GPU log stops before the run did"
        )
    if experiment.validation is not None:
        entries = table(rows, experiment.validation)
        write_table(out.validation, entries)
        for line in validation_lines(entries):
            print(line)
        print(f"experiment: wrote {out.validation}")


def main() -> None:
    parser = argparse.ArgumentParser(description="P2.5: run an experiment file.")
    parser.add_argument("experiment", type=Path)
    args = parser.parse_args()

    spec = yaml.safe_load(args.experiment.read_text())
    names = dict.fromkeys(variant_profile(spec, section) for section in spec["variants"])
    profiles = {
        name: yaml.safe_load((REPO_ROOT / "config" / f"{name}.yaml").read_text()) for name in names
    }
    check_same_replay(profiles, spec["pattern"])
    first = next(iter(profiles.values()))
    patterns = load_replay_config(first["replay"]).patterns
    experiment = load_experiment(args.experiment, patterns)
    check_routing(experiment, profiles)
    check_stall_window(experiment, profiles)
    out = outputs(experiment, REPO_ROOT)
    refuse_leftovers(experiment, out)

    def finished(rows: Sequence[CallRow], meta: Mapping[str, Any]) -> None:
        finish(experiment, out, rows, meta)

    run_experiment(experiment, out, LiveCluster(experiment, profiles), time.time, finished)


if __name__ == "__main__":
    main()
