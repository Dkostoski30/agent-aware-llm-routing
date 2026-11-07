"""Password hashes: hashlib.scrypt with a random salt per hash (P2.10a).

A stored hash is "scrypt$n$r$p$salt$key" (hex), so it carries its own cost: verifying needs no
config, and raising app.scrypt later does not lock out users whose hash used the old cost.
"""

import hashlib
import hmac
import secrets

from app_backend.config import ScryptParams

# 16 random bytes: the salt length NIST SP 800-132 asks as a minimum; two users with one password
# get different hashes.
SALT_BYTES = 16
# 64 bytes of derived key, the length RFC 7914's scrypt test vectors use.
KEY_BYTES = 64
_PREFIX = "scrypt"


def _derive(password: str, salt: bytes, params: ScryptParams) -> bytes:
    return hashlib.scrypt(
        password.encode(), salt=salt, n=params.n, r=params.r, p=params.p, dklen=KEY_BYTES
    )


def hash_password(password: str, params: ScryptParams) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    key = _derive(password, salt, params)
    return "$".join([_PREFIX, str(params.n), str(params.r), str(params.p), salt.hex(), key.hex()])


def verify_password(password: str, stored: str) -> bool:
    """Derives the key again with the stored salt and cost; compares in constant time."""
    prefix, n, r, p, salt, key = stored.split("$")
    if prefix != _PREFIX:
        raise ValueError(f"not a {_PREFIX} hash: {prefix!r}")
    derived = _derive(password, bytes.fromhex(salt), ScryptParams(n=int(n), r=int(r), p=int(p)))
    return hmac.compare_digest(derived, bytes.fromhex(key))
