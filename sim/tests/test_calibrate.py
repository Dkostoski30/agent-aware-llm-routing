"""make calibrate's driver (P2.8a): the order it measures in, what a rerun skips, what it leaves
behind on a failure, and the calls themselves. The cluster is a fake that logs what it is asked;
the pods are httpx.MockTransport. No network."""

import asyncio
import csv
import dataclasses
import json
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from calibrator.calibrate import (
    WarmCall,
    closed_loop,
    expected_models,
    forwarded_port,
    measured_call,
    ready_pods,
    run_calibration,
    warm_pod,
)
from calibrator.config import (
    Calibration,
    CalibrationError,
    Cell,
    ColdStart,
    Warmup,
    load_calibration,
    outputs,
)
from calibrator.prompts import CallPrompt
from calibrator.stats import ClockReading, Measured

REPO_ROOT = Path(__file__).resolve().parents[2]
CALIBRATE_FILE = REPO_ROOT / "experiments" / "calibrate.yaml"
SHA = {"lora": "a" * 64, "plain": "b" * 64}
CREATED = "2026-11-05T21:20:00Z"
READY = "2026-11-05T21:22:05Z"
READY_UNIX = 1793913725.0


def _calibration() -> Calibration:
    """The repo's file with two cells and three starts, so a whole run is a few rows."""
    calibration = load_calibration(CALIBRATE_FILE, REPO_ROOT / "config")
    return dataclasses.replace(
        calibration, cells=calibration.cells[:2], cold_start=ColdStart(alone=2, both=1)
    )


class FakePods:
    def __init__(self, cluster: "FakeCluster", model: str, pods: Sequence[str]) -> None:
        self.cluster = cluster
        self.model = model
        self.pods = list(pods)

    def warm(self) -> dict[str, list[WarmCall]]:
        self.cluster.log.append(f"warm {self.pods}")
        return {
            pod: [WarmCall(26000, READY_UNIX + 30.0), WarmCall(900, READY_UNIX + 31.5)]
            for pod in self.pods
        }

    def cell(self, cell: Cell) -> dict[str, list[Measured]]:
        self.cluster.log.append(f"cell {cell.prompt_tokens} {cell.cache}")
        if (self.model, len(self.pods), cell) == self.cluster.fail_at:
            raise CalibrationError("no full reply")
        calls = self.cluster.calibration.calls_per_cell
        return {
            pod: [Measured(pod, 400, 400 + 63 * 150, 64, 0, cell.concurrency)] * calls
            for pod in self.pods
        }


class FakeCluster:
    """Pods are named in the order they start; scaling down removes the newest, as a ReplicaSet
    does."""

    def __init__(
        self,
        calibration: Calibration,
        fail_at: tuple[str, int, Cell] | None = None,
        refuse: bool = False,
        add_no_pod: bool = False,
    ) -> None:
        self.calibration = calibration
        self.fail_at = fail_at
        self.refuse = refuse
        self.add_no_pod = add_no_pod
        self.log: list[str] = []
        self.started = 0
        self.pods: list[str] = []
        self.readings = 0

    def _start(self, count: int) -> None:
        for _ in range(count):
            self.started += 1
            self.pods.append(f"pod-{self.started}")

    def check_host(self) -> None:
        self.log.append("host")
        if self.refuse:
            raise CalibrationError("power plan")

    def deploy(self, profile: str) -> None:
        self.log.append(f"deploy {profile}")
        self.pods = []
        self._start(self.calibration.replicas)

    def scale(self, replicas: int) -> list[str]:
        self.log.append(f"scale {replicas}")
        if not self.add_no_pod:
            self._start(replicas - len(self.pods))
        self.pods = self.pods[:replicas]
        return list(self.pods)

    def restart(self) -> list[str]:
        self.log.append("restart")
        count = len(self.pods)
        self.pods = []
        self._start(count)
        return list(self.pods)

    def pod_json(self, pod: str) -> dict[str, Any]:
        return {
            "metadata": {"name": pod, "creationTimestamp": CREATED},
            "status": {
                "conditions": [{"type": "Ready", "status": "True", "lastTransitionTime": READY}]
            },
        }

    @contextmanager
    def reach(self, model: str, pods: Sequence[str]) -> Iterator[FakePods]:
        self.log.append(f"reach {model} {list(pods)}")
        yield FakePods(self, model, pods)

    def clock(self) -> ClockReading:
        """600 s on both clocks between readings, each read in no time."""
        self.readings += 1
        monotonic = 100.0 + 600 * self.readings
        return ClockReading(monotonic, 1_000_000 + 600_000 * self.readings, monotonic)

    def clock_jumps(self, since: float, until: float) -> int:
        return 0


