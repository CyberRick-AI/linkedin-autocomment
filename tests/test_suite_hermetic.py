"""Guards on the suite's own hermeticity.

The suite has always *claimed* to be offline. These make the claim checkable.

Adopted and adapted from the student fork (.dev/AUDIT_fork_remainder.md §4).
The problem it names is real here, and was verified against this repo before
adopting: ``load_dotenv()`` runs at import time in several modules, so importing
the package under test loaded the developer's real ``.env`` — a live, funded
``sk-proj-…`` key — into ``os.environ`` for every test in the run.

Nothing spends it today, because the OpenAI client is mocked everywhere. That is
protection by coincidence: every guard is somewhere else, and one missed mock
reaches a real vendor and bills a real account. The ``_dummy_api_keys`` autouse
fixture in conftest closes it; these tests are what stop it silently reopening.
"""

import os
import re

import pytest

from conftest import API_KEY_ENV_VARS, DUMMY_API_KEY

# A vendor key, loosely: a recognised prefix followed by a long opaque token.
# Deliberately permissive — this asserts nothing *resembling* a credential is
# present, it does not parse one.
REAL_LOOKING_KEY = re.compile(r"^(sk-proj-|sk-ant-|sk-|xai-|gsk_)[A-Za-z0-9_\-]{20,}$")


def test_every_api_key_env_var_is_the_dummy():
    """Deleting the conftest fixture must fail this, on any machine."""
    for var in API_KEY_ENV_VARS:
        assert os.environ.get(var) == DUMMY_API_KEY, (
            f"{var} is not pinned to the test dummy")


def test_no_environment_variable_holds_a_real_looking_key():
    """Broader on purpose: catches a key arriving under a name we never declared.

    The check above asserts the fixture ran. This asserts the *outcome*, so a
    credential loaded from .env under some other name is still caught.
    """
    offenders = [name for name, value in os.environ.items()
                 if value and REAL_LOOKING_KEY.match(value)]
    assert offenders == [], (
        f"real-looking credentials visible to the suite: {offenders}")


def test_the_dummy_is_not_itself_real_looking():
    """Guards the guard: a dummy matching the pattern would mask everything."""
    assert not REAL_LOOKING_KEY.match(DUMMY_API_KEY)


def test_importing_the_package_does_not_reintroduce_a_real_key():
    """load_dotenv() at import time is the exact mechanism that leaked it.

    Importing a module mid-test must not overwrite the pinned dummy.
    """
    import importlib

    from linkedin_automation import comment_generator
    importlib.reload(comment_generator)

    for var in API_KEY_ENV_VARS:
        assert os.environ.get(var) == DUMMY_API_KEY, (
            f"{var} was overwritten by a module import — load_dotenv() is "
            f"clobbering the test environment again")


@pytest.mark.parametrize("var", API_KEY_ENV_VARS)
def test_the_suite_does_not_depend_on_an_ambient_key(var, monkeypatch):
    """Removing a key entirely must not change what the pinned value would be.

    Encodes the rule: the suite supplies its own environment. If a test only
    passes because the developer had something exported, the baseline it reports
    cannot be reproduced on a clean checkout or in CI.
    """
    monkeypatch.delenv(var, raising=False)
    assert os.environ.get(var) is None
    # The fixture pins it again for the next test; nothing here leaks forward.


# ─── Buffer: the same guarantee, for the other vendor ────────────────────────
#
# The key guards above stop a credential being SPENT. These stop a request being
# SENT. Both were protection by coincidence until something checked.
#
# Eight tests in test_scheduled_ui.py really did reach api.buffer.com, through
# the queue endpoint's poll-on-render reconcile, against the real configured
# channel. Nothing failed, because `_reconcile` catches every exception so a
# Buffer outage renders last-known state rather than an error page. The only
# symptom was a line in api_usage.jsonl.

def test_the_buffer_transport_is_replaced_for_every_test():
    """The conftest guard must be in place, on any machine.

    Deleting the fixture has to fail this, the same way deleting
    `_dummy_api_keys` fails the key guards above.
    """
    from linkedin_automation import buffer_client

    with pytest.raises(AssertionError) as exc:
        buffer_client.requests.post("https://api.buffer.com/graphql", json={})
    assert "hermetic" in str(exc.value)


def test_a_buffer_call_without_an_injected_session_is_refused():
    """End to end through gql, which is what every Buffer helper funnels into."""
    from linkedin_automation import buffer_client

    with pytest.raises(AssertionError):
        buffer_client.gql("query { __typename }", key="dummy", label="probe")


def test_an_injected_session_still_works():
    """The guard must not break the tests that drive gql with a fake session.

    test_buffer_client.py exercises status handling, retries and the usage log
    that way and never touches the network; a guard that broke those would be
    replacing real coverage with a false alarm.
    """
    from linkedin_automation import buffer_client

    class _Session:
        status_code = 200

        def post(self, url, **kwargs):
            return self

        @staticmethod
        def json():
            return {"data": {"__typename": "Query"}}

    body = buffer_client.gql("query { __typename }", key="dummy",
                             label="probe", session=_Session())
    assert body["data"]["__typename"] == "Query"


def test_the_real_api_usage_log_is_never_written_by_a_test():
    """`gql` logs BEFORE it sends, so even a REFUSED call appended a line.

    api_usage.jsonl is a tracked project file and a record of real spend. A
    test writing to it is both pollution and a misleading record of money that
    was never spent.
    """
    from linkedin_automation import buffer_client

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    real = os.path.join(repo_root, "api_usage.jsonl")
    assert os.path.abspath(buffer_client.USAGE_LOG) != os.path.abspath(real), (
        "USAGE_LOG still points at the real api_usage.jsonl")
