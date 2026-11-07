"""The app section of the config/ profiles (P2.10a, D11)."""

from pathlib import Path

import pytest
import yaml
from app_backend.config import load_config
from app_backend.passwords import hash_password, verify_password

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
PROFILES = ["real", "real-plain", "sim", "demo"]


@pytest.mark.parametrize("profile", PROFILES)
def test_each_profile_loads_and_its_scrypt_cost_hashes(profile: str) -> None:
    """hashlib.scrypt refuses a cost over OpenSSL's memory limit; this fails before a user does."""
    config = load_config(CONFIG_DIR / f"{profile}.yaml")
    password = "p" * config.min_password_length
    assert verify_password(password, hash_password(password, config.scrypt))
    assert config.session_ttl_seconds > 0


def test_a_missing_key_raises(tmp_path: Path) -> None:
    profile = yaml.safe_load((CONFIG_DIR / "sim.yaml").read_text())
    del profile["app"]["min_password_length"]
    path = tmp_path / "broken.yaml"
    path.write_text(yaml.safe_dump(profile))
    with pytest.raises(KeyError, match="min_password_length"):
        load_config(path)
