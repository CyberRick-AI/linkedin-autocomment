"""The per-(profile, platform) scheduled-posting resolver, and the cache split.

Phase 2, entirely offline — no Buffer call is made anywhere in this file.

The load-bearing claim is that **LinkedIn comes out byte-identical**. That is
asserted against `tests/fixtures/scheduled_resolution_golden.json`, a snapshot
taken by running the PRE-CHANGE code (commit 873252e) over synthetic profiles.
Asserting against a captured snapshot rather than re-deriving the expected value
is the point: a test that recomputes the answer the same way the code does would
agree with the code however wrong both were.
"""

import io
import json
import os

import pytest

from linkedin_automation import buffer_client as bc
from linkedin_automation import profile_manager as pm

GOLDEN_PATH = os.path.join(os.path.dirname(__file__), "fixtures",
                           "scheduled_resolution_golden.json")

with io.open(GOLDEN_PATH, encoding="utf-8") as _f:
    GOLDEN = json.load(_f)

# The user configs the golden was captured from. Kept here so a change to one
# without the other is a failure rather than a silent divergence.
CASES = {
    "flat_configured": {"scheduled_posting": {
        "buffer_channel_id": "chan_linkedin_0001",
        "identity_slug": "example-person-one",
        "drain": {"enabled": True, "interval_minutes": 300}}},
    "flat_default": {},
    "flat_blank": {"scheduled_posting": {
        "buffer_channel_id": "", "identity_slug": ""}},
    "flat_whitespace": {"scheduled_posting": {
        "buffer_channel_id": "  chan_ws  ", "identity_slug": "  example-ws  "}},
    "flat_null": {"scheduled_posting": {
        "buffer_channel_id": None, "identity_slug": None}},
}


