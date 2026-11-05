"""config/real-plain.yaml (P2.8a, D11): the real profile without the LoRA adapter, for make
calibrate's comparison. Kept in step with config/real.yaml, so an edit to one fails here until the
other follows."""

from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def test_real_plain_is_real_without_the_adapter() -> None:
    real = yaml.safe_load((CONFIG_DIR / "real.yaml").read_text())
    plain = yaml.safe_load((CONFIG_DIR / "real-plain.yaml").read_text())
    assert "lora" in real["model"]
    assert "lora" not in plain["model"]
    del real["model"]["lora"]
    assert plain == real
