"""Guards on the suite's own hermeticity (Phase 12).

The suite has always *claimed* to be offline. These tests make the claim
checkable, because two ways of breaking it were found while establishing the
Phase 12 baseline and neither would have failed a single existing test.

**It depended on ambient environment.** 26 tests construct a generator, which
resolves an API key, which raises when none is set. CI exports
``OPENAI_API_KEY=sk-dummy``, so CI was green. A clean checkout on the build
machine reported 11 failures and 15 errors against the same, unchanged code.
Thirteen phases had recorded "869 tests passing" as a baseline that could not
be reproduced without an undocumented environment variable.

**A funded key could reach a test.** ``load_dotenv()`` runs at import time in
six modules, so importing the package under test loads the developer's ``.env``
into ``os.environ``. Nothing today spends it, because provider calls are
mocked and the Keychain is faked. That is protection by coincidence: every
guard is somewhere else, and one missed mock reaches a real vendor.

The rule these encode is the one from Phase 11b and Phase 7: when the honest
answer is "this depends on something nobody declared", say so in a test.
"""

import os
import re

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import providers

from conftest import DUMMY_API_KEY


# A vendor key, loosely: a long opaque token. Deliberately permissive, because
# this is asserting that nothing *resembling* a credential is present, not
# parsing one.
REAL_LOOKING_KEY = re.compile(r"^(sk-|xai-|gsk_|sk-ant-)[A-Za-z0-9_\-]{20,}$")


def test_every_provider_key_is_the_dummy():
    """The fixture covers every provider, not just the one CI happened to set.

    Deleting ``provider_keys`` from conftest makes this fail on a machine with
    no keys exported, which is the machine the suite must work on.
    """
    for name, spec in providers.SPECS.items():
        if not spec.key_env:
            continue
        assert os.environ.get(spec.key_env) == DUMMY_API_KEY, (
            f"{name}: {spec.key_env} is not pinned to the test dummy"
        )


def test_no_environment_variable_holds_a_real_looking_key():
    """A funded key must not be visible to any test, however it got there.

    Broader than the check above on purpose. That one asserts the fixture ran;
    this one asserts the outcome, so a key arriving under a name no spec
    declares is still caught.
    """
    leaked = [
        name for name, value in os.environ.items()
        if REAL_LOOKING_KEY.match(value.strip())
    ]
    assert leaked == [], (
        f"environment holds real-looking credentials: {sorted(leaked)}. "
        "The suite must never see a key it could spend."
    )


def test_the_dummy_is_not_a_plausible_key():
    """The stand-in must fail at a vendor rather than authenticate anywhere.

    A dummy shaped like the real thing is worse than no dummy: it survives a
    validity check and the failure surfaces as a billing line.
    """
    assert not REAL_LOOKING_KEY.match(DUMMY_API_KEY)


def test_resolving_a_key_never_returns_a_developers_credential():
    """The whole chain, not just the environment.

    ``resolve_api_key`` reads explicit, then keyring, then environment. The
    keyring leg is closed by ``fake_keyring`` and the environment leg by
    ``provider_keys``. This asserts the composition, since each fixture alone
    leaves a route open.
    """
    for name, spec in providers.SPECS.items():
        resolved = providers.resolve_api_key(name)
        if spec.local:
            assert resolved == "not-needed"
        else:
            assert resolved == DUMMY_API_KEY, f"{name} resolved to something else"


def test_the_keychain_is_faked_for_every_test(fake_keyring):
    """The stop condition in CLAUDE.md, expressed as a test.

    Removing the autouse ``fake_keyring`` fixture would let the suite write
    into the real macOS Keychain. That is named a stop condition rather than a
    preference, so it gets a guard that fails rather than a comment.
    """
    assert pm.keyring is fake_keyring
    assert fake_keyring.store == {} or all(
        isinstance(k, tuple) for k in fake_keyring.store
    )


def test_linkedin_credentials_are_not_in_the_environment():
    """``.env`` is loaded at import time and may carry a real LinkedIn password.

    ``profile_manager`` falls back to these when a profile has no stored
    credential, so a developer's own login could silently back a test.
    """
    assert os.environ.get("LINKEDIN_USERNAME") is None
    assert os.environ.get("LINKEDIN_PASSWORD") is None


def test_a_test_asking_for_a_missing_key_still_gets_the_error(monkeypatch):
    """Pinning the keys must not disarm the missing-key path.

    Phase 8's actionable error is the reason ``resolve_api_key`` exists. If
    pinning made it unreachable, the fixture would have traded one silent
    failure for another.
    """
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(providers.ProviderError) as exc:
        providers.resolve_api_key("deepseek")
    assert "DEEPSEEK_API_KEY" in str(exc.value)
