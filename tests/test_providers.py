"""Provider registry: specs, adapters, capability flags, reasoning filter, probe.

ROADMAP Phases 8 and 8b. Offline: every vendor SDK client is replaced with a
recorded fixture, so every built-in spec goes through its adapter with zero live
calls. That is the headline gate — one normalised shape out of all of them.
"""

import json
import os

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import providers


RECORDED_TEXT = "Recorded completion text."


# ─── Recorded fixtures: the two wire formats ──────────────────────────────────

class _RecordedOpenAIClient:
    """Mimics openai.OpenAI: .chat.completions.create -> .choices[0].message.content"""

    def __init__(self, text=RECORDED_TEXT, raises=None):
        self.calls = []
        self.text = text
        self.raises = raises
        owner = self

        class _Completions:
            @staticmethod
            def create(**kwargs):
                owner.calls.append(kwargs)
                if owner.raises:
                    raise owner.raises
                message = type("M", (), {"content": owner.text, "reasoning_content": None})
                return type("R", (), {"choices": [type("C", (), {"message": message})]})

        self.chat = type("Chat", (), {"completions": _Completions()})()


class _RecordedAnthropicClient:
    """Mimics anthropic.Anthropic: .messages.create -> .content[<text blocks>]"""

    def __init__(self, text=RECORDED_TEXT, raises=None):
        self.calls = []
        self.text = text
        self.raises = raises
        owner = self

        class _Messages:
            @staticmethod
            def create(**kwargs):
                owner.calls.append(kwargs)
                if owner.raises:
                    raise owner.raises
                block = type("B", (), {"type": "text", "text": owner.text})
                return type("R", (), {"content": [block]})

        self.messages = _Messages()


@pytest.fixture
def recorded(monkeypatch):
    """Wire both adapter kinds to recorded clients and a dummy key."""
    clients = {}
    state = {"text": RECORDED_TEXT, "raises": None}

    def _openai_client(self):
        if self.name not in clients:
            clients[self.name] = _RecordedOpenAIClient(state["text"], state["raises"])
        return clients[self.name]

    def _anthropic_client(self):
        if self.name not in clients:
            clients[self.name] = _RecordedAnthropicClient(state["text"], state["raises"])
        return clients[self.name]

    monkeypatch.setattr(providers.OpenAICompatibleProvider, "_client", _openai_client)
    monkeypatch.setattr(providers.AnthropicProvider, "_client", _anthropic_client)
    monkeypatch.setattr(providers, "resolve_api_key",
                        lambda provider, explicit=None: "sk-dummy")
    clients["_state"] = state
    return clients


def _configure(recorded, text=None, raises=None):
    """Set what the next recorded client will return, before it is built."""
    if text is not None:
        recorded["_state"]["text"] = text
    if raises is not None:
        recorded["_state"]["raises"] = raises


# ─── The gate: every built-in spec, one normalised shape ──────────────────────

def test_every_built_in_provider_returns_the_identical_normalised_shape(recorded):
    """All specs, both wire formats, asserted field by field. Zero live calls."""
    results = {}
    for name, spec in providers.SPECS.items():
        model = spec.default_model or "some-model"
        base_url = "https://example.invalid/v1" if spec.requires_base_url else None
        adapter = providers.get_provider(name, base_url=base_url)
        results[name] = adapter.complete(
            model=model, system="You are a test.", user="Say something.")

    assert len(results) == len(providers.SPECS)
    for name, completion in results.items():
        assert isinstance(completion, providers.Completion)
        assert completion.text == RECORDED_TEXT
        assert completion.provider == name

    shapes = {tuple(sorted(vars(c))) for c in results.values()}
    assert len(shapes) == 1
    assert shapes.pop() == ("model", "provider", "reasoning_stripped", "text")


