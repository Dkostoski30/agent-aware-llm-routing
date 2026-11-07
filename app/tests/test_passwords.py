"""Password hashes (P2.10a): scrypt with a random salt per hash, the cost stored with the hash."""

import pytest
from app_backend.config import ScryptParams
from app_backend.passwords import hash_password, verify_password

# The cheapest cost scrypt accepts, so the tests stay fast; test_app_config.py hashes with the
# profiles' cost.
CHEAP = ScryptParams(n=2, r=1, p=1)
PASSWORD = "correct horse battery staple"


def test_the_hash_is_not_the_password_and_does_not_hold_it() -> None:
    stored = hash_password(PASSWORD, CHEAP)
    assert stored != PASSWORD
    assert PASSWORD not in stored


def test_the_same_password_twice_gives_two_hashes() -> None:
    assert hash_password(PASSWORD, CHEAP) != hash_password(PASSWORD, CHEAP)


def test_only_the_right_password_verifies() -> None:
    stored = hash_password(PASSWORD, CHEAP)
    assert verify_password(PASSWORD, stored)
    assert not verify_password(PASSWORD + "!", stored)
    assert not verify_password("", stored)


def test_a_hash_of_another_kind_is_refused() -> None:
    _, *rest = hash_password(PASSWORD, CHEAP).split("$")
    with pytest.raises(ValueError, match="not a scrypt hash"):
        verify_password(PASSWORD, "$".join(["bcrypt", *rest]))


def test_a_hash_verifies_after_the_cost_changes() -> None:
    """The cost is read from the stored hash, so a new app.scrypt does not lock out old users."""
    old = hash_password(PASSWORD, CHEAP)
    new = hash_password(PASSWORD, ScryptParams(n=4, r=2, p=1))
    assert verify_password(PASSWORD, old)
    assert verify_password(PASSWORD, new)
