"""make context-check (P4.10): Prometheus's sum(thesis_context_tokens) against the replay's file.

The file's sum at a moment is each live run's latest step's prompt tokens, a fanned-out step once;
a run is live from its first call's start to its last call's end, or to the start of a failed call.
"""

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import pytest
from load_generator import context_check
from load_generator.context_check import (
    ContextCheckError,
    Sample,
    held_at,
    judge,
    samples_from,
    sums_within,
)

INTERVAL_MS = 5_000


def line(
    run_id: str,
    step: int,
    ts_start: int,
    prompt_tokens: int,
    duration_ms: int | None = 500,
    parallel_group: str | None = None,
) -> dict[str, Any]:
    """A replay line as replay.py writes it, with only the fields the check reads besides."""
    result: dict[str, Any] = {
        "run_id": run_id,
        "step": step,
        "ts_start": ts_start,
        "prompt_tokens": prompt_tokens,
    }
    if duration_ms is not None:
        result["duration_ms"] = duration_ms
    if parallel_group is not None:
        result["parallel_group"] = parallel_group
    return result


# Run a: step 1 at 10.0-10.5 s with 100 tokens, step 2 at 12.0-12.5 s with 150.
RUN_A = [line("run-a", 1, 10_000, 100), line("run-a", 2, 12_000, 150)]


def test_a_run_holds_its_latest_step_s_prompt_from_its_start_to_its_end() -> None:
    assert held_at(RUN_A, 9_999) == 0
    assert held_at(RUN_A, 10_000) == 100
    # Between its calls too: the pod keeps the context.
    assert held_at(RUN_A, 11_000) == 100
    assert held_at(RUN_A, 12_000) == 150
    assert held_at(RUN_A, 12_499) == 150
    assert held_at(RUN_A, 12_500) == 0


def test_a_fanned_out_step_counts_once() -> None:
    lines = [
        line("run-a", 1, 10_000, 100),
        line("run-a", 2, 12_000, 150, 400, "run-a-2"),
        line("run-a", 2, 12_000, 150, 600, "run-a-2"),
    ]
    assert held_at(lines, 12_100) == 150
    # The run lasts until its slower call ends.
    assert held_at(lines, 12_500) == 150
    assert held_at(lines, 12_600) == 0


def test_runs_that_overlap_add() -> None:
    lines = [*RUN_A, line("run-b", 1, 11_000, 70, 2_000)]
    assert held_at(lines, 11_500) == 100 + 70
    assert held_at(lines, 12_200) == 150 + 70
    assert held_at(lines, 12_700) == 70


def test_a_run_stops_at_the_start_of_its_failed_call() -> None:
    lines = [line("run-a", 1, 10_000, 100), line("run-a", 2, 12_000, 150, None)]
    assert held_at(lines, 11_999) == 100
    assert held_at(lines, 12_000) == 0


def test_a_run_stops_at_the_first_of_its_failed_calls() -> None:
    """A fanned-out step whose calls both failed, the second sent 10 ms after the first."""
    lines = [
        line("run-a", 1, 10_000, 100),
        line("run-a", 2, 12_000, 150, None, "run-a-2"),
        line("run-a", 2, 12_010, 150, None, "run-a-2"),
    ]
    assert held_at(lines, 12_005) == 0


def test_sums_within_are_every_value_the_window_passes_through() -> None:
    assert sums_within(RUN_A, 9_000, 9_500) == {0}
    assert sums_within(RUN_A, 9_000, 10_000) == {0, 100}
    assert sums_within(RUN_A, 10_500, 11_500) == {100}
    assert sums_within(RUN_A, 11_000, 13_000) == {100, 150, 0}


def matching_samples() -> list[Sample]:
    """Every 5 s around a run live from 20 s to 32 s, its context rising to 300."""
    return [
        Sample(5_000, 0),
        Sample(10_000, 0),
        Sample(15_000, 0),
        Sample(20_000, 0),
        Sample(25_000, 200),
        Sample(30_000, 300),
        Sample(35_000, 0),
        Sample(40_000, 0),
    ]


MATCHING_RUN = [
    line("run-a", 1, 20_000, 200, 6_000),
    line("run-a", 2, 28_000, 300, 4_000),
]


def test_samples_that_follow_the_file_pass() -> None:
    judgement = judge(MATCHING_RUN, matching_samples(), INTERVAL_MS)
    assert judgement.failures() == []
    assert judgement.highest == Sample(30_000, 300)
    assert judgement.highest_sums == [0, 200, 300]
    assert (judgement.matched, judgement.total) == (8, 8)
    # 5-15 s: no run within one scrape; 25 s: one live all through 20-30 s.
    assert (judgement.idle, judgement.busy) == (3, 1)