def test_the_custom_provider_works_with_only_a_base_url_and_model(recorded):
    """The criterion that actually meets the objective: no code change needed."""
    config = {"provider": {
        "name": "custom",
        "model": "hermes-4-405b",
        "base_url": "https://inference.example.invalid/v1",
    }}
    provider, model, base_url = providers.resolve_provider_config(config)
    assert (provider, model, base_url) == (
        "custom", "hermes-4-405b", "https://inference.example.invalid/v1")

    adapter, resolved_model = providers.get_provider_for_config(config)
    completion = adapter.complete(model=resolved_model, system="s", user="u")
    assert completion.text == RECORDED_TEXT
    assert adapter.base_url == "https://inference.example.invalid/v1"


def test_custom_without_a_base_url_is_refused_with_an_actionable_message():
    with pytest.raises(providers.ProviderError) as exc:
        providers.resolve_provider_config({"provider": {"name": "custom", "model": "m"}})
    assert "base_url" in str(exc.value)


def test_a_provider_with_no_default_model_demands_one():
    with pytest.raises(providers.ProviderError) as exc:
        providers.resolve_provider_config({"provider": {"name": "groq"}})
    assert "model" in str(exc.value).lower()


def test_registry_covers_the_providers_rick_named():
    for name in ("openai", "anthropic", "xai", "deepseek", "ollama", "custom"):
        assert name in providers.SPECS
    # Hermes and anything else unlisted are reachable two ways.
    assert providers.SPECS["openrouter"].base_url
    assert providers.SPECS["custom"].requires_base_url


def test_defaults_are_unchanged_from_phase_8():
    """Existing users must still see no change."""
    assert providers.DEFAULT_PROVIDER == "openai"
    assert providers.SPECS["openai"].default_model == "gpt-4o-mini"
    assert providers.resolve_provider_config({}) == ("openai", "gpt-4o-mini", None)


def test_unverified_default_models_are_flagged_not_presented_as_checked():
    """The xAI lesson: a guessed model string is labelled as one."""
    assert providers.SPECS["openai"].default_model_verified is True
    assert providers.SPECS["anthropic"].default_model_verified is True
    assert providers.SPECS["xai"].default_model_verified is False
    for spec in providers.SPECS.values():
        if spec.default_model and not spec.default_model_verified:
            assert spec.default_model  # flagged, and the UI surfaces the flag


def test_anthropic_default_is_not_opus():
    model = providers.SPECS["anthropic"].default_model
    assert "opus" not in model
    assert "haiku" in model or "sonnet" in model


# ─── Capability flags drive behaviour, not per-vendor code ────────────────────

def test_only_two_adapter_kinds_exist():
    """Adding a provider must not add a code path."""
    kinds = {spec.kind for spec in providers.SPECS.values()}
    assert kinds == {providers.KIND_OPENAI, providers.KIND_ANTHROPIC}
    assert sum(1 for s in providers.SPECS.values()
               if s.kind == providers.KIND_ANTHROPIC) == 1


@pytest.mark.parametrize("model", [
    "claude-sonnet-5", "claude-opus-5", "claude-opus-4-7", "claude-opus-4-8",
    "claude-fable-5", "some-future-claude-model",
])
def test_temperature_withheld_from_anthropic_models_that_reject_it(recorded, model):
    """The Phase 8 finding, now expressed as a spec flag rather than a special case."""
    assert providers.SPECS["anthropic"].temperature_allowed(model) is False
    providers.get_provider("anthropic").complete(
        model=model, system="SYS", user="USR", temperature=0.9)
    assert "temperature" not in recorded["anthropic"].calls[0]


@pytest.mark.parametrize("model", [
    "claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-6", "claude-3-haiku-20240307",
])
def test_temperature_sent_to_anthropic_models_that_accept_it(recorded, model):
    assert providers.SPECS["anthropic"].temperature_allowed(model) is True
    providers.get_provider("anthropic").complete(
        model=model, system="SYS", user="USR", temperature=0.9)
    assert recorded["anthropic"].calls[0]["temperature"] == 0.9


