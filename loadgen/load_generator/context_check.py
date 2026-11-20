"""make context-check (P4.10): the replayer's thesis_context_tokens against a replay's file.

Run after a replay, with the cluster still up. It waits until a few scrapes past the replay's last
call, reads every scrape of the replayer's series from one scrape before the first call to then, and
checks each against the sum the replay's file gives at the same moments:

- the highest reading equals the file's sum at some moment within one scrape of it;
- a reading with no run live in the file within one scrape of it is 0;
- a reading with runs live all through one scrape on either side is above 0;
- no reading is left after the replay: the replayer's /metrics closes, and Prometheus's next failed
  scrape ends the series.

The readings are Prometheus's raw samples at their own scrape times, not a range query's values on
a grid: a range query's value at t is the latest scrape before t, and scrapes slip, up to 7.6 s
apart in P4.10's first run. There, judged on the grid, two readings failed: one was a scrape 6.9 s
old, and one was judged against a run end the clock had moved. Only the replayer's series is read,
since the file holds the replayer's runs alone; on real the agent serves its own. "Within one
scrape" is either side: a scrape's time is its start, and the file's call ends, a wall-clock start
plus a monotonic duration, lag the wall clock whenever WSL steps it during the call, +1.7 to +2.0 s
a step, 6 steps in that run's 18 minutes.

The file's sum at a moment is what P4.9's pressure.py sums, on the wall clock: each live run's
latest step begun, its prompt tokens once however many calls it fanned out to. A run is live from
its first call's start to its last call's end, or to the start of a failed call, where the tracker
holds it until the step's other calls are done (card 54). ts_start, the wall clock, is read here, as
the samples carry the same VM's clock (P3.10); a retried call's line starts at its last attempt,
while the tracker counted it from the first.

Usage: python -m load_generator.context_check REPLAY [--profile real]
"""

import argparse
import json
import math
import time
import urllib.parse
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from load_generator.record import REPO_ROOT, kubectl
from load_generator.results import Line

# The replayer's series: Prometheus names the job after the Service its ServiceMonitor selects
# (infra/manifests/replayer-metrics.yaml).
SERIES = 'thesis_context_tokens{job="replayer-metrics"}'
# Prometheus's instant query through the API server's proxy, as experiment.py's range query.
PROMETHEUS_QUERY_API = (
    "/api/v1/namespaces/monitoring/services/monitoring-kube-prometheus-prometheus:9090"
    "/proxy/api/v1/query"
)
# How long after the replay's last call the series may still read: the replayer closes /metrics
# right after it, the scrape after that fails and ends the series, so two scrapes cover the
# scrape that was due and the one that fails.
AFTER_SCRAPES = 2
# How far past the last call the check reads, two scrapes beyond AFTER_SCRAPES, so a series left
# over would show.
READ_SCRAPES = 4


class ContextCheckError(Exception):
    pass


@dataclass(frozen=True)
class Sample:
    at_ms: int
    tokens: int


def _runs(lines: Sequence[Line]) -> dict[str, list[Line]]:
    runs: dict[str, list[Line]] = defaultdict(list)
    for line in lines:
        runs[line["run_id"]].append(line)
    return runs


def _end(line: Line) -> int:
    """A failed call's line has no duration; the run stops at its start, as in P4.9."""
    start: int = line["ts_start"]
    return start + line["duration_ms"] if "duration_ms" in line else start


def _live_until(calls: Sequence[Line]) -> int:
    failed: list[int] = [call["ts_start"] for call in calls if "duration_ms" not in call]
    if failed:
        return min(failed)
    return max(_end(call) for call in calls)


def _held(calls: Sequence[Line], at_ms: int) -> int:
    """The run's latest step begun by at_ms, once however many calls it fanned out to; 0 when the
    run is not live then."""
    begun = [call for call in calls if call["ts_start"] <= at_ms]
    if not begun or at_ms >= _live_until(calls):
        return 0
    latest = max(call["step"] for call in begun)
    held: int = max(call["prompt_tokens"] for call in begun if call["step"] == latest)
    return held


def held_at(lines: Sequence[Line], at_ms: int) -> int:
    """The file's sum at at_ms: every live run's context."""
    return sum(_held(calls, at_ms) for calls in _runs(lines).values())


def sums_within(lines: Sequence[Line], since_ms: int, until_ms: int) -> set[int]:
    """Every value the file's sum takes from since_ms to until_ms, both included. It changes only
    when a call starts or a run stops."""
    changes = {line["ts_start"] for line in lines} | {_end(line) for line in lines}
    moments = [since_ms, *(at for at in changes if since_ms < at <= until_ms)]
    return {held_at(lines, at) for at in moments}


def replay_end_ms(lines: Sequence[Line]) -> int:
    return max(_end(line) for line in lines)


def samples_from(result: Sequence[Any]) -> list[Sample]:
    """The raw samples of SERIES: one series, or none where Prometheus has no reading."""
    if len(result) > 1:
        raise ContextCheckError(f"{SERIES} gave {len(result)} series, expected one")
    if not result:
        return []
    return [Sample(round(float(at) * 1000), int(value)) for at, value in result[0]["values"]]


