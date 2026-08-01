"""Provider abstraction: adapters, config resolution, key handling.

ROADMAP Phase 8. Offline: every vendor SDK client is replaced with a recorded
fixture, so the same prompt goes through all three adapters with zero live
calls. That is the phase's headline gate — the three adapters must normalise to
one shape, asserted field by field.
"""

import json

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import providers


# ─── Recorded fixtures: one canned response per vendor wire format ────────────
#
# These are the *shapes* the three SDKs return, hand-written rather than
# captured from a live call (PROJECT.md caps paid calls at zero). Each carries
# the same text, which is what lets the normalisation test compare field by
# field.

RECORDED_TEXT = "Recorded completion text."


class _RecordedOpenAIClient:
    """Mimics openai.OpenAI: .chat.completions.create -> .choices[0].message.content"""

    def __init__(self, text=RECORDED_TEXT):
        self.calls = []
        message = type("M", (), {"content": text})
        choice = type("C", (), {"message": message})
        response = type("R", (), {"choices": [choice]})

        def create(**kwargs):
            self.calls.append(kwargs)
            return response

        self.chat = type("Chat", (), {"completions": type("Comp", (), {"create": staticmethod(create)})})()


class _RecordedAnthropicClient:
    """Mimics anthropic.Anthropic: .messages.create -> .content[<text blocks>]"""

    def __init__(self, text=RECORDED_TEXT):
        self.calls = []
        block = type("B", (), {"type": "text", "text": text})
        response = type("R", (), {"content": [block]})

        def create(**kwargs):
            self.calls.append(kwargs)
            return response

        self.messages = type("Msgs", (), {"create": staticmethod(create)})()


@pytest.fixture
def recorded(monkeypatch):
    """Wire all three adapters to recorded clients and a dummy key."""
    clients = {}

    def _openai_client(self):
        clients.setdefault(self.name, _RecordedOpenAIClient())
        return clients[self.name]

    def _anthropic_client(self):
        clients.setdefault(self.name, _RecordedAnthropicClient())
        return clients[self.name]

    monkeypatch.setattr(providers.OpenAIProvider, "_client", _openai_client)
    monkeypatch.setattr(providers.AnthropicProvider, "_client", _anthropic_client)
    monkeypatch.setattr(providers, "resolve_api_key", lambda provider, explicit=None: "sk-dummy")
    return clients


# ─── The gate: one prompt, three adapters, one normalised shape ───────────────

def test_all_three_adapters_return_the_identical_normalised_shape(recorded):
    """Asserted field by field against recorded fixtures. Zero live calls."""
    results = {}
    for name in providers.PROVIDERS:
        adapter = providers.get_provider(name)
        results[name] = adapter.complete(
            model=providers.DEFAULT_MODELS[name],
            system="You are a test.",
            user="Say something.",
        )

    for name, completion in results.items():
        assert isinstance(completion, providers.Completion)
        assert completion.text == RECORDED_TEXT
        assert completion.provider == name
        assert completion.model == providers.DEFAULT_MODELS[name]

    # Same field set across all three, not merely same values.
    shapes = {tuple(sorted(vars(c))) for c in results.values()}
    assert len(shapes) == 1
    assert shapes.pop() == ("model", "provider", "text")


def test_every_provider_has_a_default_model_and_key_env(recorded):
    for name in providers.PROVIDERS:
        assert providers.DEFAULT_MODELS[name]
        assert providers.API_KEY_ENV[name]


def test_default_is_still_openai_gpt_4o_mini():
    """Existing users must see no change."""
    assert providers.DEFAULT_PROVIDER == "openai"
    assert providers.DEFAULT_MODELS["openai"] == "gpt-4o-mini"


def test_anthropic_default_is_not_opus():
    """PROJECT.md: this workload never needs Opus-depth reasoning."""
    model = providers.DEFAULT_MODELS["anthropic"]
    assert "opus" not in model
    assert "haiku" in model or "sonnet" in model


# ─── Adapter wiring details ───────────────────────────────────────────────────

