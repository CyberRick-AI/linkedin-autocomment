"""One interface over OpenAI, Anthropic, and xAI text generation.

ROADMAP Phase 8. The generation provider becomes configuration rather than a
hardcoded dependency: ``comment_generator`` and ``post_generator`` ask for a
completion, and the adapter for the profile's configured provider makes the
call. Switching providers is a config change, not a code change.

Every adapter normalises to one internal shape, :class:`Completion`, so callers
never touch a vendor SDK response object.

API keys are read from the OS credential store first (the same keyring layer
that holds LinkedIn passwords) and fall back to environment variables, which
keeps existing ``.env``-based installs working untouched.
"""

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ─── The provider registry ────────────────────────────────────────────────────

OPENAI = 'openai'
ANTHROPIC = 'anthropic'
XAI = 'xai'

PROVIDERS = (OPENAI, ANTHROPIC, XAI)

DEFAULT_PROVIDER = OPENAI

# Default remains OpenAI + gpt-4o-mini so existing installs see no change.
# Anthropic defaults to Haiku: this workload is short-form generation and
# relevance scoring, and never needs Opus-depth reasoning (PROJECT.md §2).
DEFAULT_MODELS = {
    OPENAI: 'gpt-4o-mini',
    ANTHROPIC: 'claude-haiku-4-5',
    XAI: 'grok-4',
}

# Environment-variable fallback, used when the OS credential store has no key.
API_KEY_ENV = {
    OPENAI: 'OPENAI_API_KEY',
    ANTHROPIC: 'ANTHROPIC_API_KEY',
    XAI: 'XAI_API_KEY',
}

# xAI speaks the OpenAI wire format, so its adapter is the OpenAI adapter
# pointed at a different host. That is why xAI adds no dependency.
XAI_BASE_URL = 'https://api.x.ai/v1'

# Anthropic removed temperature/top_p/top_k on its 5-series and on Opus 4.7+;
# sending one returns a 400. Older models still accept them. Comment generation
# varies temperature deliberately for variety, so the adapter has to know which
# models will take it rather than sending it blindly.
#
# This is an allowlist, not a denylist, so an unrecognised (likely newer) model
# omits temperature and succeeds rather than 400ing on a parameter it dropped.
_ANTHROPIC_SAMPLING_OK_PREFIXES = (
    'claude-haiku-4-5',
    'claude-sonnet-4-6',
    'claude-sonnet-4-5',
    'claude-sonnet-4-0',
    'claude-opus-4-6',
    'claude-opus-4-5',
    'claude-opus-4-1',
    'claude-opus-4-0',
    'claude-3-',
)


class ProviderError(Exception):
    """A provider is unknown, unconfigured, or missing its API key.

    Raised instead of letting a vendor SDK surface its own exception, so the
    dashboard and the CLI get one actionable message naming what to fix.
    """


@dataclass
class Completion:
    """The single normalised response shape every adapter returns.

    ``text`` is the assistant's message with surrounding whitespace stripped.
    ``model`` and ``provider`` record what actually served the request, which
    is what ``api_usage.jsonl`` logs and what the dashboard displays.
    """

    text: str
    model: str
    provider: str


def anthropic_accepts_temperature(model):
    """True when this Anthropic model still accepts sampling parameters.

    Anthropic removed ``temperature`` on the 5-series and on Opus 4.7+. An
    unrecognised model is treated as not accepting it: omitting temperature
    degrades variety, sending it to a model that dropped it fails the call.
    """
    return str(model).startswith(_ANTHROPIC_SAMPLING_OK_PREFIXES)


# ─── Key resolution ───────────────────────────────────────────────────────────

def resolve_api_key(provider, explicit=None):
    """Return the API key for ``provider``: explicit, then keyring, then env.

    Never logs or returns any part of the value to a caller that did not ask
    for it. Raises :class:`ProviderError` naming the missing key rather than
    letting the vendor SDK raise from inside its own constructor.
    """
    validate_provider(provider)

    if explicit:
        return explicit

    # Imported here rather than at module scope: profile_manager imports a lot
    # of the package, and providers.py is imported from inside it in places.
    from . import profile_manager as pm

    stored = pm.get_api_key(provider)
    if stored:
        return stored

    env_name = API_KEY_ENV[provider]
    from_env = os.environ.get(env_name, '').strip()
    if from_env:
        return from_env

    raise ProviderError(
        f"No API key for provider '{provider}'. Set one in the dashboard's "
        f"Settings screen, or export {env_name}."
    )


def validate_provider(provider):
    """Raise :class:`ProviderError` naming the valid options if unknown."""
    if provider not in PROVIDERS:
        raise ProviderError(
            f"Unknown provider {provider!r}. Valid options: "
            f"{', '.join(PROVIDERS)}."
        )
    return provider


