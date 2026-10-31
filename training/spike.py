"""Feasibility spike of P1.7 step 1: does LoRA training of the base model fit on the 4 GB GPU, and
how fast is it.

1. Compares tokenizer.json and tokenizer_config.json (which holds the chat template) of the base
   model and of the served profile's model, at their pinned revisions, by sha256: training renders
   prompts with the served one.
2. Loads the base model in bf16, adds LoRA, turns on gradient checkpointing, and runs a few AdamW
   steps at batch size 1 on one sample per length in spike.lengths, with the loss on its last
   spike.reply_tokens tokens as in training. Prints the peak VRAM and the seconds per step for each
   length, and whether torch's peak fits in the VRAM that was free before loading.
3. Saves the adapter to <output_dir>/spike-adapter, for make lora-check.

Exits 1 if a length does not fit: under WSL, CUDA then spills into shared system memory instead of
failing, so the step still runs, many times slower (P1.7 step 1, experiments/log.md).

Run it with the cluster down (make down): the vLLM pods would hold the GPU memory it measures.

Usage: spike.py EXPERIMENT_YAML
"""

import argparse
import hashlib
import statistics
import sys
import time
from pathlib import Path

import torch
import yaml
from huggingface_hub import hf_hub_download
from peft import LoraConfig, PeftModel, get_peft_model
from transformers import AutoModelForCausalLM, AutoTokenizer, PreTrainedTokenizerBase

REPO_ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024
# The files that make the tokens the model sees: the vocabulary and merges, and the chat template.
TOKENIZER_FILES = ("tokenizer.json", "tokenizer_config.json")


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compare_tokenizers(base: dict[str, str], served: dict[str, str]) -> None:
    """Prints the sha256 of every tokenizer file of both models and whether they match."""
    for filename in TOKENIZER_FILES:
        hashes = {
            model["name"]: sha256_of(
                Path(hf_hub_download(model["name"], filename, revision=model["revision"]))
            )
            for model in (base, served)
        }
        verdict = "same" if len(set(hashes.values())) == 1 else "DIFFERENT"
        print(f"finetune-spike: {filename} {verdict}: {hashes}", flush=True)


def sample_ids(tokenizer: PreTrainedTokenizerBase, text: str, length: int) -> torch.Tensor:
    """The tokens of text, repeated and cut to length, as a batch of one. VRAM and speed depend on
    the length, not on what the tokens say."""
    ids: list[int] = tokenizer(text)["input_ids"]
    repeats = -(-length // len(ids))
    return torch.tensor([(ids * repeats)[:length]])


def reply_loss(model: PeftModel, input_ids: torch.Tensor, reply_tokens: int) -> torch.Tensor:
    """Cross-entropy of the last reply_tokens tokens, the reply of a training example: the prompt
    is masked. lm_head runs only on the positions that predict them (logits_to_keep), so the logits
    are reply_tokens x vocabulary instead of length x vocabulary; for all positions they did not
    fit (2489319424 bytes in fp32 at 4096 tokens, experiments/log.md)."""
    logits = model(input_ids=input_ids, logits_to_keep=reply_tokens + 1).logits
    # Position i predicts token i + 1, so the last kept position predicts nothing.
    return torch.nn.functional.cross_entropy(logits[0, :-1].float(), input_ids[0, -reply_tokens:])


def free_mib() -> float:
    free, _ = torch.cuda.mem_get_info()
    return free / MIB


def device_used_mib() -> float:
    """Memory used on the whole GPU, by every process, the Windows desktop included."""
    free, total = torch.cuda.mem_get_info()
    return (total - free) / MIB


def main() -> None:
    parser = argparse.ArgumentParser(description="P1.7 step 1: LoRA feasibility spike")
    parser.add_argument("experiment", type=Path)
    args = parser.parse_args()
    spec = yaml.safe_load(args.experiment.read_text())
    profile_path = REPO_ROOT / "config" / f"{spec['served_profile']}.yaml"
    profile = yaml.safe_load(profile_path.read_text())
    base = {"name": spec["base"]["name"], "revision": spec["base"]["revision"]}
    served = {"name": profile["model"]["name"], "revision": profile["model"]["revision"]}

    compare_tokenizers(base, served)
    tokenizer = AutoTokenizer.from_pretrained(served["name"], revision=served["revision"])
    # The agent's system prompt, as text to tokenize; any text would do.
    text = profile["agent"]["system_prompt"]

    # After the CUDA context exists (mem_get_info creates it), so it is what torch can reserve.
    free_before = free_mib()
    print(
        f"finetune-spike: GPU {torch.cuda.get_device_name()}, {device_used_mib():.0f} MiB used, "
        f"{free_before:.0f} MiB free before loading (more than the desktop's few hundred MiB used "
        "is another process)",
        flush=True,
    )
    model = AutoModelForCausalLM.from_pretrained(
        base["name"], revision=base["revision"], dtype=torch.bfloat16, device_map="cuda"
    )
    # Recomputes each layer's activations in the backward pass instead of keeping them: memory
    # grows much slower with length, for about a third more compute. The cache only serves
    # generation.
    model.config.use_cache = False
    # transformers' default is non-reentrant checkpointing, which passes the gradient on even though
    # the frozen embeddings' output does not require one.
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
    trainable = sum(p.numel() for p in peft_model.parameters() if p.requires_grad)
    print(f"finetune-spike: {trainable:,} trainable LoRA parameters", flush=True)
    optimizer = torch.optim.AdamW(
        (p for p in peft_model.parameters() if p.requires_grad),
        lr=spec["spike"]["learning_rate"],
    )

    reply_tokens = spec["spike"]["reply_tokens"]
    too_long: list[int] = []
    peft_model.train()
    for length in spec["spike"]["lengths"]:
        input_ids = sample_ids(tokenizer, text, length).to("cuda")
        torch.cuda.reset_peak_memory_stats()
        seconds: list[float] = []
        losses: list[float] = []
        for _ in range(spec["spike"]["steps"]):
            started = time.perf_counter()
            loss = reply_loss(peft_model, input_ids, reply_tokens)
            # The same as loss.backward(), which torch leaves without type annotations.
            torch.autograd.backward(loss)
            optimizer.step()
            optimizer.zero_grad()
            torch.cuda.synchronize()
            seconds.append(time.perf_counter() - started)
            losses.append(loss.item())
        # torch keeps what it reserved, so the whole-GPU figure after the steps is torch's peak
        # plus the desktop.
        reserved = torch.cuda.max_memory_reserved() / MIB
        fits = reserved <= free_before
        if not fits:
            too_long.append(length)
        print(
            f"finetune-spike: length {length}: peak reserved by torch {reserved:.0f} MiB, "
            f"whole GPU {device_used_mib():.0f} MiB, "
            f"{statistics.fmean(seconds[1:]):.2f} s/step (steps 2-{len(seconds)}), "
            f"loss {losses[0]:.3f} -> {losses[-1]:.3f}: "
            + ("fits" if fits else f"DOES NOT FIT in the {free_before:.0f} MiB free"),
            flush=True,
        )

    adapter = Path(spec["output_dir"]).expanduser() / "spike-adapter"
    peft_model.save_pretrained(str(adapter))
    weights = adapter / "adapter_model.safetensors"
    print(f"finetune-spike: adapter in {adapter}, sha256 {sha256_of(weights)}", flush=True)
    if too_long:
        sys.exit(f"finetune-spike: FAIL: lengths {too_long} do not fit in VRAM")


if __name__ == "__main__":
    main()