@dataclass(frozen=True)
class Judgement:
    start_ms: int
    end_ms: int
    highest: Sample
    # The file's sums within one scrape of the highest reading, in order.
    highest_sums: list[int]
    # Readings with no run live within one scrape, and those of them above 0.
    idle: int
    idle_above_0: list[Sample]
    # Readings with runs live all through one scrape on either side, and those of them at 0.
    busy: int
    busy_at_0: list[Sample]
    after: list[Sample]
    # Readings equal to one of the file's sums within one scrape of them, of all readings.
    matched: int
    total: int

    def seconds(self, at_ms: int) -> str:
        """at_ms as seconds from the replay's first call."""
        return f"{(at_ms - self.start_ms) / 1000:.1f} s"

    def failures(self) -> list[str]:
        failures = []
        if self.highest.tokens == 0:
            failures.append("no reading above 0 during the replay")
        elif self.highest.tokens not in self.highest_sums:
            failures.append(
                f"the highest reading, {self.highest.tokens} at {self.seconds(self.highest.at_ms)},"
                f" is none of the file's sums within one scrape of it: {self.highest_sums}"
            )
        if self.idle_above_0:
            readings = ", ".join(
                f"{sample.tokens} at {self.seconds(sample.at_ms)}" for sample in self.idle_above_0
            )
            failures.append(
                f"{len(self.idle_above_0)} reading above 0 while no run was live within one "
                f"scrape: {readings}"
            )
        if self.busy_at_0:
            moments = ", ".join(self.seconds(sample.at_ms) for sample in self.busy_at_0)
            failures.append(
                f"{len(self.busy_at_0)} reading of 0 while runs were live all through one scrape: "
                f"at {moments}"
            )
        if self.after:
            moments = ", ".join(self.seconds(sample.at_ms) for sample in self.after)
            failures.append(
                f"{len(self.after)} reading after the replay ended at {self.seconds(self.end_ms)}: "
                f"at {moments}"
            )
        return failures


def judge(lines: Sequence[Line], samples: Sequence[Sample], interval_ms: int) -> Judgement:
    if not samples:
        raise ContextCheckError(
            f"Prometheus has no {SERIES} over the replay: no sum to check (is the replayer's "
            "ServiceMonitor up?)"
        )
    end_ms = replay_end_ms(lines)
    windows = [
        (sample, sums_within(lines, sample.at_ms - interval_ms, sample.at_ms + interval_ms))
        for sample in samples
    ]
    highest, highest_window = max(windows, key=lambda pair: pair[0].tokens)
    idle = [sample for sample, sums in windows if sums == {0}]
    busy = [sample for sample, sums in windows if 0 not in sums]
    return Judgement(
        start_ms=min(line["ts_start"] for line in lines),
        end_ms=end_ms,
        highest=highest,
        highest_sums=sorted(highest_window),
        idle=len(idle),
        idle_above_0=[sample for sample in idle if sample.tokens > 0],
        busy=len(busy),
        busy_at_0=[sample for sample in busy if sample.tokens == 0],
        after=[s for s in samples if s.at_ms > end_ms + AFTER_SCRAPES * interval_ms],
        matched=sum(1 for sample, sums in windows if sample.tokens in sums),
        total=len(samples),
    )


def scraped(since: float, until: float) -> list[Any]:
    """Every scrape of SERIES from since to until, in seconds: a range selector at until, whose
    range Prometheus takes in whole seconds, so it starts at since or just before."""
    arguments = urllib.parse.urlencode(
        {"query": f"{SERIES}[{math.ceil(until - since)}s]", "time": f"{until:.3f}"}
    )
    answer = json.loads(kubectl("get", "--raw", f"{PROMETHEUS_QUERY_API}?{arguments}"))
    if answer["status"] != "success":
        raise ContextCheckError(f"Prometheus answered {answer.get('error')} to {SERIES}")
    result: list[Any] = answer["data"]["result"]
    return result


def wait_until(seconds: float) -> None:
    """Until the wall clock reads seconds, so Prometheus has scraped past the replay."""
    time.sleep(max(0.0, seconds - time.time()))


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("replay", type=Path, help="a replay's output, from make replay")
    parser.add_argument("--profile", default="real", help="config/<profile>.yaml")
    args = parser.parse_args(argv)
    profile = yaml.safe_load((REPO_ROOT / "config" / f"{args.profile}.yaml").read_text())
    interval = profile["monitoring"]["scrape_interval_seconds"]
    lines = [json.loads(text) for text in args.replay.read_text().splitlines() if text]

    since = min(line["ts_start"] for line in lines) / 1000 - interval
    until = replay_end_ms(lines) / 1000 + READ_SCRAPES * interval
    wait_until(until)
    samples = samples_from(scraped(since, until))
    judgement = judge(lines, samples, interval * 1000)

    print(f"context-check: {judgement.total} scrapes of {SERIES}, every {interval} s")
    print("context-check: times are seconds from the replay's first call")
    print(
        f"context-check: highest {judgement.highest.tokens} at "
        f"{judgement.seconds(judgement.highest.at_ms)}; the file's sums within one scrape: "
        f"{judgement.highest_sums}"
    )
    print(
        f"context-check: {judgement.idle} readings with no run live, "
        f"{judgement.busy} with runs live"
    )
    print(
        f"context-check: {judgement.matched} of {judgement.total} readings equal one of the file's "
        "sums within one scrape"
    )
    failures = judgement.failures()
    for failure in failures:
        print(f"context-check: {failure}")
    if failures:
        print("context-check: failed")
        raise SystemExit(1)
    print("context-check: passed")


if __name__ == "__main__":
    main()