def resolve_provider_config(config):
    """Read the ``provider`` block out of a profile config.

    Returns ``(provider, model)``. A missing block, a missing key, or an empty
    string falls back to the default, so a config written before this phase
    keeps working. An unknown provider *name* raises rather than silently
    falling back: that is a typo the user needs told about, and the gate in
    ROADMAP Phase 8 requires it fail rather than proceed.
    """
    block = (config or {}).get('provider') or {}

    name = (block.get('name') or '').strip().lower() or DEFAULT_PROVIDER
    validate_provider(name)

    model = (block.get('model') or '').strip() or DEFAULT_MODELS[name]
    return name, model


# ─── Adapters ─────────────────────────────────────────────────────────────────

class BaseProvider:
    """Interface every adapter implements."""

    name = None

    def __init__(self, api_key=None):
        self.api_key = resolve_api_key(self.name, api_key)

    def complete(self, model, system, user, temperature=None,
                 max_tokens=None, json_object=False):
        """Return a :class:`Completion` for one system+user exchange.

        ``json_object`` asks the provider to emit parseable JSON. It is a hint
        the adapters honour where the vendor supports it and fall back to a
        prompt instruction where it does not.
        """
        raise NotImplementedError


class OpenAIProvider(BaseProvider):
    """OpenAI chat completions. The historical default."""

    name = OPENAI
    base_url = None

    def _client(self):
        from openai import OpenAI

        if self.base_url:
            return OpenAI(api_key=self.api_key, base_url=self.base_url)
        return OpenAI(api_key=self.api_key)

    def complete(self, model, system, user, temperature=None,
                 max_tokens=None, json_object=False):
        kwargs = {
            'model': model,
            'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': user},
            ],
        }
        if temperature is not None:
            kwargs['temperature'] = temperature
        if max_tokens is not None:
            kwargs['max_tokens'] = max_tokens
        if json_object:
            kwargs['response_format'] = {'type': 'json_object'}

        response = self._client().chat.completions.create(**kwargs)
        text = (response.choices[0].message.content or '').strip()
        return Completion(text=text, model=model, provider=self.name)


class XAIProvider(OpenAIProvider):
    """xAI (Grok), which serves the OpenAI wire format at its own host.

    Subclassing the OpenAI adapter rather than writing a third HTTP client is
    the reason xAI support costs no new dependency.
    """

    name = XAI
    base_url = XAI_BASE_URL


class AnthropicProvider(BaseProvider):
    """Anthropic Messages API.

    Two shape differences from OpenAI worth naming, because they are why this
    is an adapter and not a base-URL swap:

    * the system prompt is a top-level ``system`` argument, not a message;
    * ``max_tokens`` is required, not optional.

    A third difference is behavioural: the 5-series and Opus 4.7+ reject
    ``temperature`` outright, so it is filtered by model rather than passed
    through. See :func:`anthropic_accepts_temperature`.
    """

    name = ANTHROPIC

    # Anthropic requires max_tokens. Callers that did not set one (OpenAI lets
    # you omit it) get a ceiling big enough for any comment or post this app
    # generates, so the parameter never silently truncates real output.
    DEFAULT_MAX_TOKENS = 4096

    def _client(self):
        import anthropic

        return anthropic.Anthropic(api_key=self.api_key)

    def complete(self, model, system, user, temperature=None,
                 max_tokens=None, json_object=False):
        if json_object:
            # No response_format equivalent; instruct in the system prompt.
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
        if temperature is not None:
            if anthropic_accepts_temperature(model):
                kwargs['temperature'] = temperature
            else:
                logger.debug(
                    "Model %s does not accept temperature; omitting it", model)

        response = self._client().messages.create(**kwargs)
        text = ''.join(
            block.text for block in response.content
            if getattr(block, 'type', None) == 'text'
        ).strip()
        return Completion(text=text, model=model, provider=self.name)


_ADAPTERS = {
    OPENAI: OpenAIProvider,
    ANTHROPIC: AnthropicProvider,
    XAI: XAIProvider,
}


def get_provider(provider, api_key=None):
    """Return a ready adapter, or raise :class:`ProviderError`.

    Both failure modes surface here rather than at the first call mid-run:
    an unknown provider name, and a missing API key.
    """
    validate_provider(provider)
    return _ADAPTERS[provider](api_key=api_key)


def get_provider_for_config(config, api_key=None):
    """Resolve a profile config to ``(adapter, model)``."""
    provider, model = resolve_provider_config(config)
    return get_provider(provider, api_key=api_key), model
