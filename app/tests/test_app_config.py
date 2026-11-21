"""The app section of the config/ profiles (P2.10a, D11)."""

from pathlib import Path

import pytest
import yaml
from app_backend.config import DemoPattern, load_config, load_demo_config
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
    assert config.runtime_timeout_seconds > 0


@pytest.mark.parametrize("profile", PROFILES)
def test_the_file_limit_is_the_agents(profile: str) -> None:
    """The app never sends a run more files than the agent runtime accepts."""
    path = CONFIG_DIR / f"{profile}.yaml"
    assert load_config(path).max_files == yaml.safe_load(path.read_text())["agent"]["max_files"]


@pytest.mark.parametrize("profile", PROFILES)
def test_the_cluster_poll_is_the_scrape_interval(profile: str) -> None:
    """The panel asks as often as Prometheus has something new (P3.11)."""
    path = CONFIG_DIR / f"{profile}.yaml"
    scrape = yaml.safe_load(path.read_text())["monitoring"]["scrape_interval_seconds"]
    assert load_config(path).cluster_poll_seconds == scrape


@pytest.mark.parametrize("profile", PROFILES)
def test_the_kv_cache_is_the_models(profile: str) -> None:
    """The panel's cache pressure divides by the pods' KV cache the profile states (P4.11)."""
    path = CONFIG_DIR / f"{profile}.yaml"
    kv_tokens = yaml.safe_load(path.read_text())["model"]["kv_cache_tokens"]
    assert load_config(path).kv_cache_tokens == kv_tokens


@pytest.mark.parametrize("profile", PROFILES)
def test_the_pressure_warning_is_positive(profile: str) -> None:
    assert load_config(CONFIG_DIR / f"{profile}.yaml").pressure_warn > 0


@pytest.mark.parametrize(
    ("section", "key"), [("app", "pressure_warn"), ("model", "kv_cache_tokens")]
)
def test_a_missing_pressure_key_raises(tmp_path: Path, section: str, key: str) -> None:
    profile = yaml.safe_load((CONFIG_DIR / "sim.yaml").read_text())
    del profile[section][key]
    path = tmp_path / "broken.yaml"
    path.write_text(yaml.safe_dump(profile))
    with pytest.raises(KeyError, match=key):
        load_config(path)


def test_a_missing_key_raises(tmp_path: Path) -> None:
    profile = yaml.safe_load((CONFIG_DIR / "sim.yaml").read_text())
    del profile["app"]["min_password_length"]
    path = tmp_path / "broken.yaml"
    path.write_text(yaml.safe_dump(profile))
    with pytest.raises(KeyError, match="min_password_length"):
        load_config(path)


@pytest.mark.parametrize("profile", PROFILES)
def test_the_demo_buttons_are_on_in_real_only(profile: str) -> None:
    """A kill needs a second pod to fail over to, and sim has no app backend (P4.6a); real-plain
    is real without the adapter, so it follows real."""
    enabled = load_demo_config(CONFIG_DIR / f"{profile}.yaml").enabled
    assert enabled is (profile in ("real", "real-plain"))


@pytest.mark.parametrize("profile", PROFILES)
def test_the_demo_patterns_carry_their_replay_runs(profile: str) -> None:
    """Start's label names the runs the runner's replay will start."""
    path = CONFIG_DIR / f"{profile}.yaml"
    loaded = yaml.safe_load(path.read_text())
    assert load_demo_config(path).patterns == tuple(
        DemoPattern(name=name, runs=loaded["replay"]["patterns"][name]["runs"])
        for name in loaded["app"]["demo_controls"]["patterns"]
    )


def test_a_demo_pattern_that_is_not_a_replay_pattern_raises(tmp_path: Path) -> None:
    profile = yaml.safe_load((CONFIG_DIR / "real.yaml").read_text())
    profile["app"]["demo_controls"]["patterns"] = ["steady", "tidal"]
    path = tmp_path / "broken.yaml"
    path.write_text(yaml.safe_dump(profile))
    with pytest.raises(KeyError, match="tidal"):
        load_demo_config(path)
