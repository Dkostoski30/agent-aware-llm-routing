"""make pressure (P4.9): cache pressure at each later-step call's start, from known lines.

The replays themselves are experiments/results/' real-pod files, which make pressure reads; here
the lines and the .meta.json are written by hand. Times are rebuilt as results.py rebuilds them:
a run's planned start, then each step's gap and duration (D9).
"""

import csv
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml
from load_generator.plan import Burst, Pattern, start_times
from load_generator.pressure import (
    CSV_FIELDS,
    BucketStats,
    PressureError,
    PressureRow,
    bucket_labels,
    bucket_of,
    bucket_stats,
    main,
    pressure_rows,
    variant_input,
    write_csv,
)

KV = 1000
EDGES = (0.75, 1.0)


def call(
    run: str,
    step: int,
    gap: int,
    prompt: int,
    duration: int | None = 1000,
    ttft: int | None = 100,
    cached: int | None = 0,
) -> dict[str, Any]:
    """A replay line. duration None is a failed call; ttft None a call with no first token."""
    line: dict[str, Any] = {
        "run_id": run,
        "task_type": run.rsplit("-", 1)[0],
        "step": step,
        "gap_before_ms": gap,
        "prompt_tokens": prompt,
        "completion_tokens": 10,
    }
    if duration is not None:
        line |= {"duration_ms": duration, "pod": "vllm-6cbf6478d6-2pj68"}
        if ttft is not None:
            line["ttft_ms"] = ttft
        if cached is not None:
            line["cached_tokens"] = cached
    return line


def rows(
    lines: Sequence[Mapping[str, Any]],
    starts: Sequence[float],
    runs_discarded: int = 0,
    capacity: int = KV,
) -> list[PressureRow]:
    found, _ = pressure_rows("exp", "v", lines, starts, runs_discarded, capacity)
    return found


def pressures(found: Sequence[PressureRow]) -> dict[tuple[str, int], list[float]]:
    by_call: dict[tuple[str, int], list[float]] = {}
    for row in found:
        by_call.setdefault((row.run, row.step), []).append(row.pressure)
    return by_call


def test_a_lone_run_holds_its_own_prompt() -> None:
    # a-1: step 1 at 0-1000 ms, step 2 begins at 1500 with 200 prompt tokens.
    lines = [call("a-1", 1, 0, 100), call("a-1", 2, 500, 200)]
    assert pressures(rows(lines, [0.0])) == {("a-1", 2): [0.2]}


def test_a_run_before_its_first_call_adds_nothing() -> None:
    lines = [call("a-1", 1, 0, 100), call("a-1", 2, 500, 200), call("b-2", 1, 0, 900)]
    # b-2 starts at 10 s, after a-1's step 2 at 1.5 s.
    assert pressures(rows(lines, [0.0, 10.0])) == {("a-1", 2): [0.2]}


def test_a_run_after_its_last_call_adds_nothing() -> None:
    # b-2's only call ends at 1000 ms, a-1's step 2 begins at 1500.
    lines = [call("a-1", 1, 0, 100), call("a-1", 2, 500, 200), call("b-2", 1, 0, 900)]
    assert pressures(rows(lines, [0.0, 0.0])) == {("a-1", 2): [0.2]}


def test_a_run_that_ended_as_the_call_began_has_left() -> None:
    # b-2 ends at exactly 1500 ms, when a-1's step 2 begins.
    lines = [
        call("a-1", 1, 0, 100),
        call("a-1", 2, 500, 200),
        call("b-2", 1, 0, 900, duration=1500),
    ]
    assert pressures(rows(lines, [0.0, 0.0])) == {("a-1", 2): [0.2]}


def test_a_run_that_begins_as_the_call_begins_is_live() -> None:
    # b-2's first call is sent at 1500 ms, as a-1's step 2 is.
    lines = [
        call("a-1", 1, 0, 100),
        call("a-1", 2, 500, 200),
        call("b-2", 1, 0, 300),
    ]
    assert pressures(rows(lines, [0.0, 1.5])) == {("a-1", 2): [0.5]}


def test_a_run_between_its_calls_adds_its_latest_prompt() -> None:
    # b-2: step 1 at 0-1000 ms with 300 tokens, step 2 at 5000 with 900: at 1500 it holds 300.
    lines = [
        call("a-1", 1, 0, 100),
        call("a-1", 2, 500, 200),
        call("b-2", 1, 0, 300),
        call("b-2", 2, 4000, 900),
    ]
    found = pressures(rows(lines, [0.0, 0.0]))
    assert found[("a-1", 2)] == [0.5]
    # At 5000 a-1 has ended (2500 ms), so b-2 holds only its own step 2.
    assert found[("b-2", 2)] == [0.9]


