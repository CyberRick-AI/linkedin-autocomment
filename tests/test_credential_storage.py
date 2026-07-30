"""Tests for OS-credential-store password handling (keyring-credentials).

Offline and hermetic: the ``fake_keyring`` autouse fixture in conftest replaces
the real Keychain / Credential Manager with an in-memory dict, so nothing here
touches the developer's actual credential store."""

import json

import pytest

from linkedin_automation import profile_manager as pm


@pytest.fixture
def isolated_store(profiles_store):
    """Profile storage redirected to tmp (alias for readability)."""
    return profiles_store


def _raw(profiles_file):
    """Read profiles.json exactly as it sits on disk."""
    return json.loads(profiles_file.read_text())


# ─── the password leaves the file ────────────────────────────────────────────

def test_password_is_not_written_to_disk(isolated_store, fake_keyring):
    pm.add_profile("work", "user@example.com", "s3cret!")

    stored = _raw(isolated_store)["profiles"]["work"]
    assert stored["password"] is None
    assert stored["password_location"] == pm.LOCATION_KEYRING
    # And the secret is nowhere in the file's text at all.
    assert "s3cret!" not in isolated_store.read_text()


def test_password_lands_in_the_credential_store(isolated_store, fake_keyring):
    pm.add_profile("work", "user@example.com", "s3cret!")
    assert fake_keyring.store[(pm.KEYRING_SERVICE, "work")] == "s3cret!"


def test_roundtrip_through_accessor(isolated_store):
    pm.add_profile("work", "user@example.com", "s3cret!")
    assert pm.get_profile_password(pm.get_profile("work")) == "s3cret!"


def test_removing_a_profile_clears_the_credential(isolated_store, fake_keyring):
    pm.add_profile("work", "user@example.com", "s3cret!")
    pm.remove_profile("work")
    assert (pm.KEYRING_SERVICE, "work") not in fake_keyring.store


def test_remove_survives_a_missing_credential(isolated_store, fake_keyring):
    """delete_password raising must not stop the profile being removed."""
    pm.add_profile("work", "user@example.com", "s3cret!")
    fake_keyring.store.clear()  # credential vanished behind our back
    pm.remove_profile("work")
    assert pm.get_profile("work") is None


# ─── legacy profiles keep working ────────────────────────────────────────────

def test_legacy_plaintext_profile_still_readable(isolated_store):
    """A profiles.json written before this change has no location marker."""
    isolated_store.write_text(json.dumps({
        "profiles": {
            "old": {"username": "u@example.com", "password": "legacy-pw"}
        },
        "default": "old",
    }))
    assert pm.get_profile_password(pm.get_profile("old")) == "legacy-pw"


def test_migration_moves_plaintext_into_the_store(isolated_store, fake_keyring):
    isolated_store.write_text(json.dumps({
        "profiles": {
            "old": {"username": "u@example.com", "password": "legacy-pw"},
            "other": {"username": "o@example.com", "password": "other-pw"},
        },
        "default": "old",
    }))

    moved, left = pm.migrate_passwords_to_keyring()

    assert (moved, left) == (2, 0)
    assert fake_keyring.store[(pm.KEYRING_SERVICE, "old")] == "legacy-pw"
    on_disk = _raw(isolated_store)["profiles"]
    assert on_disk["old"]["password"] is None
    assert on_disk["old"]["password_location"] == pm.LOCATION_KEYRING
    assert "legacy-pw" not in isolated_store.read_text()


def test_migration_is_idempotent(isolated_store, fake_keyring):
    pm.add_profile("work", "u@example.com", "pw")
    assert pm.migrate_passwords_to_keyring() == (0, 0)
    assert pm.get_profile_password(pm.get_profile("work")) == "pw"


def test_migration_leaves_password_readable(isolated_store):
    """The point of migrating: login still finds the password afterwards."""
    isolated_store.write_text(json.dumps({
        "profiles": {"old": {"username": "u@example.com", "password": "legacy-pw"}},
        "default": "old",
    }))
    pm.migrate_passwords_to_keyring()
    assert pm.get_profile_password(pm.get_profile("old")) == "legacy-pw"


# ─── machines with no credential store ───────────────────────────────────────

def test_falls_back_to_file_without_a_backend(isolated_store, no_keyring):
    pm.add_profile("work", "user@example.com", "s3cret!")

    stored = _raw(isolated_store)["profiles"]["work"]
    assert stored["password"] == "s3cret!"
    assert stored["password_location"] == pm.LOCATION_FILE
    assert pm.get_profile_password(pm.get_profile("work")) == "s3cret!"


def test_keyring_available_false_without_backend(no_keyring):
    assert pm.keyring_available() is False


def test_migration_is_a_noop_without_a_backend(isolated_store, no_keyring):
    isolated_store.write_text(json.dumps({
        "profiles": {"old": {"username": "u@example.com", "password": "legacy-pw"}},
        "default": "old",
    }))
    moved, _left = pm.migrate_passwords_to_keyring()
    assert moved == 0
    # Untouched, and still usable.
    assert pm.get_profile_password(pm.get_profile("old")) == "legacy-pw"


def test_write_failure_falls_back_to_file(isolated_store, fake_keyring):
    """A credential store that errors on write must not lose the password."""
    def boom(service, name, password):
        raise RuntimeError("keychain locked")

    fake_keyring.set_password = boom
    pm.add_profile("work", "user@example.com", "s3cret!")

    stored = _raw(isolated_store)["profiles"]["work"]
    assert stored["password_location"] == pm.LOCATION_FILE
    assert pm.get_profile_password(pm.get_profile("work")) == "s3cret!"


def test_read_failure_returns_empty_not_crash(isolated_store, fake_keyring):
    pm.add_profile("work", "user@example.com", "s3cret!")

    def boom(service, name):
        raise RuntimeError("keychain locked")

    fake_keyring.get_password = boom
    assert pm.get_profile_password(pm.get_profile("work")) == ""


# ─── special characters survive the round trip ───────────────────────────────

@pytest.mark.parametrize("secret", [
    "p@ss+w0rd&more!",
    "  leading-and-trailing  ",
    'semicolon;colon:bracket]quote"',
    "emoji-🔥-unicode-é",
])
def test_special_characters_survive_the_credential_store(isolated_store, secret):
    pm.add_profile("p", "user@example.com", secret)
    assert pm.get_profile_password(pm.get_profile("p")) == secret
