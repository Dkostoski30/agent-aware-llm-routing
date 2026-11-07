"""The app backend's settings, read from the app section of a config/ profile (D11)."""

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ScryptParams:
    """hashlib.scrypt's cost: n (CPU and memory), r (block size), p (parallel lanes)."""

    n: int
    r: int
    p: int


@dataclass(frozen=True)
class AppConfig:
    session_ttl_seconds: int
    scrypt: ScryptParams
    min_password_length: int
    cookie_secure: bool


def load_config(path: Path) -> AppConfig:
    """Reads the app section of a profile; a missing key raises KeyError."""
    app = yaml.safe_load(path.read_text())["app"]
    return AppConfig(
        session_ttl_seconds=app["session_ttl_seconds"],
        scrypt=ScryptParams(n=app["scrypt"]["n"], r=app["scrypt"]["r"], p=app["scrypt"]["p"]),
        min_password_length=app["min_password_length"],
        cookie_secure=app["cookie_secure"],
    )
