"""The training run of P1.7 step 3: a LoRA adapter for the base model, trained on the examples of
make finetune-data (docs/task-cards/19-P1.7-fine-tune-tool-calls.md).

1. Refuses data of another hash than data.hash, and a GPU with less than train.min_free_mib free.
2. Renders and tokenizes every example (render.py), refusing one longer than train.max_length.
3. Trains with transformers' Trainer, the loss on the reply's tokens only (spike.reply_loss). The
   run's directory, <output_dir>/runs/<sha12 of this config>/, holds the checkpoints and loss.jsonl
   (one line per log step: tail -f to watch it); a run with checkpoints there resumes from the
   newest, so a crash or Ctrl+C costs at most train.save_steps.
4. Saves the adapter to <output_dir>/adapters/<sha12 of its weights>/ and prints the path, the
   sha256, the peak VRAM and the time this run took.

Run it with the cluster down (make down): the vLLM pods hold the GPU.

Usage: train.py EXPERIMENT_YAML   (experiments/finetune-train.yaml)
"""

import argparse
import hashlib
import json
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import torch
import yaml
from peft import LoraConfig, PeftModel, get_peft_model
from render import data_dir, render
from spike import MIB, REPO_ROOT, free_mib, reply_loss, sha256_of
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    PreTrainedTokenizerBase,
    Trainer,
    TrainerCallback,
    TrainerControl,
    TrainerState,
    TrainingArguments,
    set_seed,
)
from transformers.trainer_utils import PREFIX_CHECKPOINT_DIR

# The file peft saves an adapter's weights to; config/real.yaml pins the adapter by its sha256.
ADAPTER_WEIGHTS = "adapter_model.safetensors"


@dataclass(frozen=True)
class Encoded:
    input_ids: list[int]
    # The last reply_tokens of input_ids are the reply, the tokens the loss counts.
    reply_tokens: int


def encode(tokenizer: PreTrainedTokenizerBase, example: dict[str, Any], max_length: int) -> Encoded:
    """The example's prompt tokens, then its reply's, each tokenized on its own: vLLM tokenizes the
    prompt, and the model writes the reply after it. Raises ValueError when they are more than
    max_length tokens."""
    rendered = render(tokenizer, example)
    prompt: list[int] = tokenizer(rendered.prompt)["input_ids"]
    reply: list[int] = tokenizer(rendered.reply)["input_ids"]
    length = len(prompt) + len(reply)
    if length > max_length:
        raise ValueError(
            f"{example['id']}: {length} tokens, more than train.max_length {max_length}"
        )
    return Encoded(prompt + reply, len(reply))


class Examples(torch.utils.data.Dataset[Encoded]):
    def __init__(self, encoded: list[Encoded]) -> None:
        self._encoded = encoded

    def __len__(self) -> int:
        return len(self._encoded)

    def __getitem__(self, index: int) -> Encoded:
        return self._encoded[index]


def collate(batch: list[Encoded]) -> dict[str, Any]:
    """One example as the model's input, and how many of its last tokens are the reply."""
    (encoded,) = batch
    return {"input_ids": torch.tensor([encoded.input_ids]), "reply_tokens": encoded.reply_tokens}


class ReplyTrainer(Trainer):
    """A Trainer whose loss is the cross-entropy of the reply's tokens (spike.reply_loss), with the
    logits computed for those only: for all positions they do not fit (P1.7 step 1)."""

    # The loss is a mean over one example's reply, so training_step divides it by the examples
    # accumulated per optimizer step (Trainer's docstring).
    loss_is_scaled_for_ga = False

    def compute_loss(
        self,
        model: torch.nn.Module,
        inputs: dict[str, torch.Tensor | Any],
        return_outputs: bool = False,
        num_items_in_batch: torch.Tensor | int | None = None,
    ) -> torch.Tensor:
        # return_outputs is for evaluation, which the run has none of.
        if not isinstance(model, PeftModel):
            raise TypeError(f"the model is a {type(model).__name__}, not the LoRA model")
        # collate passes reply_tokens as an int, and Trainer moves only tensors to the GPU.
        return reply_loss(model, inputs["input_ids"], int(inputs["reply_tokens"]))


class LossLog(TrainerCallback):
    """Appends every log step's line (the loss, the learning rate) to a JSONL file, so a resumed
    run continues the same file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def on_log(
        self,
        args: TrainingArguments,
        state: TrainerState,
        control: TrainerControl,
        logs: dict[str, float] | None = None,
        **kwargs: Any,
    ) -> None:
        line = {"step": state.global_step, "epoch": state.epoch, **(logs or {})}
        with self._path.open("a") as out:
            out.write(json.dumps(line) + "\n")


def check_data(data: Path, expected_hash: str) -> Path:
    """The training examples in data, the directory make finetune-data writes for the data
    experiment file as it is now. Raises ValueError when that file's hash is not expected_hash or
    the examples are missing."""
    if data.name != expected_hash:
        raise ValueError(
            f"the data experiment file's hash is {data.name}, not data.hash {expected_hash}: it "
            "changed since the baseline was scored"
        )
    examples = data / "train.jsonl"
    if not examples.exists():
        raise ValueError(f"{examples} does not exist: run make finetune-data first")
    return examples


def run_dir(output_dir: Path, config: Path) -> Path:
    """<output_dir>/runs/<first 12 hex digits of the config's sha256>: a changed config never
    resumes another's checkpoints."""
    return output_dir / "runs" / hashlib.sha256(config.read_bytes()).hexdigest()[:12]