@pytest.fixture(autouse=True)
def _no_ambient_buffer_keys(monkeypatch):
    """Never let the developer's .env decide a result.

    buffer_client calls load_dotenv() at import, so a real BUFFER_API_KEY_X on
    this machine would satisfy a test that meant to prove a MISSING one fails.
    Cleared for every test here; the ones that need a key set their own.
    """
    for var in ("BUFFER_API_KEY", "BUFFER_API_KEY_X"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def raw_config(monkeypatch):
    """Install a config WITHOUT the default deep-merge.

    Needed for the defensive branches: the template backfills
    `buffer_api_key_env`, so a merged config can never be missing it.
    """
    def _install(cfg):
        monkeypatch.setattr(pm, "get_profile_config", lambda *a, **k: cfg)
        return cfg
    return _install


@pytest.fixture
def with_config(monkeypatch):
    """Resolve against a given raw user config, through the real deep-merge."""
    def _install(user_config):
        merged = pm._deep_merge(pm.load_default_config(), user_config)
        monkeypatch.setattr(pm, "get_profile_config", lambda *a, **k: merged)
        return merged
    return _install


# ─── LinkedIn is byte-identical ──────────────────────────────────────────────

@pytest.mark.parametrize("case", sorted(CASES))
def test_linkedin_resolution_matches_the_pre_change_snapshot(case, with_config,
                                                             monkeypatch):
    """The whole compatibility claim, one case at a time."""
    with_config(CASES[case])
    monkeypatch.setenv("BUFFER_API_KEY", "a-key")
    got = pm.resolve_scheduled("someprofile", "linkedin")
    expected = GOLDEN[case]

    assert got["channel_id"] == expected["channel_id"]
    assert got["identity_slug"] == expected["identity_slug"]
    assert got["drain"] == expected["drain"]


def test_linkedin_reads_the_same_env_var_it_always_did():
    assert GOLDEN["_api_key_source_env_var"] == "BUFFER_API_KEY"
    assert pm.SCHEDULED_DEFAULT_KEY_ENV == "BUFFER_API_KEY"


def test_linkedin_key_comes_from_the_bare_variable(with_config, monkeypatch):
    with_config(CASES["flat_configured"])
    monkeypatch.setenv("BUFFER_API_KEY", "the-linkedin-key")
    assert pm.resolve_scheduled("p", "linkedin")["api_key"] == "the-linkedin-key"


def test_a_missing_linkedin_key_does_not_raise(with_config, monkeypatch):
    """Asking for a channel id must not start failing on an unrelated variable.

    The pre-change `_scheduled_channel_id` never touched the key at all. A
    caller that actually needs one still hits buffer_client's `key or api_key()`
    fallback, which raises the message it always did — so nothing is silently
    empty, it just fails at the point it always failed.
    """
    with_config(CASES["flat_configured"])
    monkeypatch.delenv("BUFFER_API_KEY", raising=False)
    resolved = pm.resolve_scheduled("p", "linkedin")
    assert resolved["api_key"] is None
    assert resolved["channel_id"] == "chan_linkedin_0001"


def test_the_platform_defaults_to_linkedin():
    assert pm.SCHEDULED_DEFAULT_PLATFORM == "linkedin"


def test_the_dashboard_helper_is_unchanged_for_existing_callers(with_config,
                                                                monkeypatch):
    """The drain injects this function and calls it with ONE argument."""
    from linkedin_automation import dashboard
    with_config(CASES["flat_configured"])
    assert dashboard._scheduled_channel_id("someprofile") == "chan_linkedin_0001"


# ─── The overlay ─────────────────────────────────────────────────────────────

X_CONFIG = {"scheduled_posting": {
    "buffer_channel_id": "chan_linkedin_0001",
    "identity_slug": "example-person-one",
    "platforms": {"x": {
        "buffer_api_key_env": "BUFFER_API_KEY_X",
        "buffer_channel_id": "chan_x_0002",
        "identity_slug": "example_x_handle",
        "drain": {"enabled": False, "interval_minutes": 300}}}}}


def test_an_overlaid_platform_resolves_its_own_settings(with_config, monkeypatch):
    with_config(X_CONFIG)
    monkeypatch.setenv("BUFFER_API_KEY_X", "the-x-key")
    monkeypatch.setenv("BUFFER_API_KEY", "the-linkedin-key")
    got = pm.resolve_scheduled("p", "x")
    assert got["channel_id"] == "chan_x_0002"
    assert got["identity_slug"] == "example_x_handle"
    assert got["api_key"] == "the-x-key"
    assert got["api_key_env"] == "BUFFER_API_KEY_X"


def test_the_two_platforms_do_not_bleed_into_each_other(with_config, monkeypatch):
    """The point of the overlay: one profile, two accounts, no crossover."""
    with_config(X_CONFIG)
    monkeypatch.setenv("BUFFER_API_KEY", "the-linkedin-key")
    monkeypatch.setenv("BUFFER_API_KEY_X", "the-x-key")
    li = pm.resolve_scheduled("p", "linkedin")
    x = pm.resolve_scheduled("p", "x")
    assert li["channel_id"] != x["channel_id"]
    assert li["api_key"] != x["api_key"]
    assert li["identity_slug"] != x["identity_slug"]


def test_the_default_template_ships_an_x_overlay():
    """`platforms` is inert until the resolver reads it, but it must be there."""
    sp = pm.load_default_config()["scheduled_posting"]
    assert sp["platforms"]["x"]["buffer_api_key_env"] == "BUFFER_API_KEY_X"
    # The legacy flat fields are untouched beside it.
    assert sp["buffer_channel_id"] == ""
    assert sp["identity_slug"] == ""


def test_an_existing_profile_gains_the_overlay_through_the_deep_merge(with_config):
    """A config written before this phase must not need migrating."""
    merged = with_config({"scheduled_posting": {
        "buffer_channel_id": "chan_legacy", "identity_slug": "example-legacy"}})
    sp = merged["scheduled_posting"]
    assert sp["buffer_channel_id"] == "chan_legacy"        # user value kept
    assert sp["platforms"]["x"]["buffer_api_key_env"] == "BUFFER_API_KEY_X"


# ─── Env indirection fails loudly ────────────────────────────────────────────

def test_a_missing_named_variable_raises_naming_itself(with_config, monkeypatch):
    with_config(X_CONFIG)
    monkeypatch.delenv("BUFFER_API_KEY_X", raising=False)
    with pytest.raises(pm.ScheduledConfigError) as exc:
        pm.resolve_scheduled("p", "x")
    msg = str(exc.value)
    assert "BUFFER_API_KEY_X" in msg
    assert "x" in msg


def test_a_blank_named_variable_is_treated_as_missing(with_config, monkeypatch):
    """Present-but-empty must not resolve to an empty key."""
    with_config(X_CONFIG)
    monkeypatch.setenv("BUFFER_API_KEY_X", "   ")
    with pytest.raises(pm.ScheduledConfigError):
        pm.resolve_scheduled("p", "x")


def test_the_linkedin_key_is_never_used_as_a_fallback_for_x(with_config,
                                                            monkeypatch):
    """Falling back would post X content through the LinkedIn account's key."""
    with_config(X_CONFIG)
    monkeypatch.setenv("BUFFER_API_KEY", "the-linkedin-key")
    monkeypatch.delenv("BUFFER_API_KEY_X", raising=False)
    with pytest.raises(pm.ScheduledConfigError):
        pm.resolve_scheduled("p", "x")


def test_an_overlay_with_no_key_variable_named_raises(raw_config):
    """A defensive branch, reached only WITHOUT the default merge.

    The template backfills `buffer_api_key_env`, so a merged config always has
    one — which is why this installs a raw config. Keeping the branch means a
    hand-edited or future template that drops the field fails loudly instead of
    resolving an empty key.
    """
    raw_config({"scheduled_posting": {"platforms": {"x": {
        "buffer_channel_id": "chan_x"}}}})
    with pytest.raises(pm.ScheduledConfigError) as exc:
        pm.resolve_scheduled("p", "x")
    assert "buffer_api_key_env" in str(exc.value)


def test_an_overlay_naming_a_blank_variable_raises(with_config):
    """The merged path CAN still be blanked by a user editing their config."""
    with_config({"scheduled_posting": {"platforms": {"x": {
        "buffer_api_key_env": "  ", "buffer_channel_id": "chan_x"}}}})
    with pytest.raises(pm.ScheduledConfigError) as exc:
        pm.resolve_scheduled("p", "x")
    assert "buffer_api_key_env" in str(exc.value)


def test_an_unknown_platform_refuses_rather_than_using_linkedins_channel(
        with_config, monkeypatch):
    """A typo must not publish to LinkedIn.

    Falling back to the flat fields would hand back LinkedIn's channel for
    platform="mastodon" — silent, unrecoverable, and exactly the failure the
    identity guard exists to prevent elsewhere.
    """
    with_config(X_CONFIG)
    with pytest.raises(pm.ScheduledConfigError) as exc:
        pm.resolve_scheduled("p", "mastodon")
    msg = str(exc.value)
    assert "mastodon" in msg
    assert "chan_linkedin_0001" not in msg


# ─── Cache isolation: the multi-account landmine ─────────────────────────────

@pytest.fixture
def fake_buffer(monkeypatch):
    """Answer getChannel/GetLimits from the key, so crossover is observable."""
    calls = []

    def fake_gql(query, variables=None, key=None, label="graphql", session=None):
        calls.append((label, key))
        if label == "getChannel":
            return {"data": {"channel": {"id": variables["id"],
                                         "organizationId": "org::" + str(key),
                                         "service": "linkedin"}}}
        org = variables["org"]
        return {"data": {
            "account": {"organizations": [
                {"id": org, "limits": {"scheduledPosts": 10}}]},
            "posts": {"edges": [{"node": {"channelId": "other"}}]
                      * (3 if "A" in org else 7)}}}

    monkeypatch.setattr(bc, "gql", fake_gql)
    bc.clear_caches()
    yield calls
    bc.clear_caches()


def test_two_keys_on_the_same_channel_do_not_share_a_cached_channel(fake_buffer):
    """THE BUG THIS PHASE FIXES.

    Both caches used to key on channel_id ALONE. With one Buffer account that is
    harmless; with two it means the second key is served the FIRST key's
    channel — and so the first key's organizationId.
    """
    a = bc.get_channel("SAME_CHANNEL", key="KEY_A")
    b = bc.get_channel("SAME_CHANNEL", key="KEY_B")
    assert a["organizationId"] == "org::KEY_A"
    assert b["organizationId"] == "org::KEY_B"
    assert len(fake_buffer) == 2, "the second key must not have been served a hit"


def test_two_keys_on_the_same_channel_do_not_share_a_cached_slot_count(
        fake_buffer):
    """A slot count is a number about ONE account's plan.

    Serving the other account's count is how a feeder decides how many posts
    somebody else can take — and then walks into LimitReachedError, or holds
    back posts that could have gone.
    """
    a = bc.scheduled_slots("SAME_CHANNEL", key="KEY_A")
    b = bc.scheduled_slots("SAME_CHANNEL", key="KEY_B")
    assert a["used"] == 3 and a["free"] == 7
    assert b["used"] == 7 and b["free"] == 3


def test_the_cache_still_caches(fake_buffer):
    """Isolation must not have been bought by disabling the cache.

    The cache exists because a background feeder checking every 15 minutes was
    spending 181 getChannel calls a day against a ~100-call budget.
    """
    bc.get_channel("CHAN", key="KEY_A")
    bc.get_channel("CHAN", key="KEY_A")
    bc.get_channel("CHAN", key="KEY_A")
    assert len(fake_buffer) == 1


def test_no_raw_key_is_ever_used_as_a_cache_key(fake_buffer):
    """Cache dicts get logged, repr'd and dumped into tracebacks.

    A credential must not ride along, so the key is fingerprinted.
    """
    bc.get_channel("CHAN", key="super-secret-key")
    bc.scheduled_slots("CHAN", key="super-secret-key")
    for cache in (bc._channel_cache, bc._slots_cache):
        for cache_key in cache:
            assert "super-secret-key" not in str(cache_key)


def test_the_fingerprint_is_stable_and_separates_keys():
    assert bc._key_fingerprint("abc") == bc._key_fingerprint("abc")
    assert bc._key_fingerprint("abc") != bc._key_fingerprint("abd")
    assert len(bc._key_fingerprint("abc")) == 12


def test_an_explicit_key_shares_the_bucket_with_the_same_env_key(monkeypatch):
    """key=None means "whatever the env holds", which gql falls back to.

    Fingerprinting them the same way keeps one account in one bucket instead of
    paying for two identical lookups.
    """
    monkeypatch.setenv("BUFFER_API_KEY", "env-key")
    assert bc._key_fingerprint(None) == bc._key_fingerprint("env-key")


def test_the_fingerprint_does_not_raise_when_no_key_is_configured(monkeypatch):
    """A cache lookup must not blow up on an unset variable."""
    monkeypatch.delenv("BUFFER_API_KEY", raising=False)
    assert bc._key_fingerprint(None) == "unset"
