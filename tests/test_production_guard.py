"""Dispatch 18: the production identity guard.

Nothing used to stand between an ad-hoc CLI/module run and acting as the real
LinkedIn account. verify_identity (test_identity_guard.py) guards against being
the WRONG account, live, mid-run. This guards against being the REAL one at
all - before any browser exists.

A profile NAME says nothing about which account it drives: "jeff" and "prod"
can both resolve to production, and an unregistered profile ("t",
"someprofile") resolves to no identity at all. So production is DECLARED
(PRODUCTION_IDENTITY_SLUGS), never inferred from a profile's name, and an
identity that resolves to nothing is refused exactly like a declared
production one - unknown is not the same as known-dev.
"""

import pytest

from linkedin_automation import profile_manager as pm


# ─── check_production_guard: the decision, in isolation ───────────────────────

def test_production_identity_without_flag_refuses(monkeypatch):
    monkeypatch.setattr(pm, "get_identity_slug", lambda n: "jeffwurfel")
    monkeypatch.setenv(pm.PRODUCTION_IDENTITY_SLUGS_ENV, "jeffwurfel")

    with pytest.raises(pm.ProductionAccessRefused) as exc:
        pm.check_production_guard("jeff", allow_production=False)

    detail = str(exc.value)
    assert "jeffwurfel" in detail
    assert "--allow-production" in detail


def test_production_identity_with_flag_proceeds(monkeypatch):
    monkeypatch.setattr(pm, "get_identity_slug", lambda n: "jeffwurfel")
    monkeypatch.setenv(pm.PRODUCTION_IDENTITY_SLUGS_ENV, "jeffwurfel")

    pm.check_production_guard("jeff", allow_production=True)  # does not raise


def test_production_identity_with_env_equivalent_proceeds(monkeypatch):
    monkeypatch.setattr(pm, "get_identity_slug", lambda n: "jeffwurfel")
    monkeypatch.setenv(pm.PRODUCTION_IDENTITY_SLUGS_ENV, "jeffwurfel")
    monkeypatch.setenv(pm.ALLOW_PRODUCTION_ENV, "1")

    pm.check_production_guard("jeff", allow_production=False)  # does not raise


@pytest.mark.parametrize("value", ["1", "true", "True", "yes", "YES"])
def test_env_equivalent_accepts_common_truthy_spellings(monkeypatch, value):
    monkeypatch.setattr(pm, "get_identity_slug", lambda n: "jeffwurfel")
    monkeypatch.setenv(pm.PRODUCTION_IDENTITY_SLUGS_ENV, "jeffwurfel")
    monkeypatch.setenv(pm.ALLOW_PRODUCTION_ENV, value)

    pm.check_production_guard("jeff", allow_production=False)  # does not raise


def test_dev_identity_proceeds_without_flag(monkeypatch):
    monkeypatch.setattr(pm, "get_identity_slug", lambda n: "some-dev-identity")
    monkeypatch.setenv(pm.PRODUCTION_IDENTITY_SLUGS_ENV, "jeffwurfel")

    pm.check_production_guard("dev", allow_production=False)  # does not raise


def test_an_unresolvable_identity_refuses(monkeypatch):
    monkeypatch.setattr(pm, "get_identity_slug", lambda n: "")
    monkeypatch.setenv(pm.PRODUCTION_IDENTITY_SLUGS_ENV, "jeffwurfel")

    with pytest.raises(pm.ProductionAccessRefused) as exc:
        pm.check_production_guard("someprofile", allow_production=False)
    assert "someprofile" in str(exc.value)


def test_an_unresolvable_identity_refuses_even_with_the_flag(monkeypatch):
    """Unknown is not the same as known-dev (docs/ARCHITECTURE.md §9): the
    flag authorizes a DECLARED production identity, not an unresolved one -
    there is nothing to authorize acting as."""
    monkeypatch.setattr(pm, "get_identity_slug", lambda n: "")
    monkeypatch.setenv(pm.PRODUCTION_IDENTITY_SLUGS_ENV, "jeffwurfel")

    with pytest.raises(pm.ProductionAccessRefused):
        pm.check_production_guard("t", allow_production=True)


def test_case_and_whitespace_do_not_matter_for_the_production_match(monkeypatch):
    monkeypatch.setattr(pm, "get_identity_slug", lambda n: "JeffWurfel")
    monkeypatch.setenv(pm.PRODUCTION_IDENTITY_SLUGS_ENV, " jeffwurfel , someone-else ")

    with pytest.raises(pm.ProductionAccessRefused):
        pm.check_production_guard("jeff", allow_production=False)