def test_a_highest_reading_no_moment_near_it_had_fails() -> None:
    samples = [*matching_samples()[:5], Sample(30_000, 999), *matching_samples()[6:]]
    failures = judge(MATCHING_RUN, samples, INTERVAL_MS).failures()
    assert len(failures) == 1
    assert "999" in failures[0]


def test_a_reading_above_0_while_no_run_was_live_fails() -> None:
    samples = [Sample(5_000, 0), Sample(10_000, 40), *matching_samples()[2:]]
    failures = judge(MATCHING_RUN, samples, INTERVAL_MS).failures()
    # Times are seconds from the replay's first call, at 20 s here.
    assert failures == ["1 reading above 0 while no run was live within one scrape: 40 at -10.0 s"]


def test_a_reading_of_0_while_runs_were_live_fails() -> None:
    samples = [*matching_samples()[:4], Sample(25_000, 0), *matching_samples()[5:]]
    failures = judge(MATCHING_RUN, samples, INTERVAL_MS).failures()
    assert failures == ["1 reading of 0 while runs were live all through one scrape: at 5.0 s"]


def test_a_series_after_the_replay_fails() -> None:
    """The last run ends at 32 s; the replayer's /metrics closes then and Prometheus's next failed
    scrape ends the series, so nothing past two scrapes after it."""
    samples = [*matching_samples(), Sample(45_000, 0)]
    failures = judge(MATCHING_RUN, samples, INTERVAL_MS).failures()
    assert failures == ["1 reading after the replay ended at 12.0 s: at 25.0 s"]


def test_no_reading_above_0_fails() -> None:
    samples = [Sample(at, 0) for at in range(5_000, 45_000, 5_000)]
    failures = judge(MATCHING_RUN, samples, INTERVAL_MS).failures()
    assert "no reading above 0" in failures[0]


def test_no_series_at_all_is_refused() -> None:
    with pytest.raises(ContextCheckError, match="no sum"):
        judge(MATCHING_RUN, [], INTERVAL_MS)


def test_samples_come_from_the_one_series_of_the_sum() -> None:
    result = [{"metric": {}, "values": [[25.0, "200"], [30.5, "300"]]}]
    assert samples_from(result) == [Sample(25_000, 200), Sample(30_500, 300)]
    assert samples_from([]) == []


def test_more_than_one_series_is_refused() -> None:
    series = {"metric": {}, "values": [[25.0, "200"]]}
    with pytest.raises(ContextCheckError, match="2 series"):
        samples_from([series, series])


def test_scraped_asks_for_the_replayer_s_raw_samples_over_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raw samples at their scrape times, not a range query's grid, whose value at t is the latest
    scrape before t: scrapes slip (up to 7.6 s apart in P4.10's replay) as WSL steps the clock."""
    asked: list[tuple[str, ...]] = []
    result = [{"metric": {}, "values": [[25.0, "200"]]}]
    answer = {"status": "success", "data": {"result": result}}

    def fake_kubectl(*args: str) -> str:
        asked.append(args)
        return json.dumps(answer)

    monkeypatch.setattr(context_check, "kubectl", fake_kubectl)
    # 36.6 s, which Prometheus must get in whole seconds: 37, so the window starts no later.
    assert context_check.scraped(15.4, 52.0) == result
    ((get, raw, path),) = asked
    assert (get, raw) == ("get", "--raw")
    api, query = path.split("?")
    assert api.endswith("/proxy/api/v1/query")
    assert dict(parse_qsl(query)) == {
        "query": 'thesis_context_tokens{job="replayer-metrics"}[37s]',
        "time": "52.000",
    }


def test_a_query_prometheus_refuses_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    refusal = {"status": "error", "error": "bad selector"}
    monkeypatch.setattr(context_check, "kubectl", lambda *_: json.dumps(refusal))
    with pytest.raises(ContextCheckError, match="bad selector"):
        context_check.scraped(15.0, 52.0)


def test_main_reads_the_replay_s_window_and_exits_1_on_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    replay = tmp_path / "replay.jsonl"
    replay.write_text("".join(json.dumps(each) + "\n" for each in MATCHING_RUN))
    asked: list[tuple[float, float]] = []

    def fake_scraped(since: float, until: float) -> list[Any]:
        asked.append((since, until))
        values = [[sample.at_ms / 1000, str(sample.tokens)] for sample in matching_samples()]
        values[5][1] = "999"
        return [{"metric": {}, "values": values}]

    waited: list[float] = []
    monkeypatch.setattr(context_check, "scraped", fake_scraped)
    monkeypatch.setattr(context_check, "wait_until", waited.append)
    with pytest.raises(SystemExit) as stopped:
        context_check.main([str(replay), "--profile", "sim"])
    assert stopped.value.code == 1
    # sim scrapes every 5 s: from one scrape before the first call to four after the last end.
    assert waited == [52.0]
    assert asked == [(15.0, 52.0)]
    out = capsys.readouterr().out
    assert "context-check: failed" in out
    assert "999" in out
