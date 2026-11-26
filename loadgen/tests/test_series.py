"""make experiment-series (P5.0, card 73): a series' files in order, the finished ones skipped, the
first failure stopping the rest, then the square's comparison."""

from collections.abc import Callable
from pathlib import Path

import pytest
from load_generator.experiment import Outputs
from load_generator.series import finished, print_report, run_series
from load_generator.square_compare import Series


def files(directory: Path, names: list[str]) -> Series:
    paths = [directory / f"{name}.yaml" for name in names]
    for path in paths:
        path.write_text(f"{path.stem}\n")
    return Series(name="s", files=paths, reference="default")


def outputs_in(directory: Path) -> Callable[[Path], Outputs]:
    def outputs_of(path: Path) -> Outputs:
        return Outputs(
            csv=directory / f"{path.stem}.csv",
            meta=directory / f"{path.stem}.meta.json",
            graph=directory / f"{path.stem}.png",
            validation=directory / f"{path.stem}-validation.csv",
        )

    return outputs_of


def write_all(out: Outputs) -> None:
    for path in (out.csv, out.meta, out.graph):
        path.write_text("x\n")


def test_a_file_is_finished_once_its_csv_meta_and_graph_are_written(tmp_path: Path) -> None:
    """make experiment writes the graph last (write_results)."""
    out = outputs_in(tmp_path)(tmp_path / "a.yaml")
    assert not finished(out)
    out.csv.write_text("x\n")
    out.meta.write_text("x\n")
    assert not finished(out)
    out.graph.write_text("x\n")
    assert finished(out)


def test_every_file_runs_in_the_series_order(tmp_path: Path) -> None:
    series = files(tmp_path, ["b", "a", "c"])
    ran: list[str] = []
    said: list[str] = []
    run_series(series, outputs_in(tmp_path), lambda path: ran.append(path.stem), said.append)
    assert ran == ["b", "a", "c"]
    assert said == [f"series: running {name}.yaml, {n} of 3" for n, name in enumerate(ran, 1)]


def test_a_finished_file_is_skipped_and_said_so(tmp_path: Path) -> None:
    series = files(tmp_path, ["a", "b"])
    outputs_of = outputs_in(tmp_path)
    write_all(outputs_of(series.files[0]))
    ran: list[str] = []
    said: list[str] = []
    run_series(series, outputs_of, lambda path: ran.append(path.stem), said.append)
    assert ran == ["b"]
    assert said[0] == "series: a.yaml finished before, skipped"


def test_a_file_left_half_run_is_run_again_so_its_own_check_refuses_its_leftovers(
    tmp_path: Path,
) -> None:
    """make experiment refuses to start over a replay that stopped, so the series stops there."""
    series = files(tmp_path, ["a"])
    outputs_of = outputs_in(tmp_path)
    outputs_of(series.files[0]).csv.write_text("x\n")
    ran: list[str] = []
    run_series(series, outputs_of, lambda path: ran.append(path.stem), lambda _: None)
    assert ran == ["a"]


def test_the_first_failure_stops_the_series(tmp_path: Path) -> None:
    series = files(tmp_path, ["a", "b", "c"])
    ran: list[str] = []

    def run_file(path: Path) -> None:
        ran.append(path.stem)
        if path.stem == "b":
            raise RuntimeError("b broke")

    with pytest.raises(RuntimeError, match="b broke"):
        run_series(series, outputs_in(tmp_path), run_file, lambda _: None)
    assert ran == ["a", "b"]


def series_spec(directory: Path, text: str) -> Path:
    (directory / "series").mkdir()
    for name in ("a", "b"):
        (directory / f"{name}.yaml").write_text(f"{name}\n")
    path = directory / "series" / "s.yaml"
    path.write_text(text)
    return path


@pytest.mark.parametrize(
    ("text", "printed"),
    [
        ("reference: default\nfiles: [a, b]\n", "square"),
        ("reference: queue\nfiles: [a, b]\nautoscale:\n  idle_judged: true\n", "autoscale"),
    ],
    ids=["square", "autoscale"],
)
def test_a_series_ends_with_its_squares_report_or_with_the_autoscalers_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, printed: str
) -> None:
    """P5.2 (card 76): an autoscale section asks for make autoscale-square's report, which holds
    square-compare's."""
    said: list[str] = []
    monkeypatch.setattr(
        "load_generator.series.print_comparison", lambda series: said.append("square")
    )
    monkeypatch.setattr(
        "load_generator.series.autoscale_square.print_comparison",
        lambda autoscaled: said.append("autoscale"),
    )
    print_report(series_spec(tmp_path, text))
    assert said == [printed]