def test_production_identity_slugs_is_declared_never_inferred_from_name(monkeypatch):
    """A profile named 'prod' with a dev identity is NOT production; a profile
    named 'jeff' with the declared production identity IS - the guard reads
    only the identity, never the name."""
    monkeypatch.setattr(pm, "get_identity_slug", lambda n: "some-dev-identity")
    monkeypatch.setenv(pm.PRODUCTION_IDENTITY_SLUGS_ENV, "jeffwurfel")

    pm.check_production_guard("prod", allow_production=False)  # does not raise


def test_get_production_identity_slugs_parses_and_normalizes(monkeypatch):
    monkeypatch.setenv(pm.PRODUCTION_IDENTITY_SLUGS_ENV, " JeffWurfel, other-slug ,")
    assert pm.get_production_identity_slugs() == {"jeffwurfel", "other-slug"}


def test_get_production_identity_slugs_empty_by_default(monkeypatch):
    monkeypatch.delenv(pm.PRODUCTION_IDENTITY_SLUGS_ENV, raising=False)
    assert pm.get_production_identity_slugs() == set()


# ─── create_driver: the browser factory must never be reached on a refusal ───

class RecordingChrome:
    instances = []

    def __init__(self, *a, **k):
        RecordingChrome.instances.append(self)

    def set_page_load_timeout(self, seconds):
        pass

    def maximize_window(self):
        pass

    def execute_script(self, *a):
        return None


@pytest.fixture
def driver_env(monkeypatch, tmp_path):
    """Every collaborator create_driver needs, EXCEPT the identity guard -
    the browser factory is a spy so a test can assert it was (not) called."""
    RecordingChrome.instances = []
    monkeypatch.setattr(pm.webdriver, "Chrome", RecordingChrome)
    monkeypatch.setattr(pm, "Service", lambda *a, **k: None)
    monkeypatch.setattr(pm, "ChromeDriverManager",
                        lambda: type("M", (), {"install": lambda self: "x"})())
    monkeypatch.setattr(pm, "auto_migrate_from_env", lambda: None)
    monkeypatch.setattr(pm, "get_profile", lambda n: {
        "username": "u", "session_dir": str(tmp_path)})
    monkeypatch.setattr(pm, "session_exists", lambda d: True)
    monkeypatch.setattr(pm, "update_last_used", lambda n: None)
    return tmp_path


def test_production_without_flag_never_constructs_a_browser(monkeypatch, driver_env):
    monkeypatch.setattr(pm, "get_identity_slug", lambda n: "jeffwurfel")
    monkeypatch.setenv(pm.PRODUCTION_IDENTITY_SLUGS_ENV, "jeffwurfel")

    with pytest.raises(pm.ProductionAccessRefused):
        pm.create_driver("jeff")

    assert RecordingChrome.instances == []


def test_production_with_flag_constructs_a_browser(monkeypatch, driver_env):
    monkeypatch.setattr(pm, "get_identity_slug", lambda n: "jeffwurfel")
    monkeypatch.setenv(pm.PRODUCTION_IDENTITY_SLUGS_ENV, "jeffwurfel")

    driver, _profile = pm.create_driver("jeff", allow_production=True)

    assert len(RecordingChrome.instances) == 1
    assert driver is RecordingChrome.instances[0]


def test_dev_identity_constructs_a_browser_without_the_flag(monkeypatch, driver_env):
    monkeypatch.setattr(pm, "get_identity_slug", lambda n: "some-dev-identity")
    monkeypatch.setenv(pm.PRODUCTION_IDENTITY_SLUGS_ENV, "jeffwurfel")

    pm.create_driver("dev")

    assert len(RecordingChrome.instances) == 1


def test_unresolvable_identity_never_constructs_a_browser(monkeypatch, driver_env):
    monkeypatch.setattr(pm, "get_identity_slug", lambda n: "")
    monkeypatch.setenv(pm.PRODUCTION_IDENTITY_SLUGS_ENV, "jeffwurfel")

    with pytest.raises(pm.ProductionAccessRefused):
        pm.create_driver("someprofile")

    assert RecordingChrome.instances == []


def test_refusal_message_names_the_identity_and_the_flag(monkeypatch, driver_env):
    monkeypatch.setattr(pm, "get_identity_slug", lambda n: "jeffwurfel")
    monkeypatch.setenv(pm.PRODUCTION_IDENTITY_SLUGS_ENV, "jeffwurfel")

    with pytest.raises(pm.ProductionAccessRefused) as exc:
        pm.create_driver("jeff")

    detail = str(exc.value)
    assert "jeffwurfel" in detail
    assert "--allow-production" in detail
    assert pm.ALLOW_PRODUCTION_ENV in detail


def test_missing_profile_is_still_a_valueerror_not_a_production_refusal(monkeypatch, driver_env):
    """The production guard runs AFTER the profile-exists check - a typo'd
    profile name must not be swallowed into a confusing guard message."""
    monkeypatch.setattr(pm, "get_profile", lambda n: None)

    with pytest.raises(ValueError):
        pm.create_driver("does-not-exist")

    assert RecordingChrome.instances == []
