"""One interface over any OpenAI-compatible provider, plus Anthropic.

ROADMAP Phases 8 and 8b. The generation provider is configuration, not a
hardcoded dependency, and the list of providers is *open*: a provider is a
:class:`ProviderSpec` row rather than a class, and the ``custom`` spec takes a
base URL and a model so an endpoint this file has never heard of works with no
code change.

Two adapter kinds cover everything:

* ``openai_compatible`` — one adapter, driven by the spec's capability flags.
  OpenAI, xAI, DeepSeek, Groq, Together, OpenRouter, Mistral, Fireworks, and a
  local Ollama all go through it.
* ``anthropic`` — the one wire format that genuinely differs (system prompt is
  a top-level argument, ``max_tokens`` is required).

**Why capability flags rather than more adapter classes.** "OpenAI-compatible"
means the request *shape* matches. It does not mean the behaviour matches.
Phase 8 found one instance: Anthropic's 5-series rejects ``temperature``, which
this project varies deliberately so generated comments do not all sound alike.
The flags below are that lesson generalised, so the next such difference is a
row edit rather than a new failure mode.

API keys are read from the OS credential store first and fall back to
environment variables, which keeps existing ``.env`` installs working.
"""

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)


# ─── Capability vocabulary ────────────────────────────────────────────────────

KIND_OPENAI = 'openai_compatible'
KIND_ANTHROPIC = 'anthropic'

# How a provider handles the temperature parameter.
TEMP_YES = 'yes'            # always send it
TEMP_NO = 'no'              # never send it; this vendor rejects or ignores it
TEMP_BY_MODEL = 'by_model'  # depends on the model; consult the spec's allowlist

# How a provider is asked for JSON.
JSON_NATIVE = 'response_format'  # supports response_format={"type":"json_object"}
JSON_PROMPT = 'prompt'           # no native mode; instruct in the system prompt


@dataclass(frozen=True)
class ProviderSpec:
    """Everything that distinguishes one provider from another.

    Adding a provider means adding one of these. There is deliberately no
    per-vendor code path: anything a vendor does differently is a field here.
    """

    name: str
    label: str
    kind: str = KIND_OPENAI

    # None means "the SDK's own default host" (OpenAI itself).
    base_url: str = None
    key_env: str = ''

    default_model: str = ''
    # False when the default model string could not be verified offline. The
    # Settings screen surfaces this rather than presenting a guess as checked.
    default_model_verified: bool = False

    # Capability flags.
    accepts_temperature: str = TEMP_YES
    # Model prefixes that accept temperature, consulted only when
    # accepts_temperature is TEMP_BY_MODEL.
    temperature_ok_prefixes: tuple = ()
    json_mode: str = JSON_NATIVE
    # Reasoning models renamed this parameter.
    max_tokens_param: str = 'max_tokens'
    supports_system_role: bool = True

    # Local providers need no key and must not be asked for one.
    local: bool = False
    # The custom spec has no built-in host; the user supplies it.
    requires_base_url: bool = False

    def temperature_allowed(self, model):
        """True when ``model`` on this provider will accept a temperature."""
        if self.accepts_temperature == TEMP_YES:
            return True
        if self.accepts_temperature == TEMP_NO:
            return False
        # by_model: an unrecognised (likely newer) model is treated as
        # rejecting it. Losing variety is recoverable; a 400 on every
        # generation call is not.
        return str(model).startswith(self.temperature_ok_prefixes)


# ─── The registry ─────────────────────────────────────────────────────────────
#
# Base URLs are the vendors' documented OpenAI-compatible endpoints. Default
# models are marked verified only where the string is known-good; everywhere
# else the Settings screen asks the user to supply or confirm one, and Test
# Connection is how it gets confirmed. Shipping six guessed model IDs would
# repeat the xAI mistake five more times.