def _cells_log(model: str, pods: list[str]) -> list[str]:
    return [
        f"reach {model} {pods}",
        f"warm {pods}",
        "cell 256 uncached",
        "cell 256 full",
    ]


def _model_log(model: str, profile: str, first: int) -> list[str]:
    """One model's files from a fresh deploy whose pods are pod-first and pod-first+1."""
    kept = f"pod-{first}"
    return [
        f"deploy {profile}",
        "scale 1",
        *_cells_log(model, [kept]),
        "scale 2",
        *_cells_log(model, [kept, f"pod-{first + 2}"]),
        "scale 1",
        "scale 2",
        f"reach {model} ['pod-{first + 3}']",
        f"warm ['pod-{first + 3}']",
        "scale 1",
        "scale 2",
        f"reach {model} ['pod-{first + 4}']",
        f"warm ['pod-{first + 4}']",
        "restart",
        f"reach {model} ['pod-{first + 5}', 'pod-{first + 6}']",
        f"warm ['pod-{first + 5}', 'pod-{first + 6}']",
    ]


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as file:
        return list(csv.DictReader(file))


def test_a_fresh_run_measures_every_file_in_order_and_ends_on_real(tmp_path: Path) -> None:
    calibration = _calibration()
    cluster = FakeCluster(calibration)
    planned = outputs(calibration, tmp_path)
    wrote = run_calibration(calibration, planned, cluster, SHA)
    assert wrote == planned
    assert cluster.log == [
        "host",
        *_model_log("lora", "real", 1),
        *_model_log("plain", "real-plain", 8),
        "deploy real",
    ]
    for out in planned:
        assert len(_rows(out.path)) == out.rows
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        [out.path.name for out in planned] + [f"{out.path.stem}.meta.json" for out in planned]
    )


def test_each_pod_gets_its_own_rows_in_the_grids_order(tmp_path: Path) -> None:
    calibration = _calibration()
    planned = outputs(calibration, tmp_path)
    run_calibration(calibration, planned, FakeCluster(calibration), SHA)
    rows = _rows(planned[1].path)
    assert [(row["pod"], row["cache"]) for row in rows] == [
        ("pod-1", "uncached"),
        ("pod-3", "uncached"),
        ("pod-1", "full"),
        ("pod-3", "full"),
    ]
    assert rows[0]["ttft_ms_p50"] == "400"
    assert rows[0]["per_token_ms_p50"] == "150.0"


def test_cold_start_rows_name_the_started_pods_with_their_times(tmp_path: Path) -> None:
    calibration = _calibration()
    planned = outputs(calibration, tmp_path)
    run_calibration(calibration, planned, FakeCluster(calibration), SHA)
    rows = _rows(planned[2].path)
    assert [(row["restart"], row["case"], row["pod"]) for row in rows] == [
        ("1", "alone", "pod-4"),
        ("2", "alone", "pod-5"),
        ("1", "both", "pod-6"),
        ("1", "both", "pod-7"),
    ]
    assert rows[0]["created_to_ready_s"] == "125.0"
    assert rows[0]["ready_to_first_answer_s"] == "30.0"
    assert rows[0]["first_ttft_ms"] == "26000"
    assert rows[0]["warmup_calls"] == "2"


def test_a_files_meta_holds_its_model_profile_and_clock_drift(tmp_path: Path) -> None:
    calibration = _calibration()
    planned = outputs(calibration, tmp_path)
    run_calibration(calibration, planned, FakeCluster(calibration), SHA)
    meta = json.loads(planned[3].path.with_name(f"{planned[3].path.stem}.meta.json").read_text())
    assert meta["hash"] == calibration.hash
    assert meta["model"] == "plain"
    assert meta["profile"] == "real-plain"
    assert meta["profile_sha256"] == SHA["plain"]
    assert meta["pods"] == ["pod-8"]
    assert meta["warmup_ttft_ms"] == {"pod-8": [26000, 900]}
    assert meta["drift"] == {"share": 0.0, "uncertainty": 0.0}


def test_a_rerun_with_every_file_finished_touches_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calibration = _calibration()
    planned = outputs(calibration, tmp_path)
    run_calibration(calibration, planned, FakeCluster(calibration), SHA)
    again = FakeCluster(calibration)
    assert run_calibration(calibration, planned, again, SHA) == []
    assert again.log == []
    assert "every file is finished" in capsys.readouterr().out


def test_a_rerun_measures_only_the_missing_file(tmp_path: Path) -> None:
    calibration = _calibration()
    planned = outputs(calibration, tmp_path)
    run_calibration(calibration, planned, FakeCluster(calibration), SHA)
    planned[4].path.unlink()
    again = FakeCluster(calibration)
    assert run_calibration(calibration, planned, again, SHA) == [planned[4]]
    assert again.log == [
        "host",
        "deploy real-plain",
        "scale 2",
        *_cells_log("plain", ["pod-1", "pod-2"]),
        "deploy real",
    ]


