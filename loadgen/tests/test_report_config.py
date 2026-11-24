"""config/report.yaml (P2.5): what the reports show, apart from the profiles a run stamps."""

from pathlib import Path

import pytest
import yaml
from load_generator.graph import figure_data, render
from load_generator.report_config import GraphLayout, ReportConfigError, load_report_config
from load_generator.results import CallRow, summarise, write_csv

REPO_ROOT = Path(__file__).resolve().parents[2]
SETTINGS = {
    "fewest_tokens_for_speed": 5,
    "runs_left_out": [2, 5],
    "pressure_bucket_edges": [0.75, 1.0, 1.25],
    "burst_waiting_window_seconds": 60,
    "late_start_ms": 50,
    "graph": {
        "bar_height": 0.3,
        "value_gap": 0.02,
        "hit_rate_axis_end": 1.15,
        "variant_spacing": 0.22,
        "band_margin": 0.6,
    },
}


def written(directory: Path, **changes: object) -> Path:
    path = directory / "report.yaml"
    path.write_text(yaml.safe_dump({**SETTINGS, **changes}))
    return path


def test_the_file_gives_the_settings(tmp_path: Path) -> None:
    config = load_report_config(written(tmp_path))
    assert config.fewest_tokens_for_speed == 5
    assert config.runs_left_out == (2, 5)
    assert config.pressure_bucket_edges == (0.75, 1.0, 1.25)
    assert config.burst_waiting_window_seconds == 60
    assert config.late_start_ms == 50
    assert config.graph == GraphLayout(
        bar_height=0.3,
        value_gap=0.02,
        hit_rate_axis_end=1.15,
        variant_spacing=0.22,
        band_margin=0.6,
    )


def test_the_committed_file_loads() -> None:
    load_report_config(REPO_ROOT / "config" / "report.yaml")


def test_a_missing_setting_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "report.yaml"
    path.write_text(yaml.safe_dump({"runs_left_out": [2]}))
    with pytest.raises(KeyError):
        load_report_config(path)


@pytest.mark.parametrize("window", [0, 2.5, "60"])
def test_the_window_after_a_burst_is_whole_seconds_over_zero(
    tmp_path: Path, window: object
) -> None:
    with pytest.raises(ReportConfigError, match="burst_waiting_window_seconds"):
        load_report_config(written(tmp_path, burst_waiting_window_seconds=window))


@pytest.mark.parametrize("late", [-1, 2.5])
def test_a_late_start_is_whole_milliseconds(tmp_path: Path, late: object) -> None:
    with pytest.raises(ReportConfigError, match="late_start_ms"):
        load_report_config(written(tmp_path, late_start_ms=late))


@pytest.mark.parametrize("profile", ["real", "real-plain", "sim", "demo"])
def test_a_late_start_is_what_each_profile_holds_a_replays_gaps_to(profile: str) -> None:
    replay = yaml.safe_load((REPO_ROOT / "config" / f"{profile}.yaml").read_text())["replay"]
    late = load_report_config(REPO_ROOT / "config" / "report.yaml").late_start_ms
    assert late == replay["gap_tolerance_ms"]


@pytest.mark.parametrize("fewest", [1, 0, 2.5])
def test_a_speed_needs_two_tokens_at_least(tmp_path: Path, fewest: object) -> None:
    """The speed divides by the tokens after the first, so one token would divide by zero."""
    with pytest.raises(ReportConfigError, match="fewest_tokens_for_speed"):
        load_report_config(written(tmp_path, fewest_tokens_for_speed=fewest))


@pytest.mark.parametrize("left_out", [[0], [-1], [1.5], 2])
def test_runs_left_out_are_whole_counts(tmp_path: Path, left_out: object) -> None:
    with pytest.raises(ReportConfigError, match="runs_left_out"):
        load_report_config(written(tmp_path, runs_left_out=left_out))


@pytest.mark.parametrize("edges", [[], [1.0, 0.75], [0.75, 0.75], [0, 1.0], ["1"], 1.0])
def test_pressure_buckets_need_rising_edges_over_zero(tmp_path: Path, edges: object) -> None:
    with pytest.raises(ReportConfigError, match="pressure_bucket_edges"):
        load_report_config(written(tmp_path, pressure_bucket_edges=edges))


def call(completion_tokens: int, duration_ms: int) -> CallRow:
    return CallRow(
        variant="a",
        run="r-1",
        task_type="csv",
        step=1,
        pod="pod-a",
        ttft_ms=0,
        duration_ms=duration_ms,
        gap_before_ms=0,
        prompt_tokens=100,
        completion_tokens=completion_tokens,
        cached_tokens=0,
        moved=None,
        tokens_on_pod=110,
        over_kv_limit=False,
    )


def test_the_speed_covers_the_calls_the_setting_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    # 100 ms per token over 3 tokens, 300 ms per token over 11.
    rows = [call(4, 300), call(12, 3300)]
    monkeypatch.setattr("load_generator.results.FEWEST_TOKENS_FOR_SPEED", 2)
    assert summarise(rows)["a"].generation_ms_per_token == 200
    monkeypatch.setattr("load_generator.results.FEWEST_TOKENS_FOR_SPEED", 5)
    assert summarise(rows)["a"].generation_ms_per_token == 300


def test_a_graph_is_drawn_with_the_files_layout(tmp_path: Path) -> None:
    csv = tmp_path / "exp.csv"
    write_csv(csv, [call(10, 2000)])
    assert [variant.name for variant in figure_data(csv)] == ["a"]
    out = tmp_path / "exp.png"
    render(csv, out)
    assert out.read_bytes().startswith(b"\x89PNG")