def test_two_runs_at_one_moment_add_up() -> None:
    lines = [
        call("a-1", 1, 0, 100),
        call("a-1", 2, 500, 200),
        call("b-2", 1, 0, 400, duration=2000),
    ]
    assert pressures(rows(lines, [0.0, 0.0])) == {("a-1", 2): [0.6]}


def test_the_pods_capacity_divides() -> None:
    lines = [call("a-1", 1, 0, 100), call("a-1", 2, 500, 200)]
    assert pressures(rows(lines, [0.0], capacity=2 * KV)) == {("a-1", 2): [0.1]}


def test_a_fanned_out_step_counts_once() -> None:
    # a-1's step 2 is two calls of one context, begun at 1500 and 1501 ms.
    lines = [
        call("a-1", 1, 0, 100),
        call("a-1", 2, 500, 400),
        call("a-1", 2, 501, 400),
        call("b-2", 1, 0, 300),
        call("b-2", 2, 1000, 300),
    ]
    found = pressures(rows(lines, [0.0, 0.0]))
    # b-2 holds its step 1's 300 between its calls (1000-2000 ms).
    assert found[("a-1", 2)] == [0.7, 0.7]
    # At 2000 b-2's step 2 begins: a-1 holds 400 once, not 800.
    assert found[("b-2", 2)] == [0.7]


def test_a_warm_up_run_adds_pressure_but_is_no_row() -> None:
    # x-1 is the discarded run, between its calls while k-2's step 2 begins at 1500.
    lines = [
        call("x-1", 1, 0, 300),
        call("x-1", 2, 4000, 300),
        call("k-2", 1, 0, 100),
        call("k-2", 2, 500, 200),
    ]
    assert pressures(rows(lines, [0.0, 0.0], runs_discarded=1)) == {("k-2", 2): [0.5]}


def test_a_run_stops_at_its_failed_call() -> None:
    # b-2: step 1 at 0-500 ms with 300 tokens, step 2 failed when sent at 2000.
    lines = [
        call("a-1", 1, 0, 100),
        call("a-1", 2, 500, 200),
        call("a-1", 3, 0, 250),
        call("b-2", 1, 0, 300, duration=500),
        call("b-2", 2, 1500, 300, duration=None),
    ]
    found, ended_by_failure = pressure_rows("exp", "v", lines, [0.0, 0.0], 0, KV)
    by_call = pressures(found)
    # At 1500 b-2 is between its calls; at 2500 it has failed.
    assert by_call == {("a-1", 2): [0.5], ("a-1", 3): [0.25]}
    assert ended_by_failure == 1


def test_first_steps_and_calls_with_no_first_token_are_no_rows() -> None:
    lines = [call("a-1", 1, 0, 100), call("a-1", 2, 500, 200, ttft=None)]
    assert rows(lines, [0.0]) == []


def test_a_row_carries_its_call() -> None:
    lines = [call("a-1", 1, 0, 100), call("a-1", 2, 500, 200, ttft=340, cached=150)]
    assert rows(lines, [0.0]) == [
        PressureRow("exp", "v", "a-1", 2, 0.2, 340, 0.75),
    ]


def test_a_call_with_no_cached_count_has_no_share() -> None:
    lines = [call("a-1", 1, 0, 100), call("a-1", 2, 500, 200, cached=None)]
    assert rows(lines, [0.0])[0].cached_share is None


@pytest.mark.parametrize(
    ("pressure", "bucket"), [(0.0, 0), (0.7499, 0), (0.75, 1), (0.9999, 1), (1.0, 2), (7.0, 2)]
)
def test_a_pressure_falls_in_its_bucket(pressure: float, bucket: int) -> None:
    assert bucket_of(pressure, EDGES) == bucket


def test_the_buckets_are_named_by_their_edges() -> None:
    assert bucket_labels(EDGES) == ["under 0.75", "0.75-1", "1 and over"]


def row(pressure: float, ttft: int, share: float | None) -> PressureRow:
    return PressureRow("exp", "v", "a-1", 2, pressure, ttft, share)


def test_a_bucket_gives_its_calls_first_tokens_and_cached_share() -> None:
    found = [
        row(0.1, 400, 0.9),
        row(0.2, 500, 0.7),
        row(0.3, 300, None),
        row(1.5, 9000, 0.4),
    ]
    assert bucket_stats(found, EDGES) == [
        BucketStats("under 0.75", 3, 400, 500, 0.8),
        BucketStats("0.75-1", 0, None, None, None),
        BucketStats("1 and over", 1, 9000, 9000, 0.4),
    ]


def test_a_buckets_p90_is_the_nearest_rank() -> None:
    found = [row(0.1, ttft, 0.5) for ttft in range(100, 1100, 100)]
    assert bucket_stats(found, EDGES)[0].ttft_p90_ms == 900