def test_a_host_refusal_changes_nothing(tmp_path: Path) -> None:
    calibration = _calibration()
    cluster = FakeCluster(calibration, refuse=True)
    with pytest.raises(CalibrationError, match="power plan"):
        run_calibration(calibration, outputs(calibration, tmp_path), cluster, SHA)
    assert cluster.log == ["host"]
    assert list(tmp_path.iterdir()) == []


def test_a_failure_keeps_the_partial_file_and_puts_real_back(tmp_path: Path) -> None:
    calibration = _calibration()
    planned = outputs(calibration, tmp_path)
    cluster = FakeCluster(calibration, fail_at=("lora", 2, calibration.cells[1]))
    with pytest.raises(CalibrationError, match="no full reply"):
        run_calibration(calibration, planned, cluster, SHA)
    assert cluster.log[-2:] == ["cell 256 full", "deploy real"]
    assert planned[0].path.exists()
    assert not planned[1].path.exists()
    partial = planned[1].path.with_name(f"{planned[1].path.name}.partial")
    assert len(_rows(partial)) == 2


def test_a_rerun_redoes_a_partial_file_from_its_first_cell(tmp_path: Path) -> None:
    calibration = _calibration()
    planned = outputs(calibration, tmp_path)
    failing = FakeCluster(calibration, fail_at=("lora", 2, calibration.cells[1]))
    with pytest.raises(CalibrationError):
        run_calibration(calibration, planned, failing, SHA)
    again = FakeCluster(calibration)
    run_calibration(calibration, planned, again, SHA)
    assert again.log[:4] == ["host", "deploy real", "scale 2", "reach lora ['pod-1', 'pod-2']"]
    assert len(_rows(planned[1].path)) == planned[1].rows
    assert not planned[1].path.with_name(f"{planned[1].path.name}.partial").exists()


def test_an_alone_start_that_adds_no_pod_stops_the_run(tmp_path: Path) -> None:
    calibration = dataclasses.replace(_calibration(), pods=(2,))
    cluster = FakeCluster(calibration, add_no_pod=True)
    with pytest.raises(CalibrationError, match="added 0 pods"):
        run_calibration(calibration, outputs(calibration, tmp_path), cluster, SHA)
    assert cluster.log[-1] == "deploy real"


# The pods: the closed loop, the warm-up and one call.


def test_closed_loop_keeps_concurrency_in_flight_and_counts_the_tail() -> None:
    in_flight = 0
    most = 0
    called: list[int] = []

    async def call(index: int) -> int:
        nonlocal in_flight, most
        called.append(index)
        in_flight += 1
        most = max(most, in_flight)
        for _ in range(3):
            await asyncio.sleep(0)
        in_flight -= 1
        return index

    results = asyncio.run(closed_loop(call, concurrency=2, calls=5))
    assert sorted(called) == [0, 1, 2, 3, 4]
    assert most == 2
    assert results == [(0, 2), (1, 2), (2, 2), (3, 2), (4, 1)]


def test_closed_loop_with_more_streams_than_calls_sends_each_call_once() -> None:
    async def call(index: int) -> int:
        await asyncio.sleep(0)
        return index

    results = asyncio.run(closed_loop(call, concurrency=8, calls=3))
    assert [index for index, _ in results] == [0, 1, 2]


WARMUP = Warmup(prompt_tokens=1024, settled_ttft_ms=1000, max_calls=4)


def test_warm_pod_calls_until_one_is_fast() -> None:
    ttfts = iter([25000, 14900, 900, 800])
    indexes: list[int] = []

    async def call(index: int) -> WarmCall:
        indexes.append(index)
        return WarmCall(next(ttfts), float(index))

    calls = asyncio.run(warm_pod(call, WARMUP))
    assert [warm.ttft_ms for warm in calls] == [25000, 14900, 900]
    assert indexes == [0, 1, 2]


def test_warm_pod_still_slow_after_max_calls_stops_the_run() -> None:
    async def call(index: int) -> WarmCall:
        return WarmCall(5000, float(index))

    with pytest.raises(CalibrationError, match="4 warm-up calls"):
        asyncio.run(warm_pod(call, WARMUP))


POD = "vllm-abc"


def _event(chunk: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(chunk)}\n\n".encode()


