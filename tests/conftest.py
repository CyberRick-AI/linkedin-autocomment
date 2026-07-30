"""Shared pytest fixtures for the LinkedIn automation test suite (Phase 5).

All fixtures keep tests hermetic: storage is redirected into a temp dir, the
browser/network boundary is never crossed, and no OpenAI/LinkedIn calls happen.
"""

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import dashboard as linkedin_dashboard


# Canonical activity-URL template used across tests.
ACTIVITY_URL = "https://www.linkedin.com/feed/update/urn:li:activity:{}/"


@pytest.fixture
def make_comment():
    """Factory for generator-style comment dicts (post_* keys)."""
    def _make(i, **overrides):
        comment = {
            "post_url": ACTIVITY_URL.format(i),
            "post_author": f"Author {i}",
            "post_text": f"Interesting post body number {i}.",
            "post_category": "AI",
            "comment": f"Great point {i}, thanks for sharing.",
            "word_count": 5,
            "style": "thoughtful",
        }
        comment.update(overrides)
        return comment
    return _make


class FakeKeyring:
    """In-memory stand-in for the OS credential store.

    Mirrors the three keyring functions the profile manager uses. ``priority``
    is what ``keyring_available()`` checks, so a positive value makes this look
    like a real backend.
    """

    priority = 5

    def __init__(self):
        self.store = {}

    def get_keyring(self):
        return self

    def set_password(self, service, name, password):
        self.store[(service, name)] = password

    def get_password(self, service, name):
        return self.store.get((service, name))

    def delete_password(self, service, name):
        if (service, name) not in self.store:
            raise KeyError(f"no password for {name}")
        del self.store[(service, name)]


@pytest.fixture(autouse=True)
def fake_keyring(monkeypatch):
    """Keep every test off the real Keychain / Credential Manager.

    Autouse and suite-wide: adding a profile writes a password, and without
    this the suite would leave entries in the developer's actual OS credential
    store. Tests that care about the contents can request this fixture and read
    ``.store`` directly.
    """
    fake = FakeKeyring()
    monkeypatch.setattr(pm, "keyring", fake)
    return fake


@pytest.fixture
def no_keyring(monkeypatch):
    """Simulate a machine with no credential store (headless Linux, etc.)."""
    monkeypatch.setattr(pm, "keyring", None)


@pytest.fixture
def profiles_store(tmp_path, monkeypatch):
    """Redirect the profile manager's storage globals into a temp dir."""
    profiles_dir = tmp_path / "profiles"
    sessions_dir = profiles_dir / "chrome_sessions"
    sessions_dir.mkdir(parents=True)
    profiles_file = profiles_dir / "profiles.json"
    monkeypatch.setattr(pm, "PROFILES_DIR", str(profiles_dir))
    monkeypatch.setattr(pm, "PROFILES_FILE", str(profiles_file))
    monkeypatch.setattr(pm, "CHROME_SESSIONS_DIR", str(sessions_dir))
    return profiles_file


@pytest.fixture
def comments_dir(tmp_path, monkeypatch):
    """Redirect the profile-specific comments/progress/screenshots dirs to tmp."""
    cdir = tmp_path / "quality_comments"
    shots = cdir / "debug_screenshots"
    shots.mkdir(parents=True)
    monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None: str(cdir))
    monkeypatch.setattr(pm, "get_screenshots_dir", lambda profile_name=None: str(shots))
    monkeypatch.setattr(
        pm, "get_progress_file",
        lambda profile_name=None: str(cdir / "posting_progress.json"),
    )
    # Redirect the profile data root too (used by get_config_path) so config
    # endpoints write to tmp, never the real data/<profile>/profile_config.json.
    monkeypatch.setattr(pm, "get_data_dir", lambda profile_name=None, subdir=None: str(tmp_path))
    return cdir


@pytest.fixture
def api_client(comments_dir, profiles_store):
    """Flask test client with storage redirected to tmp (no browser/network)."""
    linkedin_dashboard.app.config.update(TESTING=True)
    return linkedin_dashboard.app.test_client()
