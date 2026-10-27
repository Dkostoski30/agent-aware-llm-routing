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
    assert config.model_name == yaml.safe_load(path.read_text())["model"]["name"]


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