def resume_point(run: Path) -> Path | None:
    """The run's newest checkpoint, by step (Trainer names them checkpoint-<step>), or None for a
    run that has none yet."""
    steps = [int(path.name.split("-")[1]) for path in run.glob(f"{PREFIX_CHECKPOINT_DIR}-*")]
    return run / f"{PREFIX_CHECKPOINT_DIR}-{max(steps)}" if steps else None


def publish_adapter(saved: Path, adapters: Path) -> Path:
    """Moves a saved adapter to adapters/<first 12 hex digits of its weights' sha256>/ and returns
    that path. When it is already there (the same weights), the saved copy is removed."""
    published = adapters / sha256_of(saved / ADAPTER_WEIGHTS)[:12]
    if published.exists():
        shutil.rmtree(saved)
    else:
        adapters.mkdir(parents=True, exist_ok=True)
        saved.rename(published)
    return published


def check_free_gpu(free_mib: float, needed_mib: int) -> None:
    """Exits when less than needed_mib of GPU memory is free: under WSL, CUDA would spill into
    system memory instead of failing and train many times slower (P1.7 step 1)."""
    if free_mib < needed_mib:
        sys.exit(
            f"finetune: {free_mib:.0f} MiB of GPU memory free, train.min_free_mib is {needed_mib}: "
            "a vLLM container or pod holds it, run make down (or make vllm-stop) first"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="P1.7 step 3: train the LoRA adapter")
    parser.add_argument("experiment", type=Path, help="experiments/finetune-train.yaml")
    config: Path = parser.parse_args().experiment
    spec = yaml.safe_load(config.read_text())
    train = spec["train"]
    profile = yaml.safe_load((REPO_ROOT / "config" / f"{spec['served_profile']}.yaml").read_text())
    served = profile["model"]
    output_dir = Path(spec["output_dir"]).expanduser()
    run = run_dir(output_dir, config)

    examples_path = check_data(
        data_dir(REPO_ROOT / spec["data"]["experiment"]), spec["data"]["hash"]
    )
    check_free_gpu(free_mib(), train["min_free_mib"])
    # The served model's tokenizer and template, the ones vLLM uses (the spike checked they are
    # the base model's).
    tokenizer = AutoTokenizer.from_pretrained(served["name"], revision=served["revision"])
    examples = [
        encode(tokenizer, json.loads(line), train["max_length"])
        for line in examples_path.read_text().splitlines()
    ]
    print(f"finetune: {len(examples)} examples from {examples_path}, run in {run}", flush=True)

    # Before the LoRA weights are made, so their start is the seed's.
    set_seed(train["seed"])
    base = spec["base"]
    model = AutoModelForCausalLM.from_pretrained(
        base["name"], revision=base["revision"], dtype=torch.bfloat16, device_map="cuda"
    )
    # As in the spike: recompute activations in the backward pass instead of keeping them, which
    # is what fits 4096 tokens; the cache only serves generation.
    model.config.use_cache = False
    model.gradient_checkpointing_enable()
    lora = spec["lora"]
    peft_model = get_peft_model(
        model,
        LoraConfig(
            r=lora["rank"],
            lora_alpha=lora["alpha"],
            lora_dropout=lora["dropout"],
            target_modules=lora["target_modules"],
            task_type="CAUSAL_LM",
        ),
    )
    trainer = ReplyTrainer(
        model=peft_model,
        args=TrainingArguments(
            output_dir=str(run),
            num_train_epochs=train["epochs"],
            learning_rate=train["learning_rate"],
            lr_scheduler_type="linear",
            warmup_steps=train["warmup"],
            # One example per forward pass (experiments/finetune-train.yaml, train).
            per_device_train_batch_size=1,
            gradient_accumulation_steps=train["gradient_accumulation"],
            logging_steps=train["logging_steps"],
            # The loss before training has moved anything: the baseline of the loss curve.
            logging_first_step=True,
            save_steps=train["save_steps"],
            save_total_limit=train["checkpoints_kept"],
            seed=train["seed"],
            # The model is loaded in bf16, as in the spike.
            bf16=True,
            # Otherwise Trainer drops reply_tokens from the batch: the model's forward has no such
            # argument.
            remove_unused_columns=False,
        ),
        train_dataset=Examples(examples),
        data_collator=collate,
        callbacks=[LossLog(run / "loss.jsonl")],
    )

    resume = resume_point(run)
    print(
        f"finetune: resuming from {resume}" if resume else "finetune: starting at step 0",
        flush=True,
    )
    torch.cuda.reset_peak_memory_stats()
    started = time.perf_counter()
    trainer.train(resume_from_checkpoint=None if resume is None else str(resume))
    minutes = (time.perf_counter() - started) / 60
    losses = [entry["loss"] for entry in trainer.state.log_history if "loss" in entry]
    print(f"finetune: loss {losses[0]:.4g} -> {losses[-1]:.4g} (log in {run / 'loss.jsonl'})")

    saved = run / "adapter"
    peft_model.save_pretrained(str(saved))
    adapter = publish_adapter(saved, output_dir / "adapters")
    print(f"finetune: adapter in {adapter}, sha256 {sha256_of(adapter / ADAPTER_WEIGHTS)}")
    print(
        f"finetune: peak reserved by torch {torch.cuda.max_memory_reserved() / MIB:.0f} MiB, "
        f"{minutes:.1f} min this run (a resumed run counts from where it resumed)"
    )


if __name__ == "__main__":
    main()
