"""make calibrate's experiment file (P2.8a): what it accepts, the grid it gives, the files a run
writes and which of them a rerun still has to write. No cluster: the profiles are the repo's own."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from calibrator.config import (
    CalibrationError,
    Cell,
    load_calibration,
    outputs,
    pending,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
CALIBRATE_FILE = REPO_ROOT / "experiments" / "calibrate.yaml"


def _spec() -> dict[str, Any]:
    spec: dict[str, Any] = yaml.safe_load(CALIBRATE_FILE.read_text())
    return spec


def _write(tmp_path: Path, spec: dict[str, Any]) -> Path:
    path = tmp_path / "calibrate.yaml"
    path.write_text(yaml.safe_dump(spec))
    return path


def test_repo_file_gives_the_grid_in_order() -> None:
    calibration = load_calibration(CALIBRATE_FILE, CONFIG_DIR)
    assert calibration.name == "calibrate"
    assert len(calibration.hash) == 12
    assert calibration.profiles == {"lora": "real", "plain": "real-plain"}
    assert len(calibration.cells) == 4 * 4 * 3
    assert calibration.cells[0] == Cell(concurrency=1, prompt_tokens=256, cache="uncached")
    assert calibration.cells[1] == Cell(concurrency=1, prompt_tokens=256, cache="full")
    assert calibration.cells[3] == Cell(concurrency=1, prompt_tokens=1024, cache="uncached")
    assert calibration.cells[12] == Cell(concurrency=2, prompt_tokens=256, cache="uncached")
    assert calibration.cells[-1] == Cell(concurrency=8, prompt_tokens=3584, cache="partial")
    assert len(set(calibration.cells)) == len(calibration.cells)


def _set(spec: dict[str, Any], path: str, value: Any) -> None:
    *parents, last = path.split(".")
    section = spec
    for name in parents:
        section = section[name]
    section[last] = value


@pytest.mark.parametrize(
    ("path", "value", "named"),
    [
        ("concurrency", [0, 2], "concurrency"),
        ("concurrency", [], "concurrency"),
        ("concurrency", [2, 1], "concurrency"),
        ("concurrency", [1, 1], "concurrency"),
        ("concurrency", [1, 2.0], "concurrency"),
        ("pods", [1, 3], "pods"),
        ("pods", [], "pods"),
        ("prompt_tokens", [256, 4096], "prompt_tokens"),
        ("prompt_tokens", [True], "prompt_tokens"),
        ("completion_tokens", 1, "completion_tokens"),
        ("calls_per_cell", 0, "calls_per_cell"),
        ("cache.modes", ["uncached", "warm"], "cache.modes"),
        ("cache.modes", [], "cache.modes"),
        ("cache.modes", ["full", "full"], "cache.modes"),
        ("cache.partial_share", 1.0, "cache.partial_share"),
        ("cache.partial_share", 0, "cache.partial_share"),
        ("warmup.prompt_tokens", 4090, "warmup.prompt_tokens"),
        ("warmup.settled_ttft_ms", True, "warmup.settled_ttft_ms"),
        ("warmup.max_calls", 0, "warmup.max_calls"),
        ("cold_start.alone", 0, "cold_start.alone"),
        ("cold_start.both", 1.5, "cold_start.both"),
        ("host.sleep_settings", [], "host.sleep_settings"),
        ("call_timeout_seconds", 0, "call_timeout_seconds"),
        ("profiles", {"lora": "real"}, "profiles"),
        ("profiles", {"lora": "real-plain", "plain": "real-plain"}, "profiles.lora"),
        ("profiles", {"lora": "real", "plain": "real"}, "profiles.plain"),
        ("profiles", {"lora": "real", "plain": "nowhere"}, "profiles.plain"),
    ],
)
def test_refuses_a_value_and_names_its_key(
    tmp_path: Path, path: str, value: Any, named: str
) -> None:
    spec = _spec()
    _set(spec, path, value)
    with pytest.raises(CalibrationError, match=named):
        load_calibration(_write(tmp_path, spec), CONFIG_DIR)


def test_partial_share_is_not_needed_without_the_partial_mode(tmp_path: Path) -> None:
    spec = _spec()
    spec["cache"] = {"modes": ["uncached", "full"]}
    calibration = load_calibration(_write(tmp_path, spec), CONFIG_DIR)
    assert {cell.cache for cell in calibration.cells} == {"uncached", "full"}


def test_a_missing_key_raises_with_its_name(tmp_path: Path) -> None:
    spec = _spec()
    del spec["seed"]
    with pytest.raises(KeyError, match="seed"):
        load_calibration(_write(tmp_path, spec), CONFIG_DIR)


def test_outputs_one_file_per_model_and_pod_count_then_cold_start(tmp_path: Path) -> None:
    calibration = load_calibration(CALIBRATE_FILE, CONFIG_DIR)
    stem = f"calibration-{calibration.hash}"
    found = [
        (out.kind, out.model, out.pods, out.path.name, out.rows)
        for out in outputs(calibration, tmp_path)
    ]
    assert found == [
        ("cells", "lora", 1, f"{stem}-lora-1pod.csv", 48),
        ("cells", "lora", 2, f"{stem}-lora-2pods.csv", 96),
        ("cold_start", "lora", None, f"cold-start-{calibration.hash}-lora.csv", 5 + 3 * 2),
        ("cells", "plain", 1, f"{stem}-plain-1pod.csv", 48),
        ("cells", "plain", 2, f"{stem}-plain-2pods.csv", 96),
        ("cold_start", "plain", None, f"cold-start-{calibration.hash}-plain.csv", 5 + 3 * 2),
    ]
    assert {out.path.parent for out in outputs(calibration, tmp_path)} == {tmp_path}


def _csv(path: Path, rows: int) -> None:
    path.write_text("header\n" + "row\n" * rows)


def test_pending_skips_complete_files_and_redoes_the_rest(tmp_path: Path) -> None:
    calibration = load_calibration(CALIBRATE_FILE, CONFIG_DIR)
    lora_1pod, lora_2pods, lora_cold, *_ = outputs(calibration, tmp_path)
    _csv(lora_1pod.path, 48)
    _csv(lora_cold.path, 11)
    _csv(lora_2pods.path.with_name(lora_2pods.path.name + ".partial"), 30)
    _csv(tmp_path / "calibration-000000000000-plain-1pod.csv", 48)

    left = pending(outputs(calibration, tmp_path))

    assert [out.path.name for out in left] == [
        lora_2pods.path.name,
        f"calibration-{calibration.hash}-plain-1pod.csv",
        f"calibration-{calibration.hash}-plain-2pods.csv",
        f"cold-start-{calibration.hash}-plain.csv",
    ]


def test_pending_refuses_a_final_file_of_another_length(tmp_path: Path) -> None:
    calibration = load_calibration(CALIBRATE_FILE, CONFIG_DIR)
    first = outputs(calibration, tmp_path)[0]
    _csv(first.path, 47)
    with pytest.raises(CalibrationError, match="47 rows"):
        pending(outputs(calibration, tmp_path))