def test_xai_reuses_the_openai_wire_format_at_its_own_host(recorded):
    adapter = providers.get_provider("xai")
    assert isinstance(adapter, providers.OpenAIProvider)
    assert adapter.base_url == providers.XAI_BASE_URL
    assert providers.get_provider("openai").base_url is None


def test_openai_adapter_sends_system_as_a_message(recorded):
    providers.get_provider("openai").complete(
        model="gpt-4o-mini", system="SYS", user="USR", temperature=0.5, max_tokens=99)
    sent = recorded["openai"].calls[0]
    assert sent["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USR"},
    ]
    assert sent["temperature"] == 0.5
    assert sent["max_tokens"] == 99
    assert "response_format" not in sent


def test_openai_adapter_requests_json_when_asked(recorded):
    providers.get_provider("openai").complete(
        model="gpt-4o-mini", system="SYS", user="USR", json_object=True)
    assert recorded["openai"].calls[0]["response_format"] == {"type": "json_object"}


def test_anthropic_adapter_sends_system_top_level_not_as_a_message(recorded):
    """The shape difference that makes this an adapter, not a base-URL swap."""
    providers.get_provider("anthropic").complete(
        model="claude-haiku-4-5", system="SYS", user="USR")
    sent = recorded["anthropic"].calls[0]
    assert sent["system"] == "SYS"
    assert sent["messages"] == [{"role": "user", "content": "USR"}]


def test_anthropic_adapter_always_sends_max_tokens(recorded):
    """Anthropic requires it; OpenAI lets you omit it."""
    providers.get_provider("anthropic").complete(
        model="claude-haiku-4-5", system="SYS", user="USR")
    assert recorded["anthropic"].calls[0]["max_tokens"] == \
        providers.AnthropicProvider.DEFAULT_MAX_TOKENS

    providers.get_provider("anthropic").complete(
        model="claude-haiku-4-5", system="SYS", user="USR", max_tokens=42)
    assert recorded["anthropic"].calls[1]["max_tokens"] == 42


def test_anthropic_adapter_instructs_json_in_the_system_prompt(recorded):
    """No response_format equivalent, so the instruction goes in the prompt."""
    providers.get_provider("anthropic").complete(
        model="claude-haiku-4-5", system="SYS", user="USR", json_object=True)
    sent = recorded["anthropic"].calls[0]
    assert sent["system"].startswith("SYS")
    assert "valid JSON object" in sent["system"]


# ─── The temperature trap ─────────────────────────────────────────────────────

@pytest.mark.parametrize("model", [
    "claude-sonnet-5", "claude-opus-5", "claude-opus-4-7", "claude-opus-4-8",
    "claude-fable-5", "some-future-claude-model",
])
def test_temperature_is_withheld_from_models_that_reject_it(recorded, model):
    """Anthropic's 5-series and Opus 4.7+ return 400 if sent temperature.

    Comment generation varies temperature deliberately for variety, so passing
    it through blindly would break every generation call on those models. An
    unrecognised model is treated as rejecting it: losing variety is recoverable,
    a 400 on every call is not.
    """
    assert providers.anthropic_accepts_temperature(model) is False
    providers.get_provider("anthropic").complete(
        model=model, system="SYS", user="USR", temperature=0.9)
    assert "temperature" not in recorded["anthropic"].calls[0]


@pytest.mark.parametrize("model", [
    "claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-6", "claude-3-haiku-20240307",
])
def test_temperature_is_sent_to_models_that_accept_it(recorded, model):
    assert providers.anthropic_accepts_temperature(model) is True
    providers.get_provider("anthropic").complete(
        model=model, system="SYS", user="USR", temperature=0.9)
    assert recorded["anthropic"].calls[0]["temperature"] == 0.9


def test_the_default_anthropic_model_accepts_temperature():
    """Otherwise switching to Anthropic would silently flatten comment variety."""
    assert providers.anthropic_accepts_temperature(
        providers.DEFAULT_MODELS["anthropic"]) is True


# ─── Config resolution ────────────────────────────────────────────────────────

def test_missing_provider_block_falls_back_to_the_default():
    """A config written before Phase 8 keeps working."""
    assert providers.resolve_provider_config({}) == ("openai", "gpt-4o-mini")
    assert providers.resolve_provider_config(None) == ("openai", "gpt-4o-mini")


