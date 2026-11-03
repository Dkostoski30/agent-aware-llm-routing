"""make experiment (P2.5): one experiment file's variants, as one CSV, .meta.json and graph.

For each variant, in the file's order: restart the vLLM pods, put the picker on the variant, warm
each pod, replay. Both variants send identical prompts at one seed, so without the restart the
second would find the first's text cached. A failed call is a result, not a stop: a replay runs
under overload on purpose. After any failure the default picker is put back, since a variant left
behind would silently change every later run; if even that fails, the run ends with that error.

Usage: python -m load_generator.experiment EXPERIMENT
"""

import argparse
import asyncio
import hashlib
import json
import subprocess
import time
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

VLLM_SELECTOR = "app.kubernetes.io/name=vllm"
EPP_DEPLOYMENT = "vllm-pool-epp"
CLOCK_JUMP_LINE = "Clock change detected"
# journalctl's exit codes with --grep: 0 found lines, 1 found none. Anything else is an error.
JOURNAL_FOUND_OR_NONE = (0, 1)


class ExperimentError(Exception):
    """The experiment must not start or go on: its results would not mean what they say."""


@dataclass(frozen=True)
class Warmup:
    runs_discarded: int
    calls_per_pod: int
    words: int
    completion_tokens: int


@dataclass(frozen=True)
class Variant:
    name: str
    # On top of infra/helm/router-values.yaml; None is the default picker.
    values: Path | None
    # The file the running picker must name.
    epp_config: str


@dataclass(frozen=True)
class Experiment:
    # The file's stem, which names its results.
    name: str
    hash: str
    profile: str
    trace: Path
    pattern_name: str
    pattern: Pattern
    seed: int
    kv_capacity_tokens_per_pod: int
    warmup: Warmup
    variants: list[Variant]


@dataclass(frozen=True)
class Outputs:
    csv: Path
    meta: Path
    graph: Path

    def replay(self, variant: str) -> Path:
        return self.csv.with_name(f"{self.csv.stem}-{variant}.jsonl")


class Cluster(Protocol):
    """What an experiment asks of the cluster; LiveCluster is the real one."""

    def check_ready(self) -> None: ...

    def restart_pods(self) -> None: ...

    def set_routing(self, values: Path | None) -> None: ...

    def epp_config_file(self) -> str: ...

    def warm_pods(self) -> dict[str, list[int]]: ...

    def replay(self, out: Path) -> list[dict[str, Any]]: ...

    def clock_jumps(self, since: float, until: float) -> int: ...


def _variant(directory: Path, section: Mapping[str, Any]) -> Variant:
    values = None
    if section["values"] is not None:
        values = (directory / section["values"]).resolve()
        if not values.exists():
            raise ExperimentError(f"variant {section['name']}: no file {section['values']}")
    return Variant(section["name"], values, section["epp_config"])


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
    variants = [_variant(path.parent, section) for section in spec["variants"]]
    names = [variant.name for variant in variants]
    for name in names:
        if names.count(name) > 1:
            raise ExperimentError(f"two variants named {name}: their results would mix")
    return Experiment(
        name=path.stem,
        hash=experiment_hash(path),
        profile=spec["profile"],
        trace=trace,
        pattern_name=spec["pattern"],
        pattern=pattern,
        seed=spec["seed"],
        kv_capacity_tokens_per_pod=spec["kv_capacity_tokens_per_pod"],
        warmup=warmup,
        variants=variants,
    )


def outputs(experiment: Experiment, root: Path) -> Outputs:
    stem = f"{experiment.name}-{experiment.hash}"
    results = root / "experiments" / "results"
    return Outputs(
        csv=results / f"{stem}.csv",
        meta=results / f"{stem}.meta.json",
        graph=root / "experiments" / "graphs" / f"{stem}.png",
    )


def refuse_leftovers(experiment: Experiment, out: Outputs) -> None:
    """Before anything is restarted: every file a run of this experiment file would write, a replay
    that stopped included. A long run must not start only to fail at its first write."""
    stem = f"{experiment.name}-{experiment.hash}"
    found = sorted(out.csv.parent.glob(f"{stem}*")) + sorted(out.graph.parent.glob(f"{stem}*"))
    if found:
        names = ", ".join(str(path) for path in found)
        raise ExperimentError(f"results of this file exist, delete them to run it again: {names}")


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _trace_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(text) for text in path.read_text().splitlines()]


def _variant_meta(
    variant: Variant,
    warmup_ttft: Mapping[str, list[int]],
    lines: Sequence[Mapping[str, Any]],
    trace: Sequence[Mapping[str, Any]],
    starts: Sequence[float],
    times: tuple[float, float],
    clock_jumps: int,
) -> dict[str, Any]:
    return {
        "epp_config": variant.epp_config,
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
    }


