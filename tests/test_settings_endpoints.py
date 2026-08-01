"""Dashboard Settings screen: provider selection and write-only API keys.

ROADMAP Phase 8. Offline: no browser, no network, no vendor SDK. The keyring is
already faked by the autouse fixture in conftest.py, so nothing here can reach
the real macOS Keychain.

The recurring assertion in this file is negative: no response body, at any
endpoint, ever contains a whole API key.
"""

import json

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import providers


SECRET = "sk-super-secret-key-value-6789"


@pytest.fixture(autouse=True)
def _no_env_migration(monkeypatch):
    monkeypatch.setattr(pm, "auto_migrate_from_env", lambda: None)


@pytest.fixture(autouse=True)
def _clean_provider_env(monkeypatch):
    """Unset provider keys so env fallback never leaks in from the shell."""
    for env_name in providers.API_KEY_ENV.values():
        monkeypatch.delenv(env_name, raising=False)


# ─── The provider catalogue ───────────────────────────────────────────────────

def test_providers_endpoint_lists_all_three(api_client):
    body = api_client.get("/api/settings/providers").get_json()
    names = [p["name"] for p in body["providers"]]
    assert names == ["openai", "anthropic", "xai"]
    assert body["default_provider"] == "openai"


def test_providers_endpoint_reports_key_status_without_the_key(api_client):
    api_client.post("/api/settings/api-key", json={"provider": "openai", "api_key": SECRET})
    resp = api_client.get("/api/settings/providers")
    raw = resp.get_data(as_text=True)

    openai = next(p for p in resp.get_json()["providers"] if p["name"] == "openai")
    assert openai["key"]["set"] is True
    assert openai["key"]["last4"] == "6789"
    assert SECRET not in raw


# ─── Per-profile provider selection ───────────────────────────────────────────

def _make_profile(api_client, name="work"):
    api_client.post("/api/profiles", json={
        "name": name, "username": "w@x.com", "password": "p@ss",
    })
    return name


def test_new_profile_defaults_to_openai(api_client):
    name = _make_profile(api_client)
    body = api_client.get(f"/api/profiles/{name}/provider").get_json()
    assert body["provider"] == "openai"
    assert body["model"] == "gpt-4o-mini"


def test_set_provider_persists_and_round_trips(api_client):
    name = _make_profile(api_client)
    resp = api_client.post(f"/api/profiles/{name}/provider", json={"provider": "anthropic"})
    assert resp.status_code == 200
    assert resp.get_json()["model"] == providers.DEFAULT_MODELS["anthropic"]

    body = api_client.get(f"/api/profiles/{name}/provider").get_json()
    assert body["provider"] == "anthropic"
    assert body["model"] == providers.DEFAULT_MODELS["anthropic"]


def test_set_provider_accepts_an_explicit_model(api_client):
    name = _make_profile(api_client)
    api_client.post(f"/api/profiles/{name}/provider",
                    json={"provider": "xai", "model": "grok-custom"})
    body = api_client.get(f"/api/profiles/{name}/provider").get_json()
    assert (body["provider"], body["model"]) == ("xai", "grok-custom")


def test_switching_provider_does_not_touch_persona_tone_or_voice(api_client):
    """The gate: changing where text is generated must not change its voice."""
    name = _make_profile(api_client)
    api_client.post(f"/api/profiles/{name}/config", json={
        "comment_generator": {
            "persona": "AI consultant", "tone": "warm", "voice": "I/my only",
        },
    })
    before = api_client.get(f"/api/profiles/{name}/config").get_json()["comment_generator"]

    api_client.post(f"/api/profiles/{name}/provider", json={"provider": "anthropic"})

    after = api_client.get(f"/api/profiles/{name}/config").get_json()["comment_generator"]
    assert after == before
    assert after["persona"] == "AI consultant"
    assert after["tone"] == "warm"
    assert after["voice"] == "I/my only"


