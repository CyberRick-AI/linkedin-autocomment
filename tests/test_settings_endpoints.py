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

def test_providers_endpoint_lists_the_whole_registry(api_client):
    body = api_client.get("/api/settings/providers").get_json()
    names = [p["name"] for p in body["providers"]]
    assert names == list(providers.SPECS)
    assert body["default_provider"] == "openai"
    # The open-list options, which are what make this not a fixed menu.
    assert "custom" in names
    assert "ollama" in names


def test_custom_provider_is_advertised_as_needing_a_base_url(api_client):
    body = api_client.get("/api/settings/providers").get_json()
    custom = next(p for p in body["providers"] if p["name"] == "custom")
    assert custom["requires_base_url"] is True
    assert custom["base_url"] == ""


def test_local_providers_are_not_asked_for_a_key(api_client):
    body = api_client.get("/api/settings/providers").get_json()
    ollama = next(p for p in body["providers"] if p["name"] == "ollama")
    assert ollama["local"] is True
    assert ollama["key"]["set"] is True
    assert ollama["key"]["source"] == "local"


def test_unverified_default_models_are_flagged_to_the_ui(api_client):
    body = api_client.get("/api/settings/providers").get_json()
    by_name = {p["name"]: p for p in body["providers"]}
    assert by_name["openai"]["default_model_verified"] is True
    assert by_name["xai"]["default_model_verified"] is False


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


# ─── Custom / open provider list (Phase 8b) ───────────────────────────────────

def test_custom_provider_round_trips_with_a_base_url(api_client):
    """The whole point of 8b: a provider nobody coded for, configured in the UI."""
    name = _make_profile(api_client)
    resp = api_client.post(f"/api/profiles/{name}/provider", json={
        "provider": "custom",
        "model": "hermes-4-405b",
        "base_url": "https://inference.example.invalid/v1",
    })
    assert resp.status_code == 200

    body = api_client.get(f"/api/profiles/{name}/provider").get_json()
    assert body["provider"] == "custom"
    assert body["model"] == "hermes-4-405b"
    assert body["base_url"] == "https://inference.example.invalid/v1"


def test_custom_without_a_base_url_is_rejected_before_it_is_saved(api_client):
    """A config that cannot resolve must never be persisted."""
    name = _make_profile(api_client)
    resp = api_client.post(f"/api/profiles/{name}/provider",
                           json={"provider": "custom", "model": "m"})
    assert resp.status_code == 400
    assert "base_url" in resp.get_json()["error"]

    # And the profile is untouched, still on the default.
    assert api_client.get(f"/api/profiles/{name}/provider").get_json()["provider"] == "openai"


def test_provider_without_a_default_model_is_rejected_without_one(api_client):
    name = _make_profile(api_client)
    resp = api_client.post(f"/api/profiles/{name}/provider", json={"provider": "groq"})
    assert resp.status_code == 400
    assert "model" in resp.get_json()["error"].lower()


def test_non_string_base_url_is_a_400(api_client):
    name = _make_profile(api_client)
    resp = api_client.post(f"/api/profiles/{name}/provider",
                           json={"provider": "custom", "model": "m", "base_url": 42})
    assert resp.status_code == 400


# ─── Test Connection ──────────────────────────────────────────────────────────

def test_test_connection_reports_the_probe_result(api_client, monkeypatch):
    monkeypatch.setattr(providers, "probe", lambda provider, model=None, base_url=None,
                        api_key=None: {"ok": True, "provider": provider, "model": model,
                                       "calls": 1, "temperature_accepted": True,
                                       "reasoning_stripped": False,
                                       "text": "connection ok", "error": None})
    resp = api_client.post("/api/settings/test-connection",
                           json={"provider": "openai", "model": "gpt-4o-mini"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert resp.get_json()["temperature_accepted"] is True


def test_test_connection_passes_the_custom_base_url_through(api_client, monkeypatch):
    seen = {}
    monkeypatch.setattr(providers, "probe",
                        lambda provider, model=None, base_url=None, api_key=None:
                        seen.update(provider=provider, model=model, base_url=base_url)
                        or {"ok": True, "calls": 1, "error": None})
    api_client.post("/api/settings/test-connection", json={
        "provider": "custom", "model": "m", "base_url": "https://x.invalid/v1"})
    assert seen == {"provider": "custom", "model": "m", "base_url": "https://x.invalid/v1"}


def test_a_failed_probe_is_a_200_carrying_the_reason(api_client, monkeypatch):
    """The report is the result; the UI has to render why it failed."""
    monkeypatch.setattr(providers, "probe", lambda provider, **kw: {
        "ok": False, "calls": 2, "error": "401 unauthorized"})
    resp = api_client.post("/api/settings/test-connection", json={"provider": "openai"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is False
    assert "401" in resp.get_json()["error"]


@pytest.mark.parametrize("body", [
    {"provider": "nonsense"},
    {},
    {"provider": "openai", "model": 42},
    {"provider": "openai", "base_url": []},
])
def test_malformed_test_connection_requests_are_4xx(api_client, body):
    resp = api_client.post("/api/settings/test-connection", json=body)
    assert resp.status_code == 400


def test_test_connection_never_runs_by_itself(api_client, monkeypatch):
    """It spends money, so nothing but an explicit POST may trigger it."""
    called = {"n": 0}
    monkeypatch.setattr(providers, "probe",
                        lambda provider, **kw:
                        called.update(n=called["n"] + 1) or {"ok": True})

    api_client.get("/api/settings/providers")
    name = _make_profile(api_client)
    api_client.get(f"/api/profiles/{name}/provider")
    api_client.post(f"/api/profiles/{name}/provider", json={"provider": "openai"})
    api_client.post("/api/settings/api-key", json={"provider": "openai", "api_key": SECRET})

    assert called["n"] == 0


def test_key_status_falls_back_to_the_environment(api_client, monkeypatch):
    """.env stays supported, and the UI shows where the key came from."""
    monkeypatch.setenv("XAI_API_KEY", "sk-env-key-4321")
    resp = api_client.get("/api/settings/providers")
    xai = next(p for p in resp.get_json()["providers"] if p["name"] == "xai")
    assert xai["key"] == {"set": True, "source": "env", "last4": "4321"}
    assert "sk-env-key-4321" not in resp.get_data(as_text=True)