def _run_variant(
    experiment: Experiment,
    variant: Variant,
    out: Outputs,
    cluster: Cluster,
    clock: Callable[[], float],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    running = cluster.epp_config_file()
    if running != variant.epp_config:
        raise ExperimentError(
            f"variant {variant.name} wants the picker on {variant.epp_config}, it runs {running}"
        )
    warmup_ttft = cluster.warm_pods()
    started = clock()
    lines = cluster.replay(out.replay(variant.name))
    ended = clock()
    meta = _variant_meta(
        variant,
        warmup_ttft,
        lines,
        _trace_lines(experiment.trace),
        start_times(experiment.pattern, experiment.seed),
        (started, ended),
        cluster.clock_jumps(started, ended),
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
    # Now and not at the end: the profile was loaded just before, and a run takes hours.
    profile_sha256 = _file_sha256(REPO_ROOT / "config" / f"{experiment.profile}.yaml")
    starts = start_times(experiment.pattern, experiment.seed)
    rows: list[CallRow] = []
    variants_meta: dict[str, Any] = {}
    # True from a switch away from the default until a switch back has finished: a switch that fails
    # halfway may have changed the picker.
    off_default = False
    try:
        for variant in experiment.variants:
            cluster.restart_pods()
            off_default = off_default or variant.values is not None
            cluster.set_routing(variant.values)
            off_default = variant.values is not None
            lines, variant_meta = _run_variant(experiment, variant, out, cluster, clock)
            rows += call_rows(
                variant.name,
                lines,
                starts,
                experiment.kv_capacity_tokens_per_pod,
                experiment.warmup.runs_discarded,
            )
            variants_meta[variant.name] = variant_meta
        meta = {
            "experiment": experiment.name,
            "hash": experiment.hash,
            "profile": experiment.profile,
            # The hash covers the experiment file only, and its pattern and prompts come from here.
            "profile_sha256": profile_sha256,
            "trace": experiment.trace.name,
            "pattern": experiment.pattern_name,
            "seed": experiment.seed,
            "runs_discarded": experiment.warmup.runs_discarded,
            "kv_capacity_tokens_per_pod": experiment.kv_capacity_tokens_per_pod,
            "variants": variants_meta,
        }
        if finished is not None:
            finished(rows, meta)
    finally:
        if off_default:
            cluster.set_routing(None)
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


class LiveCluster:
    def __init__(self, experiment: Experiment, profile: Mapping[str, Any]) -> None:
        self.experiment = experiment
        self.profile = profile

    def _ready_pods(self) -> dict[str, str]:
        pods = json.loads(kubectl("get", "pods", "-l", VLLM_SELECTOR, "-o", "json"))
        return pods_to_warm(pods, self.profile["replicas"])

    def check_ready(self) -> None:
        self._ready_pods()
        check_journal_readable()

    def restart_pods(self) -> None:
        subprocess.run(["bash", str(REPO_ROOT / "infra" / "restart-vllm.sh")], check=True)

    def set_routing(self, values: Path | None) -> None:
        script = str(REPO_ROOT / "infra" / "set-routing.sh")
        extra = [] if values is None else [str(values)]
        subprocess.run(["bash", script, self.experiment.profile, *extra], check=True)

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

    def replay(self, out: Path) -> list[dict[str, Any]]:
        return replay_to_file(
            self.profile,
            self.experiment.trace,
            self.experiment.pattern_name,
            self.experiment.seed,
            out,
        )

    def clock_jumps(self, since: float, until: float) -> int:
        return count_clock_jumps(since, until)


def _seconds_text(milliseconds: int | None) -> str:
    return "-" if milliseconds is None else f"{milliseconds / 1000:.2f} s"


def warmed_ttft_ms(warmup_ttft: Mapping[str, Sequence[int]]) -> float | None:
    """The mean time to first token of the warm-up calls after each pod's first. They go straight to
    the pods, so they show the host's speed with no picker involved."""
    times = [time for pod_times in warmup_ttft.values() for time in pod_times[1:]]
    return sum(times) / len(times) if times else None


def _number_text(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


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
        for limit, over in (("under", False), ("over", True)):
            for label, moved in (("all", None), ("stayed", False), ("moved", True)):
                stats = ttft_stats(calls, over, moved)
                report.append(
                    f"  {limit} the KV limit, {label}: {stats.calls} calls, TTFT p50 "
                    f"{_seconds_text(stats.p50_ms)}, p95 {_seconds_text(stats.p95_ms)}"
                )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="P2.5: run an experiment file.")
    parser.add_argument("experiment", type=Path)
    args = parser.parse_args()

    spec = yaml.safe_load(args.experiment.read_text())
    profile = yaml.safe_load((REPO_ROOT / "config" / f"{spec['profile']}.yaml").read_text())
    patterns = load_replay_config(profile["replay"]).patterns
    experiment = load_experiment(args.experiment, patterns)
    out = outputs(experiment, REPO_ROOT)
    refuse_leftovers(experiment, out)

    def finished(rows: Sequence[CallRow], meta: Mapping[str, Any]) -> None:
        write_results(out, rows, meta)
        for line in report_lines(rows, meta):
            print(line)
        print(f"experiment: wrote {out.csv}, {out.meta} and {out.graph}")

    run_experiment(experiment, out, LiveCluster(experiment, profile), time.time, finished)


if __name__ == "__main__":
    main()