SPECS = {
    'openai': ProviderSpec(
        name='openai', label='OpenAI',
        base_url=None, key_env='OPENAI_API_KEY',
        default_model='gpt-4o-mini', default_model_verified=True,
        # OpenAI's reasoning families reject or ignore temperature.
        accepts_temperature=TEMP_BY_MODEL,
        temperature_ok_prefixes=('gpt-4', 'gpt-3.5', 'chatgpt-4'),
    ),
    'anthropic': ProviderSpec(
        name='anthropic', label='Anthropic (Claude)', kind=KIND_ANTHROPIC,
        base_url=None, key_env='ANTHROPIC_API_KEY',
        default_model='claude-haiku-4-5', default_model_verified=True,
        # Removed on the 5-series and Opus 4.7+; still accepted below that.
        accepts_temperature=TEMP_BY_MODEL,
        temperature_ok_prefixes=(
            'claude-haiku-4-5', 'claude-sonnet-4-6', 'claude-sonnet-4-5',
            'claude-sonnet-4-0', 'claude-opus-4-6', 'claude-opus-4-5',
            'claude-opus-4-1', 'claude-opus-4-0', 'claude-3-',
        ),
        json_mode=JSON_PROMPT,
    ),
    'xai': ProviderSpec(
        name='xai', label='xAI (Grok)',
        base_url='https://api.x.ai/v1', key_env='XAI_API_KEY',
        # Verified live 2026-08-01 via Test Connection: the model answered,
        # temperature was accepted, one call, $0.0002. It shipped as a guess in
        # Phase 8 because the per-phase paid budget is zero and no offline check
        # can confirm a model identifier.
        default_model='grok-4', default_model_verified=True,
    ),
    'deepseek': ProviderSpec(
        name='deepseek', label='DeepSeek',
        base_url='https://api.deepseek.com/v1', key_env='DEEPSEEK_API_KEY',
        default_model='deepseek-chat', default_model_verified=False,
    ),
    'groq': ProviderSpec(
        name='groq', label='Groq',
        base_url='https://api.groq.com/openai/v1', key_env='GROQ_API_KEY',
    ),
    'together': ProviderSpec(
        name='together', label='Together AI',
        base_url='https://api.together.xyz/v1', key_env='TOGETHER_API_KEY',
    ),
    'openrouter': ProviderSpec(
        name='openrouter', label='OpenRouter',
        base_url='https://openrouter.ai/api/v1', key_env='OPENROUTER_API_KEY',
    ),
    'mistral': ProviderSpec(
        name='mistral', label='Mistral',
        base_url='https://api.mistral.ai/v1', key_env='MISTRAL_API_KEY',
    ),
    'fireworks': ProviderSpec(
        name='fireworks', label='Fireworks AI',
        base_url='https://api.fireworks.ai/inference/v1',
        key_env='FIREWORKS_API_KEY',
    ),
    'ollama': ProviderSpec(
        name='ollama', label='Ollama (local)',
        base_url='http://localhost:11434/v1', key_env='',
        local=True, json_mode=JSON_PROMPT,
    ),
    'custom': ProviderSpec(
        name='custom', label='Custom (OpenAI-compatible)',
        base_url=None, key_env='CUSTOM_API_KEY',
        requires_base_url=True, json_mode=JSON_PROMPT,
    ),
}

PROVIDERS = tuple(SPECS)
DEFAULT_PROVIDER = 'openai'

# Kept for callers that only want the model string.
DEFAULT_MODELS = {name: spec.default_model for name, spec in SPECS.items()}
API_KEY_ENV = {name: spec.key_env for name, spec in SPECS.items()}

XAI_BASE_URL = SPECS['xai'].base_url


class ProviderError(Exception):
    """A provider is unknown, unconfigured, or missing its API key."""


@dataclass
class Completion:
    """The single normalised response shape every adapter returns."""

    text: str
    model: str
    provider: str
    # True when a chain-of-thought block was stripped out of the raw response.
    reasoning_stripped: bool = False


# ─── Reasoning-output filter ──────────────────────────────────────────────────
#
# This is the failure this phase exists to prevent. DeepSeek's reasoner, the
# Hermes reasoning variants, and several models served through OpenRouter or
# Ollama emit chain-of-thought either inline in <think> tags or in a separate
# reasoning_content field. Unfiltered, that becomes the text this project posts
# to LinkedIn as a comment.