def test_a_buckets_cached_share_is_the_mean_of_its_calls() -> None:
    found = [row(0.1, 400, 0.9), row(0.1, 400, 0.7), row(0.1, 400, 0.2)]
    assert bucket_stats(found, EDGES)[0].cached_share == 0.6


def test_the_same_rows_write_the_same_csv(tmp_path: Path) -> None:
    lines = [call("a-1", 1, 0, 100), call("a-1", 2, 500, 200, cached=150)]
    first, second = tmp_path / "first.csv", tmp_path / "second.csv"
    write_csv(first, rows(lines, [0.0]))
    write_csv(second, rows(lines, [0.0]))
    assert first.read_bytes() == second.read_bytes()
    with first.open() as file:
        assert tuple(next(csv.reader(file))) == CSV_FIELDS


BURST = Pattern(runs=3, rate_per_second=0.02, burst=Burst(runs=1, every_seconds=120), fan_out=None)
REAL = {"replicas": 2}


def meta(**variant: Any) -> dict[str, Any]:
    return {
        "profile": "real",
        "pattern": "burst",
        "seed": 6,
        "runs_discarded": 1,
        "kv_capacity_tokens_per_pod": 4384,
        "variants": {"default": {"profile": "real", "replicas": None} | variant},
    }


def test_a_variant_reads_its_capacity_starts_and_warm_up() -> None:
    found = variant_input(meta(), "default", {"real": REAL}, {"burst": BURST})
    assert found.capacity_tokens == 2 * 4384
    assert found.starts == start_times(BURST, 6)
    assert found.runs_discarded == 1


def test_a_variant_with_its_own_replicas_uses_them() -> None:
    found = variant_input(meta(replicas=1), "default", {"real": REAL}, {"burst": BURST})
    assert found.capacity_tokens == 4384


def test_a_variant_with_no_profile_of_its_own_has_the_files() -> None:
    # The first baselines' .meta.json name the profile once, for the whole file.
    found = variant_input(meta(profile=None), "default", {"real": REAL}, {"burst": BURST})
    assert found.capacity_tokens == 2 * 4384


def test_a_simulated_variant_is_refused() -> None:
    with pytest.raises(PressureError, match="does not model KV admission"):
        variant_input(meta(profile="sim"), "default", {"sim": REAL}, {"burst": BURST})


def test_an_unknown_variant_is_refused() -> None:
    with pytest.raises(PressureError, match="has no variant plugin"):
        variant_input(meta(), "plugin", {"real": REAL}, {"burst": BURST})


def test_an_autoscaled_variant_is_refused() -> None:
    # Its pods changed during the replay, so the capacity is not one number.
    autoscaled = meta(autoscale={"csv": "x-autoscale.csv"})
    with pytest.raises(PressureError, match="autoscaled"):
        variant_input(autoscaled, "default", {"real": REAL}, {"burst": BURST})


def test_a_run_past_the_patterns_runs_is_refused() -> None:
    lines = [call("a-4", 1, 0, 100), call("a-4", 2, 500, 200)]
    with pytest.raises(PressureError, match="run a-4 is number 4, the pattern starts 3"):
        pressure_rows("exp", "v", lines, start_times(BURST, 6), 0, KV)


def write_result(results: Path, stem: str, lines: Sequence[Mapping[str, Any]]) -> None:
    results.mkdir(parents=True, exist_ok=True)
    (results / f"{stem}.meta.json").write_text(json.dumps(meta()))
    (results / f"{stem}-default.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines)
    )


def test_make_pressure_writes_the_same_csv_twice_and_the_graph(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    experiments = tmp_path / "experiments"
    lines = [
        call("w-1", 1, 0, 100),
        call("a-2", 1, 0, 100),
        call("a-2", 2, 500, 2000, ttft=900, cached=1000),
        call("b-3", 1, 0, 100),
        call("b-3", 2, 500, 9000, ttft=9000, cached=900),
    ]
    write_result(experiments / "results", "routing-x-0123456789ab", lines)
    spec = experiments / "pressure.yaml"
    spec.write_text(
        yaml.safe_dump(
            {"sources": [{"experiment": "routing-x-0123456789ab", "variants": ["default"]}]}
        )
    )
    main([str(spec)])
    written = sorted((experiments / "results").glob("pressure-*.csv"))
    assert len(written) == 1
    first = written[0].read_bytes()
    main([str(spec)])
    assert written[0].read_bytes() == first
    assert (experiments / "graphs" / f"{written[0].stem}.png").exists()
    printed = capsys.readouterr().out
    assert "routing-x-0123456789ab default" in printed
    assert "pooled" in printed


def test_a_source_with_no_variants_is_refused(tmp_path: Path) -> None:
    spec = tmp_path / "pressure.yaml"
    spec.write_text(yaml.safe_dump({"sources": [{"experiment": "x", "variants": []}]}))
    with pytest.raises(PressureError, match="x names no variant"):
        main([str(spec)])
