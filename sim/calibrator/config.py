"""make calibrate's experiment file (P2.8a, experiments/calibrate.yaml): checked before anything on
the cluster changes, since a run takes a night and a bad value should not fail it at hour six.

The grid is every concurrency x prompt length x cache mode, in that order. Each model (the profile
with the adapter and the one without) gets one file per pod count and one cold-start file, named by
the file's hash, so a rerun can tell which of them a run already finished.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from load_generator.record import experiment_hash

CACHE_MODES = ("uncached", "full", "partial")
# The two models make calibrate compares, in the order it measures them, and what each profile must
# have: lora serves the P1.7 adapter, plain the same pods without it.
MODELS = ("lora", "plain")


class CalibrationError(Exception):
    """The calibration must not start, or must stop: nothing more is written."""


@dataclass(frozen=True)
class Cell:
    concurrency: int
    prompt_tokens: int
    cache: str


@dataclass(frozen=True)
class Warmup:
    prompt_tokens: int
    settled_ttft_ms: int
    max_calls: int


@dataclass(frozen=True)
class ColdStart:
    alone: int
    both: int


@dataclass(frozen=True)
class Host:
    power_scheme: str
    sleep_settings: tuple[str, ...]


@dataclass(frozen=True)
class Calibration:
    name: str
    hash: str
    profiles: Mapping[str, str]
    # The profiles' pod count, which the "both" cold starts restart at once.
    replicas: int
    pods: tuple[int, ...]
    cells: tuple[Cell, ...]
    completion_tokens: int
    calls_per_cell: int
    partial_share: float | None
    warmup: Warmup
    cold_start: ColdStart
    host: Host
    seed: int
    call_timeout_seconds: int


@dataclass(frozen=True)
class Output:
    # "cells": one row per pod and cell; "cold_start": one row per pod start.
    kind: str
    model: str
    pods: int | None
    path: Path
    rows: int


def _is_count(value: Any, least: int) -> bool:
    """YAML's yes is an int to Python, and a float would pass a comparison and fail later."""
    return type(value) is int and value >= least


def _count(spec: Mapping[str, Any], key: str, least: int = 1) -> int:
    value = spec[key.rsplit(".", 1)[-1]]
    if not _is_count(value, least):
        raise CalibrationError(f"{key} must be a whole number from {least}, not {value!r}")
    count: int = value
    return count


def _levels(spec: Mapping[str, Any], key: str, most: int | None = None) -> tuple[int, ...]:
    """Ascending, each once: a file's rows follow this order, and a level twice would mix cells."""
    values = spec[key]
    if (
        not isinstance(values, list)
        or not values
        or not all(_is_count(value, 1) for value in values)
        or (most is not None and max(values) > most)
        or values != sorted(set(values))
    ):
        bound = "" if most is None else f" up to {most}"
        raise CalibrationError(f"{key} must be ascending whole numbers from 1{bound}: {values!r}")
    return tuple(values)


def _profile(config_dir: Path, model: str, name: str) -> dict[str, Any]:
    path = config_dir / f"{name}.yaml"
    if not path.exists():
        raise CalibrationError(f"profiles.{model}: no profile {path}")
    profile: dict[str, Any] = yaml.safe_load(path.read_text())
    if ("lora" in profile["model"]) != (model == "lora"):
        has = "serves" if "lora" in profile["model"] else "has no"
        raise CalibrationError(f"profiles.{model}: {name} {has} model.lora")
    return profile


def _cache(spec: Mapping[str, Any]) -> tuple[tuple[str, ...], float | None]:
    modes = spec["modes"]
    if (
        not isinstance(modes, list)
        or not modes
        or not set(modes) <= set(CACHE_MODES)
        or len(set(modes)) != len(modes)
    ):
        raise CalibrationError(f"cache.modes must be some of {CACHE_MODES}, each once: {modes!r}")
    if "partial" not in modes:
        return tuple(modes), None
    share = spec["partial_share"]
    if type(share) is not float or not 0 < share < 1:
        raise CalibrationError(f"cache.partial_share must be between 0 and 1: {share!r}")
    return tuple(modes), share


