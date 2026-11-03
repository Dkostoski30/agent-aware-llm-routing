"""The graph reads only the CSV (P2.5): make experiment-graph needs no cluster or replay file."""

from pathlib import Path

import pytest
from load_generator.graph import figure_data, graph_path, main, render
from load_generator.results import CallRow, write_csv


def row(variant: str, run: str, step: int = 1, **fields: object) -> CallRow:
    values: dict[str, object] = {
        "variant": variant,
        "run": run,
        "task_type": "csv",
        "step": step,
        "pod": "pod-a",
        "ttft_ms": 500,
        "duration_ms": 2000,
        "gap_before_ms": 0,
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "cached_tokens": 50,
        "moved": None,
        "tokens_on_pod": 110,
        "over_kv_limit": False,
        **fields,
    }
    return CallRow(**values)  # type: ignore[arg-type]


def is_png(path: Path) -> bool:
    return path.read_bytes().startswith(b"\x89PNG")


def test_the_graph_is_drawn_from_a_csv_alone(tmp_path: Path) -> None:
    csv = tmp_path / "exp-abc.csv"
    write_csv(csv, [row("round-robin", "r-1"), row("default", "r-1", ttft_ms=800)])
    out = tmp_path / "exp-abc.png"
    render(csv, out)
    assert is_png(out)


def test_a_variant_with_no_finished_run_and_no_calls_over_the_limit_is_still_drawn(
    tmp_path: Path,
) -> None:
    csv = tmp_path / "exp.csv"
    write_csv(
        csv,
        [
            row("a", "r-1", ttft_ms=None, duration_ms=None, cached_tokens=None, pod=None,
                tokens_on_pod=None, over_kv_limit=None),
            row("b", "r-1", over_kv_limit=True),
        ],
    )  # fmt: skip
    out = tmp_path / "exp.png"
    render(csv, out)
    assert is_png(out)


def test_the_figures_data_gives_each_variants_runs_hit_rate_and_split(tmp_path: Path) -> None:
    csv = tmp_path / "exp.csv"
    write_csv(
        csv,
        [
            row("a", "r-1", step=1, duration_ms=2000, cached_tokens=0),
            row("a", "r-1", step=2, gap_before_ms=5, duration_ms=1000, cached_tokens=100,
                over_kv_limit=True, ttft_ms=9000),
            row("a", "r-2", step=1, duration_ms=None, ttft_ms=None, cached_tokens=None, pod=None,
                tokens_on_pod=None, over_kv_limit=None),
            row("b", "r-1", step=1, duration_ms=4000),
        ],
    )  # fmt: skip
    data = figure_data(csv)
    assert [variant.name for variant in data] == ["a", "b"]
    assert data[0].finished_runs == 1
    assert data[0].total_runs == 2
    assert data[0].completions_seconds == [3.005]
    assert data[0].ecdf == [1.0]
    assert data[0].hit_rate == pytest.approx(100 / 200)
    assert data[0].under.calls == 1
    assert data[0].over.calls == 1
    assert data[0].over.p50_ms == 9000
    assert data[1].completions_seconds == [4.0]


def test_a_variant_keeps_its_place_whichever_ran_first(tmp_path: Path) -> None:
    """A place gives a colour, and the baseline and its reversed run are read side by side."""
    one = tmp_path / "one.csv"
    other = tmp_path / "other.csv"
    write_csv(one, [row("round-robin", "r-1"), row("default", "r-1")])
    write_csv(other, [row("default", "r-1"), row("round-robin", "r-1")])
    assert [variant.name for variant in figure_data(one)] == ["default", "round-robin"]
    assert [variant.name for variant in figure_data(other)] == ["default", "round-robin"]


def test_an_empty_csv_is_refused_instead_of_drawing_nothing(tmp_path: Path) -> None:
    csv = tmp_path / "exp.csv"
    write_csv(csv, [])
    with pytest.raises(ValueError, match="no rows"):
        render(csv, tmp_path / "exp.png")


def test_the_default_output_is_in_experiments_graphs_under_the_csvs_name() -> None:
    path = graph_path(Path("experiments/results/baseline-abc.csv"))
    assert path.parts[-2:] == ("graphs", "baseline-abc.png")
    assert path.parts[-3] == "experiments"


def test_main_draws_the_named_csv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    csv = tmp_path / "exp.csv"
    write_csv(csv, [row("a", "r-1")])
    out = tmp_path / "drawn.png"
    monkeypatch.setattr("sys.argv", ["graph", str(csv), "--out", str(out)])
    main()
    assert is_png(out)
