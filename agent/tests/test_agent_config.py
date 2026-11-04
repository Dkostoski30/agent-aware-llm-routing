"""Loading the agent section of the config/ profiles (P1.3a-1, D10, D11)."""

from pathlib import Path

import pytest
import yaml
from agent_runtime.config import load_config

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


@pytest.mark.parametrize(
    ("profile", "tool_choice"),
    [("real", "auto"), ("sim", "auto"), ("demo", "required")],
)
def test_profile_loads_with_its_d10_mode(profile: str, tool_choice: str) -> None:
    path = CONFIG_DIR / f"{profile}.yaml"
    config = load_config(path)
    assert config.tool_choice == tool_choice
    model = yaml.safe_load(path.read_text())["model"]
    assert config.model_name == (model["lora"]["name"] if "lora" in model else model["name"])


def profile_with_model(tmp_path: Path, model: dict[str, object]) -> Path:
    profile = yaml.safe_load((CONFIG_DIR / "real.yaml").read_text())
    profile["model"] = model
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(profile))
    return path


def test_with_an_adapter_the_agent_names_the_adapter(tmp_path: Path) -> None:
    """vLLM serves a LoRA adapter under its own name next to the base model; a request that names
    the base model gets no adapter (P1.7)."""
    lora = {"name": "thesis-b03ea3894e7c", "path": "/adapters", "sha256": "b0" * 32, "max_rank": 16}
    path = profile_with_model(tmp_path, {"name": "Qwen/Qwen3-0.6B-GPTQ-Int8", "lora": lora})
    assert load_config(path).model_name == "thesis-b03ea3894e7c"


def test_without_an_adapter_the_agent_names_the_model(tmp_path: Path) -> None:
    path = profile_with_model(tmp_path, {"name": "Qwen/Qwen3-0.6B-GPTQ-Int8"})
    assert load_config(path).model_name == "Qwen/Qwen3-0.6B-GPTQ-Int8"


def test_every_profile_sends_the_same_system_prompt() -> None:
    prompts = {load_config(CONFIG_DIR / f"{p}.yaml").system_prompt for p in ("real", "sim", "demo")}
    assert len(prompts) == 1


def test_unknown_tool_choice_is_rejected(tmp_path: Path) -> None:
    profile = yaml.safe_load((CONFIG_DIR / "real.yaml").read_text())
    profile["agent"]["tool_choice"] = "none"
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(profile))
    with pytest.raises(ValueError, match="tool_choice"):
        load_config(path)


@pytest.mark.parametrize("profile", ["real", "sim", "demo"])
def test_profile_sets_the_file_and_result_limits(profile: str) -> None:
    config = load_config(CONFIG_DIR / f"{profile}.yaml")
    assert config.max_file_bytes > 0
    assert config.max_files > 0
    assert config.max_result_rows > 0
    assert config.max_result_chars > 0
    assert config.result_decimals >= 0


@pytest.mark.parametrize("profile", ["real", "sim", "demo"])
def test_profile_sets_the_events_poll_interval(profile: str) -> None:
    assert load_config(CONFIG_DIR / f"{profile}.yaml").events_poll_seconds > 0


@pytest.mark.parametrize("profile", ["real", "sim", "demo"])
def test_profile_sets_the_contracts_tools_limits(profile: str) -> None:
    config = load_config(CONFIG_DIR / f"{profile}.yaml")
    assert config.max_clause_chars > 0
    assert config.max_diff_changes > 0
    assert config.diff_context_words >= 0