def load_calibration(path: Path, config_dir: Path) -> Calibration:
    """config_dir holds the profiles the file names. A missing key raises KeyError."""
    spec = yaml.safe_load(path.read_text())
    if sorted(spec["profiles"]) != sorted(MODELS):
        raise CalibrationError(f"profiles must name {MODELS}: {sorted(spec['profiles'])}")
    profiles = {model: _profile(config_dir, model, spec["profiles"][model]) for model in MODELS}
    max_model_len = min(profile["model"]["max_model_len"] for profile in profiles.values())
    replicas = min(profile["replicas"] for profile in profiles.values())
    completion = _count(spec, "completion_tokens", least=2)
    longest = max_model_len - completion
    modes, share = _cache(spec["cache"])
    warmup = spec["warmup"]
    warmup_prompt = _count(warmup, "warmup.prompt_tokens")
    if warmup_prompt > longest:
        raise CalibrationError(
            f"warmup.prompt_tokens {warmup_prompt} + completion_tokens {completion} is over "
            f"max_model_len {max_model_len}"
        )
    host = spec["host"]
    if not host["sleep_settings"]:
        raise CalibrationError("host.sleep_settings must name at least one setting")
    return Calibration(
        name=path.stem,
        hash=experiment_hash(path),
        profiles=dict(spec["profiles"]),
        replicas=replicas,
        pods=_levels(spec, "pods", most=replicas),
        cells=tuple(
            Cell(concurrency, prompt_tokens, cache)
            for concurrency in _levels(spec, "concurrency")
            for prompt_tokens in _levels(spec, "prompt_tokens", most=longest)
            for cache in modes
        ),
        completion_tokens=completion,
        calls_per_cell=_count(spec, "calls_per_cell"),
        partial_share=share,
        warmup=Warmup(
            prompt_tokens=warmup_prompt,
            settled_ttft_ms=_count(warmup, "warmup.settled_ttft_ms"),
            max_calls=_count(warmup, "warmup.max_calls"),
        ),
        cold_start=ColdStart(
            alone=_count(spec["cold_start"], "cold_start.alone"),
            both=_count(spec["cold_start"], "cold_start.both"),
        ),
        host=Host(power_scheme=host["power_scheme"], sleep_settings=tuple(host["sleep_settings"])),
        seed=_count(spec, "seed", least=0),
        call_timeout_seconds=_count(spec, "call_timeout_seconds"),
    )


def _pods_name(pods: int) -> str:
    return "1pod" if pods == 1 else f"{pods}pods"


def outputs(calibration: Calibration, directory: Path) -> list[Output]:
    """Every file a whole run writes, in the order it writes them: per model, each pod count's
    cells, then its cold starts, one row per pod that started."""
    found: list[Output] = []
    for model in MODELS:
        for pods in calibration.pods:
            name = f"calibration-{calibration.hash}-{model}-{_pods_name(pods)}.csv"
            rows = pods * len(calibration.cells)
            found.append(Output("cells", model, pods, directory / name, rows))
        name = f"cold-start-{calibration.hash}-{model}.csv"
        rows = calibration.cold_start.alone + calibration.cold_start.both * calibration.replicas
        found.append(Output("cold_start", model, None, directory / name, rows))
    return found


def _rows(path: Path) -> int:
    """Data rows: the file's lines but its header."""
    return len(path.read_text().splitlines()) - 1


def pending(planned: Sequence[Output]) -> list[Output]:
    """What a run still has to write. A file is written under .partial and renamed once whole, so a
    final name means a finished file; one of another length was changed by hand and is not
    overwritten."""
    left = []
    for out in planned:
        if not out.path.exists():
            left.append(out)
        elif _rows(out.path) != out.rows:
            raise CalibrationError(
                f"{out.path} has {_rows(out.path)} rows, a finished one has {out.rows}: "
                "delete it to measure it again"
            )
    return left