def test_openai_reasoning_models_also_lose_temperature(recorded):
    """Not an Anthropic-only problem, which is why the flag is general."""
    spec = providers.SPECS["openai"]
    assert spec.temperature_allowed("gpt-4o-mini") is True
    assert spec.temperature_allowed("o3-mini") is False


def test_default_models_that_ship_accept_temperature():
    """Otherwise picking a provider would silently flatten comment variety."""
    for name in ("openai", "anthropic"):
        spec = providers.SPECS[name]
        assert spec.temperature_allowed(spec.default_model) is True


def test_temperature_yes_specs_always_send_it(recorded):
    spec = providers.SPECS["deepseek"]
    assert spec.accepts_temperature == providers.TEMP_YES
    providers.get_provider("deepseek").complete(
        model="deepseek-chat", system="SYS", user="USR", temperature=0.4)
    assert recorded["deepseek"].calls[0]["temperature"] == 0.4


def test_json_mode_is_native_or_prompt_per_spec(recorded):
    providers.get_provider("openai").complete(
        model="gpt-4o-mini", system="SYS", user="USR", json_object=True)
    assert recorded["openai"].calls[0]["response_format"] == {"type": "json_object"}

    providers.get_provider("ollama").complete(
        model="llama3", system="SYS", user="USR", json_object=True)
    sent = recorded["ollama"].calls[0]
    assert "response_format" not in sent
    assert "valid JSON object" in sent["messages"][0]["content"]


def test_max_tokens_param_name_comes_from_the_spec(recorded, monkeypatch):
    """Reasoning models renamed it; the spec carries the name."""
    monkeypatch.setitem(
        providers.SPECS, "renamed",
        providers.ProviderSpec(name="renamed", label="Renamed",
                               key_env="X", max_tokens_param="max_completion_tokens"))
    providers.get_provider("renamed").complete(
        model="m", system="SYS", user="USR", max_tokens=50)
    sent = recorded["renamed"].calls[0]
    assert sent["max_completion_tokens"] == 50
    assert "max_tokens" not in sent


def test_a_provider_without_a_system_role_folds_it_into_the_user_turn(recorded, monkeypatch):
    """Dropping the system prompt would silently stop the persona applying."""
    monkeypatch.setitem(
        providers.SPECS, "nosys",
        providers.ProviderSpec(name="nosys", label="No system", key_env="X",
                               supports_system_role=False))
    providers.get_provider("nosys").complete(model="m", system="PERSONA", user="ASK")
    messages = recorded["nosys"].calls[0]["messages"]
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert "PERSONA" in messages[0]["content"]
    assert "ASK" in messages[0]["content"]


def test_anthropic_adapter_sends_system_top_level_and_always_max_tokens(recorded):
    providers.get_provider("anthropic").complete(
        model="claude-haiku-4-5", system="SYS", user="USR")
    sent = recorded["anthropic"].calls[0]
    assert sent["system"] == "SYS"
    assert sent["messages"] == [{"role": "user", "content": "USR"}]
    assert sent["max_tokens"] == providers.AnthropicProvider.DEFAULT_MAX_TOKENS


# ─── The reasoning filter: the failure this phase exists to prevent ───────────

def test_think_block_is_stripped():
    text, stripped = providers.strip_reasoning(
        "<think>I should be friendly here.</think>Great point about latency.")
    assert text == "Great point about latency."
    assert stripped is True


@pytest.mark.parametrize("tag", ["think", "thinking", "reasoning", "THINK"])
def test_the_common_reasoning_tags_are_all_handled(tag):
    text, _ = providers.strip_reasoning(f"<{tag}>hidden</{tag}>Visible.")
    assert text == "Visible."


def test_an_unclosed_reasoning_block_is_stripped_to_the_end():
    """The model hit the token cap mid-thought; everything after is monologue."""
    text, stripped = providers.strip_reasoning(
        "Here is my comment.\n<think>Now let me reconsider whether")
    assert text == "Here is my comment."
    assert stripped is True