def test_provider_without_model_uses_that_provider_default():
    assert providers.resolve_provider_config({"provider": {"name": "anthropic"}}) == \
        ("anthropic", providers.DEFAULT_MODELS["anthropic"])


def test_provider_name_is_case_and_whitespace_tolerant():
    assert providers.resolve_provider_config(
        {"provider": {"name": "  Anthropic "}})[0] == "anthropic"


def test_explicit_model_overrides_the_provider_default():
    assert providers.resolve_provider_config(
        {"provider": {"name": "xai", "model": "grok-custom"}}) == ("xai", "grok-custom")


def test_unknown_provider_raises_naming_the_valid_options():
    """The gate: fails at startup with an actionable message, not mid-run."""
    with pytest.raises(providers.ProviderError) as exc:
        providers.resolve_provider_config({"provider": {"name": "nonsense"}})
    message = str(exc.value)
    assert "nonsense" in message
    for name in providers.PROVIDERS:
        assert name in message


def test_get_provider_rejects_an_unknown_name(recorded):
    with pytest.raises(providers.ProviderError):
        providers.get_provider("nonsense")


# ─── Key resolution ───────────────────────────────────────────────────────────

def test_key_comes_from_the_credential_store_first(monkeypatch):
    monkeypatch.setattr(pm, "get_api_key", lambda provider: "sk-from-keyring")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    assert providers.resolve_api_key("openai") == "sk-from-keyring"


def test_key_falls_back_to_the_environment(monkeypatch):
    """.env stays supported for installs with no credential store."""
    monkeypatch.setattr(pm, "get_api_key", lambda provider: "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    assert providers.resolve_api_key("anthropic") == "sk-from-env"


def test_explicit_key_wins_over_both(monkeypatch):
    monkeypatch.setattr(pm, "get_api_key", lambda provider: "sk-from-keyring")
    assert providers.resolve_api_key("openai", explicit="sk-explicit") == "sk-explicit"


def test_missing_key_raises_one_actionable_error_naming_the_variable(monkeypatch):
    """Not a stack trace from inside a vendor SDK — the Phase 8 gate."""
    monkeypatch.setattr(pm, "get_api_key", lambda provider: "")
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    with pytest.raises(providers.ProviderError) as exc:
        providers.resolve_api_key("xai")
    message = str(exc.value)
    assert "xai" in message
    assert "XAI_API_KEY" in message


# ─── Key storage never leaks the value ────────────────────────────────────────

def test_api_key_status_reports_only_the_last_four(monkeypatch):
    monkeypatch.setattr(pm, "get_api_key", lambda provider: "sk-secret-value-ABCD")
    status = pm.api_key_status("openai")
    assert status["set"] is True
    assert status["last4"] == "ABCD"
    assert "sk-secret-value-ABCD" not in json.dumps(status)


def test_api_key_status_when_nothing_is_set(monkeypatch):
    monkeypatch.setattr(pm, "get_api_key", lambda provider: "")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert pm.api_key_status("openai") == {"set": False, "source": None, "last4": ""}


def test_api_key_status_reports_the_env_fallback_as_its_source(monkeypatch):
    monkeypatch.setattr(pm, "get_api_key", lambda provider: "")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-key-WXYZ")
    status = pm.api_key_status("openai")
    assert status == {"set": True, "source": "env", "last4": "WXYZ"}


def test_api_keys_use_a_service_separate_from_linkedin_passwords():
    """A profile named 'openai' must not collide with the OpenAI API key."""
    assert pm.API_KEY_SERVICE != pm.KEYRING_SERVICE


def test_set_api_key_rejects_an_empty_value():
    with pytest.raises(ValueError):
        pm.set_api_key("openai", "   ")


def test_set_api_key_refuses_rather_than_writing_to_a_file(monkeypatch):
    """An API key must never land in profiles.json, so there is no file fallback."""
    monkeypatch.setattr(pm, "keyring_available", lambda: False)
    with pytest.raises(RuntimeError) as exc:
        pm.set_api_key("openai", "sk-real-key")
    assert "environment variable" in str(exc.value)