_THINK_BLOCK = re.compile(
    r'<(think|thinking|reasoning)\b[^>]*>.*?</\1>',
    re.DOTALL | re.IGNORECASE,
)
# An unterminated opening tag: the model started reasoning and hit the token
# cap before closing it. Everything after it is chain-of-thought.
_UNCLOSED_THINK = re.compile(
    r'<(think|thinking|reasoning)\b[^>]*>.*\Z',
    re.DOTALL | re.IGNORECASE,
)


def strip_reasoning(text):
    """Remove chain-of-thought blocks. Returns ``(clean_text, was_stripped)``.

    Applied to every OpenAI-compatible response, because it is a no-op when no
    such block is present and a posted-monologue incident when it is missing.

    **Never returns empty because of stripping.** If a response is entirely a
    reasoning block, the original is returned instead: a comment that looks
    wrong is a review-gate problem, and an empty one is a silent data loss that
    the pipeline would carry forward as a real result.
    """
    if not text:
        return '', False

    cleaned = _THINK_BLOCK.sub('', text)
    cleaned = _UNCLOSED_THINK.sub('', cleaned)
    cleaned = cleaned.strip()

    if not cleaned:
        logger.warning(
            "Response was entirely a reasoning block; keeping the raw text "
            "rather than returning nothing. Check the model choice.")
        return text.strip(), False

    return cleaned, cleaned != text.strip()


# ─── Paid-call logging ────────────────────────────────────────────────────────

# The cost ledger's path, as a module attribute rather than a literal, so the
# test suite can redirect it. Before this was centralised, every test run that
# exercised the generator appended fake spend records to the real file, which
# left it unable to answer the one question it exists to answer: how much has
# actually been spent. See tests/conftest.py.
API_USAGE_FILE = 'api_usage.jsonl'


def log_api_usage(provider, model, endpoint, est_cost):
    """Append a paid-call record to the cost ledger.

    PROJECT.md requires this be written **before** the call, so a crash mid
    call still leaves a record that money may have been spent.
    """
    try:
        with open(API_USAGE_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps({
                'timestamp': datetime.now().isoformat(),
                'api': provider,
                'model': model,
                'endpoint': endpoint,
                'estimated_cost': est_cost,
            }) + '\n')
    except Exception:
        logger.debug("Failed to write api_usage.jsonl", exc_info=True)


# ─── Spec / config resolution ─────────────────────────────────────────────────

def get_spec(provider):
    """Return the :class:`ProviderSpec` for ``provider``, or raise."""
    spec = SPECS.get(provider)
    if spec is None:
        raise ProviderError(
            f"Unknown provider {provider!r}. Valid options: "
            f"{', '.join(PROVIDERS)}."
        )
    return spec


def validate_provider(provider):
    """Raise :class:`ProviderError` naming the valid options if unknown."""
    get_spec(provider)
    return provider


def resolve_provider_config(config):
    """Read the ``provider`` block out of a profile config.

    Returns ``(provider, model, base_url)``. A missing block or key falls back
    to the default, so a config written before Phase 8 keeps working. An unknown
    provider *name* raises rather than falling back silently.
    """
    block = (config or {}).get('provider') or {}

    name = (block.get('name') or '').strip().lower() or DEFAULT_PROVIDER
    spec = get_spec(name)

    model = (block.get('model') or '').strip() or spec.default_model
    base_url = (block.get('base_url') or '').strip() or spec.base_url

    if spec.requires_base_url and not base_url:
        raise ProviderError(
            f"Provider '{name}' needs a base_url. Set one in the dashboard's "
            f"Settings screen (for example http://localhost:8000/v1)."
        )
    if not model:
        raise ProviderError(
            f"Provider '{name}' has no default model, so a model name is "
            f"required. Set one in the dashboard's Settings screen."
        )

    return name, model, base_url