def _reply(
    body: dict[str, Any], pod: str = POD, cached: int | None = 32, prompt_tokens: int = 256
) -> AsyncIterator[bytes]:
    async def stream() -> AsyncIterator[bytes]:
        yield _event({"choices": [{"delta": {"role": "assistant", "content": ""}}]})
        yield _event({"choices": [{"delta": {"content": "x"}}]})
        usage: dict[str, Any] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": body["max_tokens"],
        }
        if cached is not None:
            usage["prompt_tokens_details"] = {"cached_tokens": cached}
        yield _event({"choices": [], "usage": usage, "system_fingerprint": pod})
        yield b"data: [DONE]\n\n"

    return stream()


def _call(
    prompt: CallPrompt, seen: list[dict[str, Any]], **reply: Any
) -> tuple[list[dict[str, Any]], Any]:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append({"body": body, "headers": dict(request.headers)})
        return httpx.Response(200, content=_reply(body, **reply))

    async def go() -> Any:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(base_url="http://pod", transport=transport) as client:
            return await measured_call(
                client,
                "thesis-adapter",
                POD,
                prompt,
                completion_tokens=64,
                prompt_tokens=256,
                headers={"x-run-id": "calibrate", "x-step": "1", "x-expected-steps": "20"},
                timeout_seconds=300,
            )

    return seen, asyncio.run(go())


def test_an_uncached_call_is_sent_alone() -> None:
    seen, reply = _call(CallPrompt(primer=None, prompt="a b c"), [])
    assert len(seen) == 1
    assert seen[0]["body"]["messages"][0]["content"] == "a b c"
    assert seen[0]["body"]["max_tokens"] == 64
    assert seen[0]["body"]["model"] == "thesis-adapter"
    assert seen[0]["headers"]["x-run-id"] == "calibrate"
    assert reply.cached_tokens == 32
    assert reply.completion_tokens == 64


def test_a_cached_call_sends_its_primer_first_for_one_token() -> None:
    seen, _ = _call(CallPrompt(primer="a b", prompt="a b c"), [])
    assert [call["body"]["messages"][0]["content"] for call in seen] == ["a b", "a b c"]
    assert [call["body"]["max_tokens"] for call in seen] == [1, 64]


@pytest.mark.parametrize(
    ("reply", "named"),
    [
        ({"pod": "vllm-other"}, "vllm-other"),
        ({"cached": None}, "cached_tokens"),
        ({"prompt_tokens": 255}, "255"),
    ],
)
def test_a_reply_that_is_not_this_cells_is_refused(reply: dict[str, Any], named: str) -> None:
    with pytest.raises(CalibrationError, match=named):
        _call(CallPrompt(primer=None, prompt="a b c"), [], **reply)


def test_a_failed_call_stops_the_run_with_the_pods_name() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async def go() -> None:
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(base_url="http://pod", transport=transport) as client:
            await measured_call(
                client, "m", POD, CallPrompt(None, "a"), 64, 256, {"x-run-id": "c"}, 300
            )

    with pytest.raises(CalibrationError, match=POD):
        asyncio.run(go())


# What the live cluster reads from kubectl.


def _pod(name: str, ready: bool, deleting: bool = False) -> dict[str, Any]:
    metadata: dict[str, Any] = {"name": name}
    if deleting:
        metadata["deletionTimestamp"] = READY
    return {
        "metadata": metadata,
        "status": {
            "conditions": [
                {"type": "PodScheduled", "status": "True"},
                {"type": "Ready", "status": str(ready)},
            ]
        },
    }


def test_ready_pods_leave_out_pods_not_ready_or_being_deleted() -> None:
    pods = {
        "items": [
            _pod("vllm-b", ready=True),
            _pod("vllm-a", ready=True),
            _pod("vllm-c", ready=False),
            _pod("vllm-d", ready=True, deleting=True),
        ]
    }
    assert ready_pods(pods, 2) == ["vllm-a", "vllm-b"]
    with pytest.raises(CalibrationError, match="replicas 3"):
        ready_pods(pods, 3)


def test_the_forwarded_port_is_read_from_kubectls_first_line() -> None:
    assert forwarded_port("Forwarding from 127.0.0.1:39129 -> 8000\n") == 39129
    with pytest.raises(CalibrationError, match="pods"):
        forwarded_port('error: pods "vllm-x" not found\n')


def test_the_lora_profile_serves_its_adapter_and_the_plain_one_does_not() -> None:
    base = {"name": "Qwen/Qwen3-0.6B-GPTQ-Int8"}
    assert expected_models(base) == {"Qwen/Qwen3-0.6B-GPTQ-Int8"}
    lora = {**base, "lora": {"name": "thesis-5554fcf81b09"}}
    assert expected_models(lora) == {"Qwen/Qwen3-0.6B-GPTQ-Int8", "thesis-5554fcf81b09"}