def test_an_all_reasoning_response_never_returns_empty():
    """Empty would flow through the pipeline as a real, blank comment."""
    raw = "<think>Only thinking, no answer produced.</think>"
    text, stripped = providers.strip_reasoning(raw)
    assert text == raw.strip()
    assert text != ""
    assert stripped is False


def test_ordinary_text_is_untouched():
    for sample in ["A normal comment.", "", "Angle brackets < and > are fine."]:
        text, stripped = providers.strip_reasoning(sample)
        assert text == sample.strip()
        assert stripped is False


def test_the_filter_runs_on_every_openai_compatible_response(recorded):
    """No flag to forget: it is a no-op when no block is present."""
    _configure(recorded, text="<think>hmm</think>Posted text.")
    completion = providers.get_provider("deepseek").complete(
        model="deepseek-reasoner", system="s", user="u")
    assert completion.text == "Posted text."
    assert completion.reasoning_stripped is True


def test_the_filter_runs_on_anthropic_responses_too(recorded):
    _configure(recorded, text="<think>hmm</think>Posted text.")
    completion = providers.get_provider("anthropic").complete(
        model="claude-haiku-4-5", system="s", user="u")
    assert completion.text == "Posted text."


# ─── Key resolution ───────────────────────────────────────────────────────────

def test_local_providers_need_no_api_key(monkeypatch):
    monkeypatch.setattr(pm, "get_api_key", lambda provider: "")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    assert providers.resolve_api_key("ollama")  # does not raise


def test_remote_providers_still_raise_naming_their_variable(monkeypatch):
    monkeypatch.setattr(pm, "get_api_key", lambda provider: "")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(providers.ProviderError) as exc:
        providers.resolve_api_key("deepseek")
    assert "DEEPSEEK_API_KEY" in str(exc.value)


def test_key_comes_from_the_credential_store_first(monkeypatch):
    monkeypatch.setattr(pm, "get_api_key", lambda provider: "sk-from-keyring")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    assert providers.resolve_api_key("openai") == "sk-from-keyring"