def resolve_api_key(provider, explicit=None):
    """Return the API key: explicit, then keyring, then environment.

    Local providers need none and get an empty string. Remote providers raise
    :class:`ProviderError` naming the missing variable rather than letting a
    vendor SDK raise from inside its own constructor.
    """
    spec = get_spec(provider)

    if explicit:
        return explicit

    if spec.local:
        # Ollama and friends accept any non-empty string; the OpenAI client
        # requires one to be present at all.
        return 'not-needed'

    from . import profile_manager as pm

    stored = pm.get_api_key(provider)
    if stored:
        return stored

    from_env = os.environ.get(spec.key_env, '').strip() if spec.key_env else ''
    if from_env:
        return from_env

    raise ProviderError(
        f"No API key for provider '{provider}'. Set one in the dashboard's "
        f"Settings screen, or export {spec.key_env}."
    )


# ─── Adapters ─────────────────────────────────────────────────────────────────

class BaseProvider:
    """Interface every adapter implements."""

    def __init__(self, spec, api_key=None, base_url=None):
        self.spec = spec
        self.name = spec.name
        self.base_url = base_url or spec.base_url
        self.api_key = resolve_api_key(spec.name, api_key)

    def complete(self, model, system, user, temperature=None,
                 max_tokens=None, json_object=False):
        raise NotImplementedError


class OpenAICompatibleProvider(BaseProvider):
    """One adapter for every provider that speaks the OpenAI wire format.

    All per-vendor variation is read off the spec, so adding a provider adds no
    code here.
    """

    def _client(self):
        from openai import OpenAI

        if self.base_url:
            return OpenAI(api_key=self.api_key, base_url=self.base_url)
        return OpenAI(api_key=self.api_key)

    def complete(self, model, system, user, temperature=None,
                 max_tokens=None, json_object=False):
        spec = self.spec

        if spec.supports_system_role:
            messages = [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': user},
            ]
        else:
            # No system role: fold it into the first user turn rather than
            # dropping it, or the persona silently stops applying.
            messages = [{'role': 'user', 'content': f"{system}\n\n{user}"}]

        if json_object and spec.json_mode == JSON_PROMPT:
            messages[0]['content'] += (
                "\n\nRespond with a single valid JSON object and nothing else. "
                "No prose, no markdown code fences."
            )

        kwargs = {'model': model, 'messages': messages}

        if temperature is not None and spec.temperature_allowed(model):
            kwargs['temperature'] = temperature
        elif temperature is not None:
            logger.debug("%s/%s does not accept temperature; omitting it",
                         spec.name, model)

        if max_tokens is not None:
            kwargs[spec.max_tokens_param] = max_tokens

        if json_object and spec.json_mode == JSON_NATIVE:
            kwargs['response_format'] = {'type': 'json_object'}

        response = self._client().chat.completions.create(**kwargs)
        message = response.choices[0].message

        raw = message.content or ''
        # Some servers return the chain-of-thought in its own field rather than
        # inline. Reading content alone is correct there; this is belt and
        # braces for servers that do both.
        if getattr(message, 'reasoning_content', None) and not raw.strip():
            logger.warning(
                "%s/%s returned only reasoning_content and no answer", spec.name, model)

        text, stripped = strip_reasoning(raw)
        return Completion(text=text, model=model, provider=spec.name,
                          reasoning_stripped=stripped)