@pytest.mark.parametrize("body", [
    {"provider": "nonsense"},
    {"provider": ""},
    {},
])
def test_bad_provider_is_a_4xx_naming_the_valid_options(api_client, body):
    name = _make_profile(api_client)
    resp = api_client.post(f"/api/profiles/{name}/provider", json=body)
    assert resp.status_code == 400
    error = resp.get_json()["error"]
    for valid in providers.PROVIDERS:
        assert valid in error


def test_non_object_provider_body_is_a_400_not_a_500(api_client):
    name = _make_profile(api_client)
    resp = api_client.post(f"/api/profiles/{name}/provider",
                           json=["not", "an", "object"])
    assert resp.status_code == 400


def test_non_string_model_is_rejected(api_client):
    name = _make_profile(api_client)
    resp = api_client.post(f"/api/profiles/{name}/provider",
                           json={"provider": "openai", "model": 42})
    assert resp.status_code == 400


# ─── API keys are write-only ──────────────────────────────────────────────────

def test_setting_a_key_stores_it_and_echoes_only_the_last_four(api_client):
    resp = api_client.post("/api/settings/api-key",
                           json={"provider": "anthropic", "api_key": SECRET})
    assert resp.status_code == 200
    assert resp.get_json()["key"] == {"set": True, "source": "keyring", "last4": "6789"}
    assert SECRET not in resp.get_data(as_text=True)
    # It really is in the store, it just is not in any response.
    assert pm.get_api_key("anthropic") == SECRET


def test_no_endpoint_ever_returns_a_whole_api_key(api_client):
    """Swept across every Settings surface, not just the setter."""
    name = _make_profile(api_client)
    for provider in providers.PROVIDERS:
        api_client.post("/api/settings/api-key",
                        json={"provider": provider, "api_key": SECRET})

    surfaces = [
        api_client.get("/api/settings/providers"),
        api_client.get(f"/api/profiles/{name}/provider"),
        api_client.get(f"/api/profiles/{name}/config"),
        api_client.get("/api/profiles"),
        api_client.post(f"/api/profiles/{name}/provider", json={"provider": "openai"}),
        api_client.delete("/api/settings/api-key/xai"),
    ]
    for resp in surfaces:
        assert SECRET not in resp.get_data(as_text=True)


def test_api_key_is_not_written_into_the_profile_config(api_client):
    """It belongs in the credential store, never in profiles.json or config."""
    name = _make_profile(api_client)
    api_client.post("/api/settings/api-key", json={"provider": "openai", "api_key": SECRET})
    config = api_client.get(f"/api/profiles/{name}/config").get_json()
    assert SECRET not in json.dumps(config)


@pytest.mark.parametrize("body", [
    {"provider": "openai"},
    {"provider": "openai", "api_key": ""},
    {"provider": "openai", "api_key": "   "},
    {"provider": "openai", "api_key": 12345},
    {"api_key": "sk-x"},
    {"provider": "nonsense", "api_key": "sk-x"},
])
def test_malformed_api_key_requests_are_4xx_not_500(api_client, body):
    resp = api_client.post("/api/settings/api-key", json=body)
    assert 400 <= resp.status_code < 500
    assert "error" in resp.get_json()


def test_deleting_a_key_clears_it(api_client):
    api_client.post("/api/settings/api-key", json={"provider": "openai", "api_key": SECRET})
    resp = api_client.delete("/api/settings/api-key/openai")
    assert resp.status_code == 200
    assert resp.get_json()["key"]["set"] is False
    assert pm.get_api_key("openai") == ""


def test_deleting_an_unknown_provider_is_a_400(api_client):
    assert api_client.delete("/api/settings/api-key/nonsense").status_code == 400


def test_key_status_falls_back_to_the_environment(api_client, monkeypatch):
    """.env stays supported, and the UI shows where the key came from."""
    monkeypatch.setenv("XAI_API_KEY", "sk-env-key-4321")
    resp = api_client.get("/api/settings/providers")
    xai = next(p for p in resp.get_json()["providers"] if p["name"] == "xai")
    assert xai["key"] == {"set": True, "source": "env", "last4": "4321"}
    assert "sk-env-key-4321" not in resp.get_data(as_text=True)
