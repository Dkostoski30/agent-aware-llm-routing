"""The parts of the P1.7 training run (step 3) that run without a GPU: what an example becomes,
which data and run directory a config trains in, where a run resumes, where the adapter goes, and
the loss log."""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from render import render
from train import (
    LossLog,
    check_data,
    check_free_gpu,
    collate,
    encode,
    publish_adapter,
    resume_point,
    run_dir,
)
from transformers import (
    AutoTokenizer,
    PreTrainedTokenizerBase,
    TrainerControl,
    TrainerState,
    TrainingArguments,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE = yaml.safe_load((REPO_ROOT / "config" / "real.yaml").read_text())

EXAMPLE: dict[str, Any] = {
    "id": "train-arithmetic-0001-auto-1",
    "tools": [],
    "messages": [
        {"role": "system", "content": "You are a data analysis agent."},
        {"role": "user", "content": "Hi"},
    ],
    "reply": {"role": "assistant", "content": "I can only do arithmetic."},
}


@pytest.fixture(scope="module")
def tokenizer() -> PreTrainedTokenizerBase:
    model = PROFILE["model"]
    loaded: PreTrainedTokenizerBase = AutoTokenizer.from_pretrained(
        model["name"], revision=model["revision"]
    )
    return loaded


def test_the_last_reply_tokens_are_the_reply_and_the_rest_the_prompt(
    tokenizer: PreTrainedTokenizerBase,
) -> None:
    """The loss counts the reply only (spike.reply_loss), so the count must cut exactly there."""
    rendered = render(tokenizer, EXAMPLE)
    encoded = encode(tokenizer, EXAMPLE, max_length=4096)
    prompt, reply = (
        encoded.input_ids[: -encoded.reply_tokens],
        encoded.input_ids[-encoded.reply_tokens :],
    )
    assert tokenizer.decode(reply) == rendered.reply
    assert tokenizer.decode(prompt) == rendered.prompt


def test_an_example_longer_than_max_length_is_refused(tokenizer: PreTrainedTokenizerBase) -> None:
    """Cutting it would train on a prompt vLLM never sends."""
    length = len(encode(tokenizer, EXAMPLE, max_length=4096).input_ids)
    with pytest.raises(ValueError, match=EXAMPLE["id"]):
        encode(tokenizer, EXAMPLE, max_length=length - 1)
    assert len(encode(tokenizer, EXAMPLE, max_length=length).input_ids) == length


def test_collate_makes_a_batch_of_one(tokenizer: PreTrainedTokenizerBase) -> None:
    encoded = encode(tokenizer, EXAMPLE, max_length=4096)
    batch = collate([encoded])
    assert batch["input_ids"].tolist() == [encoded.input_ids]
    assert batch["reply_tokens"] == encoded.reply_tokens


def test_check_data_gives_the_training_examples_of_the_configured_hash(tmp_path: Path) -> None:
    data = tmp_path / "6e3ac3eda752"
    data.mkdir()
    (data / "train.jsonl").write_text("{}\n")
    assert check_data(data, "6e3ac3eda752") == data / "train.jsonl"


def test_check_data_refuses_data_of_another_hash(tmp_path: Path) -> None:
    """experiments/finetune-data.yaml changed since the config named its hash: training on it would
    not be the data the baseline was scored against."""
    data = tmp_path / "d384c6bd2721"
    data.mkdir()
    (data / "train.jsonl").write_text("{}\n")
    with pytest.raises(ValueError, match="6e3ac3eda752"):
        check_data(data, "6e3ac3eda752")


def test_check_data_refuses_when_make_finetune_data_has_not_run(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="make finetune-data"):
        check_data(tmp_path / "6e3ac3eda752", "6e3ac3eda752")


def test_the_run_directory_is_named_after_the_config(tmp_path: Path) -> None:
    """A changed config never resumes the checkpoints of another."""
    config = tmp_path / "finetune-train.yaml"
    config.write_text("train: {epochs: 2}\n")
    first = run_dir(tmp_path / "out", config)
    assert first == run_dir(tmp_path / "out", config)
    assert first.parent == tmp_path / "out" / "runs"
    config.write_text("train: {epochs: 3}\n")
    assert run_dir(tmp_path / "out", config) != first


def test_a_new_run_starts_from_the_beginning(tmp_path: Path) -> None:
    assert resume_point(tmp_path / "run") is None
    (tmp_path / "run").mkdir()
    assert resume_point(tmp_path / "run") is None


def test_a_run_resumes_from_its_newest_checkpoint(tmp_path: Path) -> None:
    """Newest by step, not by name: checkpoint-50 sorts after checkpoint-100 as text."""
    for step in (50, 100):
        (tmp_path / f"checkpoint-{step}").mkdir()
    assert resume_point(tmp_path) == tmp_path / "checkpoint-100"


def test_the_adapter_is_published_under_its_weights_hash(tmp_path: Path) -> None:
    """config/real.yaml names the adapter by the sha256 of its weights (model.lora)."""
    saved = tmp_path / "run" / "adapter"
    saved.mkdir(parents=True)
    (saved / "adapter_model.safetensors").write_bytes(b"weights")
    (saved / "adapter_config.json").write_text("{}")
    published = publish_adapter(saved, tmp_path / "adapters")
    assert published == tmp_path / "adapters" / hashlib.sha256(b"weights").hexdigest()[:12]
    assert sorted(path.name for path in published.iterdir()) == [
        "adapter_config.json",
        "adapter_model.safetensors",
    ]
    assert not saved.exists()


def test_publishing_the_same_weights_again_keeps_the_published_adapter(tmp_path: Path) -> None:
    """A run of the same config and data on the same machine can end with the same weights."""
    published = []
    for run in ("first", "second"):
        saved = tmp_path / run / "adapter"
        saved.mkdir(parents=True)
        (saved / "adapter_model.safetensors").write_bytes(b"weights")
        published.append(publish_adapter(saved, tmp_path / "adapters"))
    assert published[0] == published[1]
    assert (published[0] / "adapter_model.safetensors").read_bytes() == b"weights"


def test_training_refuses_a_gpu_another_process_holds() -> None:
    """A vLLM container or pod keeps most of the 4 GB; under WSL CUDA would then spill into system
    memory and train many times slower instead of failing (P1.7 step 1)."""
    with pytest.raises(SystemExit, match="make down"):
        check_free_gpu(free_mib=800, needed_mib=2200)
    check_free_gpu(free_mib=3300, needed_mib=2200)


def test_the_loss_log_gets_a_line_per_log_step(tmp_path: Path) -> None:
    """Appended, so a resumed run continues the same file."""
    path = tmp_path / "loss.jsonl"
    log = LossLog(path)
    args = TrainingArguments(output_dir=str(tmp_path / "run"))
    for step, loss in ((1, 2.5), (10, 0.75)):
        state = TrainerState(global_step=step, epoch=step / 200)
        log.on_log(args, state, TrainerControl(), logs={"loss": loss, "learning_rate": 1e-4})
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [(line["step"], line["loss"]) for line in lines] == [(1, 2.5), (10, 0.75)]
    assert lines[1]["epoch"] == 0.05
    assert lines[1]["learning_rate"] == 1e-4