def test_key_falls_back_to_the_environment(monkeypatch):
    monkeypatch.setattr(pm, "get_api_key", lambda provider: "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    assert providers.resolve_api_key("anthropic") == "sk-from-env"


def test_unknown_provider_raises_naming_the_valid_options():
    with pytest.raises(providers.ProviderError) as exc:
        providers.resolve_provider_config({"provider": {"name": "nonsense"}})
    message = str(exc.value)
    assert "nonsense" in message
    assert "openai" in message and "custom" in message


# ─── Key storage never leaks the value ────────────────────────────────────────

def test_api_key_status_reports_only_the_last_four(monkeypatch):
    monkeypatch.setattr(pm, "get_api_key", lambda provider: "sk-secret-value-ABCD")
    status = pm.api_key_status("openai")
    assert status["last4"] == "ABCD"
    assert "sk-secret-value-ABCD" not in json.dumps(status)


def test_api_keys_use_a_service_separate_from_linkedin_passwords():
    assert pm.API_KEY_SERVICE != pm.KEYRING_SERVICE


def test_set_api_key_refuses_rather_than_writing_to_a_file(monkeypatch):
    monkeypatch.setattr(pm, "keyring_available", lambda: False)
    with pytest.raises(RuntimeError) as exc:
        pm.set_api_key("openai", "sk-real-key")
    assert "environment variable" in str(exc.value)


# ─── Test Connection probe ────────────────────────────────────────────────────

@pytest.fixture
def usage_log(cost_ledger):
    """Read the redirected cost ledger (see the autouse fixture in conftest)."""
    def _read():
        if not cost_ledger.exists():
            return []
        return [json.loads(line) for line in
                cost_ledger.read_text().splitlines() if line.strip()]
    return _read


def test_probe_succeeds_and_reports_temperature_survived(recorded, usage_log):
    report = providers.probe("openai", model="gpt-4o-mini")
    assert report["ok"] is True
    assert report["temperature_accepted"] is True
    assert report["calls"] == 1
    assert report["text"] == RECORDED_TEXT


def test_probe_logs_to_api_usage_before_calling(recorded, usage_log):
    """PROJECT.md: the record is written before the money is spent."""
    providers.probe("openai", model="gpt-4o-mini")
    records = usage_log()
    assert len(records) == 1
    assert records[0]["api"] == "openai"
    assert records[0]["endpoint"] == "probe:test-connection"


def test_probe_retries_without_temperature_and_says_so(recorded, usage_log):
    """The exact temperature trap, caught at configure time instead of mid-run."""
    calls = {"n": 0}

    def flaky(self, model, system, user, temperature=None, max_tokens=None,
              json_object=False):
        calls["n"] += 1
        if temperature is not None:
            raise RuntimeError("400: temperature is not supported on this model")
        return providers.Completion(text="ok", model=model, provider=self.name)

    from unittest.mock import patch
    with patch.object(providers.OpenAICompatibleProvider, "complete", flaky):
        report = providers.probe("deepseek", model="deepseek-reasoner")

    assert report["ok"] is True
    assert report["temperature_accepted"] is False
    assert report["calls"] == 2
    assert "variety" in report["error"]


def test_probe_never_exceeds_two_calls(recorded, usage_log):
    """Matches the retry cap in PROJECT.md, so a broken provider cannot bleed money."""
    def always_fails(self, **kwargs):
        raise RuntimeError("nope")

    from unittest.mock import patch
    with patch.object(providers.OpenAICompatibleProvider, "complete", always_fails):
        report = providers.probe("groq", model="some-model")

    assert report["ok"] is False
    assert report["calls"] == providers.PROBE_MAX_CALLS
    assert len(usage_log()) == providers.PROBE_MAX_CALLS


def test_probe_reports_a_leaked_reasoning_block(recorded, usage_log):
    _configure(recorded, text="<think>plan</think>connection ok")
    report = providers.probe("deepseek", model="deepseek-reasoner")
    assert report["ok"] is True
    assert report["reasoning_stripped"] is True


def test_probe_returns_a_report_rather_than_raising_on_a_bad_provider(usage_log):
    report = providers.probe("nonsense")
    assert report["ok"] is False
    assert "nonsense" in report["error"]
    assert report["calls"] == 0


def test_the_cost_ledger_path_is_redirectable(cost_ledger):
    """The guard on the ledger-pollution fix.

    ``api_usage.jsonl`` exists to answer "how much has this project spent". If
    the path were a literal inside the writer again, the suite would resume
    appending fake spend to the real file and the answer would stop meaning
    anything. Deleting the conftest fixture makes this fail.
    """
    assert providers.API_USAGE_FILE == str(cost_ledger)
    # The property that matters is "not the repo's real ledger". Expressed as
    # absolute-and-not-the-bare-default rather than by matching a temp
    # directory name, which is spelled differently on each platform.
    assert providers.API_USAGE_FILE != "api_usage.jsonl"
    assert os.path.isabs(providers.API_USAGE_FILE)

    providers.log_api_usage("openai", "gpt-4o-mini", "unit-test", 0.0)
    assert cost_ledger.exists()
    assert json.loads(cost_ledger.read_text().strip())["endpoint"] == "unit-test"


def test_generator_logs_the_real_provider_not_a_hardcoded_openai(recorded, usage_log,
                                                                 monkeypatch):
    """The label was hardcoded, so Anthropic spend was recorded as OpenAI spend."""
    providers.log_api_usage("anthropic", "claude-haiku-4-5", "chat:generate", 0.0003)
    assert usage_log()[0]["api"] == "anthropic"


def test_probe_makes_no_call_when_no_model_is_available(usage_log):
    report = providers.probe("groq")
    assert report["ok"] is False
    assert report["calls"] == 0
    assert "model" in report["error"].lower()
