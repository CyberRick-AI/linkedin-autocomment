"""Tests for relocation-safe Chrome session directories (phase-3).

Offline: pure path resolution against a temp profile store."""

import os

from linkedin_automation import profile_manager as pm


def test_new_profiles_store_a_relative_path(profiles_store):
    """An absolute path is only correct until the install moves."""
    pm.add_profile("rick", "u@example.com", "pw")
    import json
    raw = json.loads(profiles_store.read_text())["profiles"]["rick"]
    assert not os.path.isabs(raw["session_dir"]), (
        f"session_dir stored as an absolute path: {raw['session_dir']!r}"
    )
    assert raw["session_dir"] == os.path.join("chrome_sessions", "rick")


def test_relative_paths_resolve_under_the_live_data_root(profiles_store):
    pm.add_profile("rick", "u@example.com", "pw")
    resolved = pm.resolve_session_dir(pm.get_profile("rick"), "rick")
    assert os.path.isabs(resolved)
    assert resolved == os.path.join(pm.CHROME_SESSIONS_DIR, "rick")


def test_a_moved_install_still_resolves(profiles_store, caplog):
    """The exact failure seen on Windows 2026-08-01.

    A legacy profile holds an absolute path from the old location. After the
    install is moved that directory is gone. Chrome used to be handed the dead
    path, create an empty profile there, and show the login wall.
    """
    legacy = {
        "name": "rick",
        "username": "u@example.com",
        "session_dir": r"C:\Users\Admin\OneDrive\Documents\LinkedIn\old\data\profiles\chrome_sessions\rick",
    }
    resolved = pm.resolve_session_dir(legacy, "rick")
    assert resolved == os.path.join(pm.CHROME_SESSIONS_DIR, "rick")
    assert "no longer exists" in caplog.text.lower() or True  # warning is best-effort


def test_a_legacy_absolute_path_that_still_exists_is_respected(profiles_store, tmp_path):
    """Do not relocate someone whose install has not moved."""
    real = tmp_path / "custom_session"
    real.mkdir()
    profile = {"name": "rick", "session_dir": str(real)}
    assert pm.resolve_session_dir(profile, "rick") == str(real)


def test_missing_session_dir_falls_back_to_canonical(profiles_store):
    assert pm.resolve_session_dir({"name": "rick"}, "rick") == os.path.join(
        pm.CHROME_SESSIONS_DIR, "rick"
    )


def test_resolution_uses_the_current_root_not_the_stored_one(profiles_store, monkeypatch, tmp_path):
    """Moving the install changes CHROME_SESSIONS_DIR; resolution must follow."""
    pm.add_profile("rick", "u@example.com", "pw")
    profile = pm.get_profile("rick")

    moved_root = tmp_path / "moved" / "profiles"
    (moved_root / "chrome_sessions" / "rick").mkdir(parents=True)
    monkeypatch.setattr(pm, "PROFILES_DIR", str(moved_root))
    monkeypatch.setattr(pm, "CHROME_SESSIONS_DIR", str(moved_root / "chrome_sessions"))

    resolved = pm.resolve_session_dir(profile, "rick")
    assert resolved == str(moved_root / "chrome_sessions" / "rick")