class AnthropicProvider(BaseProvider):
    """Anthropic Messages API: the one genuinely different wire format.

    System prompt is a top-level argument rather than a message, and
    ``max_tokens`` is required rather than optional.
    """

    DEFAULT_MAX_TOKENS = 4096

    def _client(self):
        import anthropic

        if self.base_url:
            return anthropic.Anthropic(api_key=self.api_key, base_url=self.base_url)
        return anthropic.Anthropic(api_key=self.api_key)

    def complete(self, model, system, user, temperature=None,
                 max_tokens=None, json_object=False):
        spec = self.spec

        if json_object:
            system = (
                f"{system}\n\n"
                "Respond with a single valid JSON object and nothing else. "
                "No prose, no markdown code fences."
            )

        kwargs = {
            'model': model,
            'system': system,
            'messages': [{'role': 'user', 'content': user}],
            'max_tokens': max_tokens or self.DEFAULT_MAX_TOKENS,
        }
        if temperature is not None and spec.temperature_allowed(model):
            kwargs['temperature'] = temperature
        elif temperature is not None:
            logger.debug("%s/%s does not accept temperature; omitting it",
                         spec.name, model)

        response = self._client().messages.create(**kwargs)
        raw = ''.join(
            block.text for block in response.content
            if getattr(block, 'type', None) == 'text'
        )
        text, stripped = strip_reasoning(raw)
        return Completion(text=text, model=model, provider=spec.name,
                          reasoning_stripped=stripped)


_ADAPTERS = {
    KIND_OPENAI: OpenAICompatibleProvider,
    KIND_ANTHROPIC: AnthropicProvider,
}


def get_provider(provider, api_key=None, base_url=None):
    """Return a ready adapter, or raise :class:`ProviderError`.

    Both failure modes surface here rather than at the first call mid-run: an
    unknown provider name, and a missing API key.
    """
    spec = get_spec(provider)
    return _ADAPTERS[spec.kind](spec, api_key=api_key, base_url=base_url)


def get_provider_for_config(config, api_key=None):
    """Resolve a profile config to ``(adapter, model)``."""
    provider, model, base_url = resolve_provider_config(config)
    return get_provider(provider, api_key=api_key, base_url=base_url), model


# ─── Test Connection ──────────────────────────────────────────────────────────

PROBE_SYSTEM = "You are a connection test. Answer in one short sentence."
PROBE_USER = "Reply with exactly: connection ok"

# The only deliberate spend in this project. Two calls maximum, matching the
# retry cap in PROJECT.md, and only ever when a human presses the button.
PROBE_MAX_CALLS = 2
PROBE_EST_COST = 0.0002


def probe(provider, model=None, base_url=None, api_key=None):
    """Send one small prompt and report what actually worked.

    You cannot enumerate every provider's quirks in advance, so this finds out
    empirically at the moment a provider is configured, rather than mid-run
    when a real comment is being generated.

    Returns a report dict. Never raises: a failed probe is a result, not an
    error, because reporting *why* it failed is the whole point.
    """
    report = {
        'ok': False,
        'provider': provider,
        'model': model,
        'calls': 0,
        'temperature_accepted': None,
        'reasoning_stripped': None,
        'text': '',
        'error': None,
    }

    try:
        spec = get_spec(provider)
        model = model or spec.default_model
        report['model'] = model
        if not model:
            report['error'] = f"Provider '{provider}' needs a model name."
            return report

        adapter = get_provider(provider, api_key=api_key, base_url=base_url)
    except ProviderError as e:
        report['error'] = str(e)
        return report

    def _call(temperature):
        report['calls'] += 1
        log_api_usage(provider, model, 'probe:test-connection', PROBE_EST_COST)
        return adapter.complete(
            model=model, system=PROBE_SYSTEM, user=PROBE_USER,
            temperature=temperature, max_tokens=32,
        )

    # First attempt with a temperature, so the report can say whether variety
    # is available on this provider/model pair.
    try:
        completion = _call(0.7)
        report.update(ok=True, temperature_accepted=True,
                      text=completion.text[:200],
                      reasoning_stripped=completion.reasoning_stripped)
        return report
    except Exception as e:
        first_error = str(e)

    if report['calls'] >= PROBE_MAX_CALLS:
        report['error'] = first_error
        return report

    # Retry without temperature. If that works, the parameter was the problem
    # and the spec's flag for this provider needs correcting.
    try:
        completion = _call(None)
        report.update(ok=True, temperature_accepted=False,
                      text=completion.text[:200],
                      reasoning_stripped=completion.reasoning_stripped,
                      error=("Succeeded only without temperature. Comment "
                             "variety will be flatter on this model."))
        return report
    except Exception as e:
        report['error'] = str(e)
        return report
