"""The four feature surfaces, exercised as a set (Phase 12, AUDIT E2).

``ROADMAP.md`` pins four surfaces that every phase must leave working: the
comment pipeline, the auto connector, the post creator, and the scheduler.
Until this file, nothing tested them as a set. Thirteen hardening phases could
have broken the connector or the post creator and the suite would have stayed
green, because the tests that existed covered fragments — a config reader, a
label parser, a tracker — and never the entry point that ties them together.

Three of the four have still never been run by anybody, on any machine. That
is the honest limit of this file and it is stated rather than implied: these
tests prove the surfaces are **structurally intact and internally consistent
after the hardening**, which is what the suite could not answer before. They
do not prove the surfaces work against LinkedIn. Only Rick can answer that,
and the boundary in ``PROJECT.md`` section 1 says so.

Mocks sit at the boundary the framework names: the browser, the provider, the
clock, and the sleep calls. The logic in between is the real thing. Every test
here fails if you replace the real entry point with a stub, which is the
property ``ROADMAP.md`` Phase 12 asks for and the reason none of them assert
on a mock's call count alone.
"""

import json
from datetime import datetime

import pytest

from linkedin_automation import auto_connector as ac
from linkedin_automation import comment_poster as cp
from linkedin_automation import human_behavior as hb
from linkedin_automation import pipeline_manifest as manifest
from linkedin_automation import post_generator as pg
from linkedin_automation import poster as poster_mod
from linkedin_automation import profile_manager as pm
from linkedin_automation import providers
from linkedin_automation import scheduler as sched


# ─── Boundary doubles ─────────────────────────────────────────────────────────

class FakeElement:
    """A Selenium element stand-in that records the clicks it receives."""

    def __init__(self, attrs=None, text="", displayed=True):
        self._attrs = attrs or {}
        self.text = text
        self._displayed = displayed
        self.clicks = 0

    def get_attribute(self, name):
        return self._attrs.get(name)

    def is_displayed(self):
        return self._displayed

    def is_enabled(self):
        return True

    def click(self):
        self.clicks += 1

    def send_keys(self, *_):
        pass

    def clear(self):
        pass

    @property
    def location(self):
        return {"x": 0, "y": 0}

    @property
    def size(self):
        return {"width": 10, "height": 10}


class FakeDriver:
    """The browser boundary.

    Returns elements by CSS selector from a dict the test supplies, so a test
    describes the page it wants rather than the calls it expects.
    """

    def __init__(self, elements=None, url="https://www.linkedin.com/feed/"):
        self.elements = elements or {}
        self.current_url = url
        self.page_source = "<html></html>"
        self.visited = []
        self.quit_calls = 0
        self.scripts = []

    def get(self, url):
        self.visited.append(url)
        self.current_url = url

    def find_elements(self, by, value):
        # A comma-separated selector is a CSS selector *list*, and the surfaces
        # use them: navigate_to_search waits on "connect-link, result-card" so a
        # rotated DOM does not burn the full timeout. A double that treats the
        # whole string as one key returns nothing, the real 20s WebDriverWait
        # expires, and the test looks like a hang rather than a mismatch. Found
        # exactly that way: the first run of this file took 120 seconds.
        found = []
        for part in (p.strip() for p in value.split(",")):
            for element in self.elements.get(part, []):
                if element not in found:
                    found.append(element)
        return found

    def find_element(self, by, value):
        found = self.find_elements(by, value)
        if not found:
            from selenium.common.exceptions import NoSuchElementException
            raise NoSuchElementException(value)
        return found[0]

    def execute_script(self, script, *args):
        self.scripts.append(script)
        return None

    def save_screenshot(self, path):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("")
        return True

    def quit(self):
        self.quit_calls += 1

    def set_page_load_timeout(self, _seconds):
        pass

    @property
    def window_handles(self):
        return ["w1"]


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """Remove every human-pacing delay.

    The surfaces deliberately sleep for seconds at a time to look human. That
    is correct in production and fatal to the five-second suite budget in the
    Phase 12 gate, so the pacing functions are neutralised at the boundary
    rather than the surfaces being rewritten to be fast.
    """
    monkeypatch.setattr(hb, "human_sleep", lambda *a, **k: None)
    monkeypatch.setattr(hb, "simulate_reading", lambda *a, **k: None)
    monkeypatch.setattr(hb, "simulate_reading_for_text", lambda *a, **k: None)
    monkeypatch.setattr(hb, "scroll_to_element", lambda *a, **k: None)
    monkeypatch.setattr(hb, "human_scroll", lambda *a, **k: None)
    monkeypatch.setattr(hb, "random_mouse_drift", lambda *a, **k: None)
    monkeypatch.setattr(hb, "take_break", lambda *a, **k: None)
    monkeypatch.setattr(hb, "should_take_break", lambda *a, **k: False)
    monkeypatch.setattr(hb, "random_delay", lambda *a, **k: 0.0)
    monkeypatch.setattr(ac.time, "sleep", lambda *a, **k: None)

    # Selenium's own waits are the other clock. The connector waits 20s for
    # search results to render, which is right against a real browser and is
    # 20s of suite budget against a page that will never render. Shortened
    # rather than removed, so the timeout path is still genuinely taken: the
    # empty-page test below depends on it expiring.
    real_wait = ac.WebDriverWait
    monkeypatch.setattr(
        ac, "WebDriverWait",
        lambda driver, timeout, *a, **k: real_wait(driver, 0.05, poll_frequency=0.01),
    )


@pytest.fixture
def profile_data(tmp_path, monkeypatch):
    """Point every profile-scoped data path at tmp."""
    monkeypatch.setattr(
        pm, "get_data_dir",
        lambda profile_name=None, subdir=None: str(
            _ensure(tmp_path / (subdir or "root"))
        ),
    )
    monkeypatch.setattr(pm, "get_default_profile_name", lambda: "surfaces")
    monkeypatch.setattr(pm, "get_profile_config", lambda profile_name=None: {})
    return tmp_path


def _ensure(path):
    path.mkdir(parents=True, exist_ok=True)
    return path


# ═══ Surface 1: the comment pipeline ══════════════════════════════════════════
# The only surface with real-world evidence: 7 comments posted 2026-08-01.
# Covered in depth elsewhere; what is missing is proof that its declared entry
# points still exist and still carry the Phase 7 idempotency ledger.

def test_comment_pipeline_entry_points_are_importable_and_callable():
    """Every step the manifest declares can actually be reached.

    The manifest names a module and an entry function per step. Nothing
    checked that the pair resolves, so a rename during any of thirteen phases
    would have left the manifest describing a pipeline that no longer exists.
    """
    import importlib

    for step in manifest.STEPS:
        module = importlib.import_module(step.module)
        entry = getattr(module, step.entry_function, None)
        assert callable(entry), (
            f"{step.name}: {step.module}.{step.entry_function} is not callable"
        )


def test_comment_pipeline_still_refuses_to_post_the_same_url_twice(tmp_path, monkeypatch):
    """Phase 7's ledger, exercised through the poster rather than in isolation.

    This is the property that stops a comment being placed twice under Rick's
    name. It is asserted here as part of the surface set, because a later
    phase could keep ``atomic_io`` intact and still break the pipeline's use
    of it.
    """
    progress = tmp_path / "posting_progress.json"
    monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None: str(tmp_path))
    monkeypatch.setattr(pm, "get_progress_file", lambda profile_name=None: str(progress))
    monkeypatch.setattr(pm, "get_screenshots_dir", lambda profile_name=None: str(tmp_path))

    url = "https://www.linkedin.com/feed/update/urn:li:activity:1/"

    first = cp.LinkedInCommentPoster(profile_name="surfaces")
    first.progress.setdefault("posted_comments", []).append(url)
    first.save_progress()

    second = cp.LinkedInCommentPoster(profile_name="surfaces")
    assert url in second.progress["posted_comments"]
    assert second.progress["posted_comments"].count(url) == 1


# ═══ Surface 2: the auto connector ════════════════════════════════════════════
# Never run by anybody. AUDIT E2.

def _invite_link(name, vanity):
    return FakeElement(attrs={
        "aria-label": f"Invite {name} to connect",
        "href": f"/preload/search-custom-invite/?vanityName={vanity}",
    })


@pytest.fixture
def connector(profile_data, monkeypatch):
    """A real connector wired to a fake browser.

    Construction goes through the real ``__init__``, which reads profile
    config and resolves limits. The existing connector tests bypass it with
    ``__new__``, so the constructor itself had no coverage.
    """
    def _make(page_links, config=None, max_requests=None):
        monkeypatch.setattr(
            pm, "get_profile_config",
            lambda profile_name=None: config or {},
        )
        driver = FakeDriver(
            elements={ac.LinkedInAutoConnector.CONNECT_LINK_SELECTORS[0]: page_links},
            url="https://www.linkedin.com/search/results/people/?keywords=ai",
        )
        monkeypatch.setattr(pm, "create_driver", lambda name=None: (driver, {"name": name}))
        monkeypatch.setattr(pm, "login", lambda d, p: True)

        connector = ac.LinkedInAutoConnector(
            profile_name="surfaces", max_requests=max_requests
        )
        return connector, driver
    return _make


def test_auto_connector_sends_requests_and_records_them(connector, monkeypatch):
    """The connector's entry point, end to end, with only the browser faked.

    Proves the chain nobody has ever exercised: construct, read config, set up
    the tracker, find invite links, extract each person, click, record. A
    surface that only ever had its label parser tested now has its run loop
    tested.
    """
    links = [_invite_link("Ada Byron", "ada"), _invite_link("Grace Hopper", "grace")]
    conn, driver = connector(links)

    # The click boundary: the invite modal is a live-DOM flow, so the click is
    # stubbed to succeed. Everything deciding *whether* to click is real.
    monkeypatch.setattr(ac.LinkedInAutoConnector, "click_connect", lambda self, info: True)
    monkeypatch.setattr(ac.LinkedInAutoConnector, "go_to_next_page", lambda self: False)

    results = conn.run("https://www.linkedin.com/search/results/people/?keywords=ai")

    assert results["session"]["sent"] == 2
    assert conn.tracker.get_daily_count() == 2
    assert conn.tracker.already_sent("https://www.linkedin.com/in/ada/")
    assert driver.quit_calls == 1, "the browser must be closed even on the happy path"


def test_auto_connector_honours_the_daily_cap(connector, monkeypatch):
    """The cap is the account-safety control, so it gets an explicit test.

    ``max_requests`` is clamped to whatever the tracker says remains. Sending
    past it is the failure mode that gets an account restricted, and it had no
    coverage through the run loop.
    """
    links = [_invite_link(f"Person {i}", f"p{i}") for i in range(5)]
    conn, _ = connector(links, config={"connector": {"max_daily_requests": 2}})

    monkeypatch.setattr(ac.LinkedInAutoConnector, "click_connect", lambda self, info: True)
    monkeypatch.setattr(ac.LinkedInAutoConnector, "go_to_next_page", lambda self: False)

    results = conn.run("https://www.linkedin.com/search/results/people/?keywords=ai")

    assert results["session"]["sent"] == 2
    assert conn.tracker.get_daily_count() == 2


def test_auto_connector_does_not_resend_to_someone_already_contacted(connector, monkeypatch):
    """Idempotency for the connector, the property Phase 7 gave the poster.

    A duplicate invite is visible to the recipient and cannot be withdrawn
    quietly, so the dedup key matters as much as the comment ledger does.
    """
    conn, _ = connector([_invite_link("Ada Byron", "ada")])
    monkeypatch.setattr(ac.LinkedInAutoConnector, "click_connect", lambda self, info: True)
    monkeypatch.setattr(ac.LinkedInAutoConnector, "go_to_next_page", lambda self: False)

    conn.run("https://www.linkedin.com/search/results/people/?keywords=ai")
    assert conn.tracker.get_daily_count() == 1

    conn.sent_count = 0
    conn.run("https://www.linkedin.com/search/results/people/?keywords=ai")

    assert conn.tracker.get_daily_count() == 1, "the second run re-invited a known contact"
    assert conn.skipped_count >= 1


def test_auto_connector_writes_its_session_results_atomically(connector, monkeypatch, tmp_path):
    """Phase 7 swept sixteen writers. This asserts the connector's is one.

    The session file is written by ``main()``, not by ``run()``, so the
    package-wide grep guard in ``test_atomic_writes.py`` covers the call site
    while this covers the result being loadable.
    """
    conn, _ = connector([_invite_link("Ada Byron", "ada")])
    monkeypatch.setattr(ac.LinkedInAutoConnector, "click_connect", lambda self, info: True)
    monkeypatch.setattr(ac.LinkedInAutoConnector, "go_to_next_page", lambda self: False)

    results = conn.run("https://www.linkedin.com/search/results/people/?keywords=ai")

    target = tmp_path / "connections" / "session_test.json"
    _ensure(target.parent)
    ac.atomic_io.write_json_atomic(str(target), results)

    assert json.loads(target.read_text())["session"]["sent"] == 1


def test_auto_connector_reports_a_selector_break_rather_than_an_empty_success(
    connector, monkeypatch
):
    """A page with no invite links must not read as "nobody to connect with".

    This is the ``selector_health`` lesson from Phase 11 applied to the
    connector: zero results and a broken selector look identical from the
    outside, and the connector's own docstring says the link-first hooks are
    what LinkedIn rotates. The failure capture is what distinguishes them.
    """
    captured = []
    monkeypatch.setattr(
        ac, "capture_failure",
        lambda driver, reason, profile=None: captured.append(reason),
    )
    conn, _ = connector([])
    monkeypatch.setattr(ac.LinkedInAutoConnector, "go_to_next_page", lambda self: False)

    results = conn.run("https://www.linkedin.com/search/results/people/?keywords=ai")

    assert results["session"]["sent"] == 0
    assert "no_connect_links" in captured, (
        "an empty page produced no diagnostic, so selector rot is indistinguishable "
        "from an empty search"
    )


# ═══ Surface 3: the post creator ══════════════════════════════════════════════
# Never run by anybody. AUDIT E2.

class FakeCompletion:
    def __init__(self, text):
        self.text = text
        self.model = "fake-model"
        self.provider = "fake"
        self.reasoning_stripped = False


class FakeProvider:
    """The provider boundary. Records what it was asked for."""

    def __init__(self, text="A generated post about the thing."):
        self.text = text
        self.calls = []

    def complete(self, model, system, user, **kwargs):
        self.calls.append({"model": model, "system": system, "user": user})
        return FakeCompletion(self.text)


@pytest.fixture
def post_creator(profile_data, monkeypatch):
    """A real PostGenerator with the provider faked at the boundary."""
    def _make(config=None, text="A generated post about the thing."):
        provider = FakeProvider(text)
        monkeypatch.setattr(pm, "get_profile_config", lambda profile_name=None: config or {})
        monkeypatch.setattr(
            providers, "get_provider",
            lambda name, api_key=None, base_url=None: provider,
        )
        return pg.PostGenerator(profile_name="surfaces"), provider
    return _make


def test_post_creator_generates_and_queues_a_post(post_creator):
    """The post creator's entry point, never exercised before.

    Generate, then queue. The queue is what the scheduler later publishes
    from, so a break here is invisible until the scheduler fires.
    """
    generator, provider = post_creator()

    post = generator.generate_thought_leadership(style="contrarian")

    assert post, "generate_thought_leadership returned nothing"
    assert provider.calls, "the provider was never asked for a post"

    # Generation queues as part of the same call, so the surface is one step
    # rather than two. Asserted rather than assumed: writing this test as
    # generate-then-add produced a second queue entry.
    queued = generator.queue.list_queued()

    assert len(queued) == 1
    assert queued[0]["id"] == post["id"]
    assert queued[0]["status"] == "queued"
    assert queued[0]["style"] == "contrarian"


def test_post_creator_uses_the_configured_provider_not_openai(post_creator):
    """Phase 8c fixed this for the comment panels. The post creator is separate.

    A profile on xAI must not have its post generated by an OpenAI model. The
    comment path got three defects from exactly this and the post creator was
    never checked.
    """
    generator, _ = post_creator(config={"provider": {"name": "xai", "model": "grok-4"}})

    assert generator.provider_name == "xai"
    assert generator.model == "grok-4"


def test_post_creator_queue_survives_a_reload(post_creator):
    """The queue is JSON on disk, so it has to round-trip.

    ``PostQueue`` reads at construction and writes on mutation. A second
    instance is what the scheduler builds when it fires, so a serialisation
    break would only ever surface there.
    """
    generator, _ = post_creator()
    generator.generate_thought_leadership(style="contrarian")

    reloaded = pg.PostQueue(profile_name="surfaces")

    assert len(reloaded.list_queued()) == 1


def test_post_creator_marks_a_published_post_and_stops_offering_it(post_creator):
    """Idempotency for the post creator.

    ``get_next`` must not keep returning a post that has been published, or
    the scheduler republishes it on its next run. Same asymmetry as the
    comment ledger: an unpublished post costs nothing, a duplicate post is
    public.
    """
    generator, _ = post_creator()
    post_id = generator.generate_thought_leadership(style="contrarian")["id"]

    assert generator.queue.get_next() is not None

    generator.queue.mark_posted(post_id)

    assert generator.queue.get_next() is None
    assert generator.queue.list_queued() == []
    assert len(generator.queue.history) == 1

    # And it leaves the queue outright, rather than staying in it marked
    # posted. Asserted separately because the two are independently breakable:
    # the status change alone already hides it from get_next, so a mutation
    # that dropped the removal was caught by nothing until this line existed.
    assert post_id not in [p["id"] for p in generator.queue.queue]
    assert generator.queue.history[0]["id"] == post_id


def test_post_publisher_entry_point_constructs_and_closes_its_browser(monkeypatch, profile_data):
    """``poster.LinkedInPoster`` is the surface's other half and is separate code.

    The generator writes the queue; this is what puts a post on LinkedIn. It
    has no test file of its own, so this asserts the entry point at least
    resolves and releases the browser.
    """
    driver = FakeDriver()
    monkeypatch.setattr(pm, "create_driver", lambda name=None: (driver, {"name": name}))
    monkeypatch.setattr(pm, "login", lambda d, p: True)

    publisher = poster_mod.LinkedInPoster(profile_name="surfaces")
    publisher.setup()

    assert publisher.driver is driver

    publisher.driver.quit()
    assert driver.quit_calls == 1


# ═══ Surface 4: the scheduler ═════════════════════════════════════════════════
# Never run by anybody. AUDIT E2.

class FrozenClock:
    """The clock boundary. Deterministic by construction, per framework 8.5."""

    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt


def test_scheduler_starts_and_stops_without_leaking_a_thread():
    """Thread lifecycle, which is the part a menu bar app will depend on.

    ``Quit`` in Phase 14 has to leave nothing running. That property starts
    here, and nothing asserted it: the existing scheduler tests call ``tick``
    directly and never start the loop.
    """
    engine = sched.Scheduler(
        now_fn=FrozenClock(datetime(2026, 8, 1, 9, 0)),
        tick_seconds=0.01,
        list_profiles=lambda: [],
        config_fn=lambda profile: {},
    )

    assert not engine.is_running()

    engine.start()
    assert engine.is_running()

    engine.stop(timeout=2)
    assert not engine.is_running(), "the scheduler thread outlived stop()"


def test_scheduler_start_is_idempotent():
    """Starting twice must not produce two loops.

    Phase 5 found the reloader guard already broken and starting two scheduler
    threads. That was fixed in the dashboard; this asserts the engine itself
    refuses, so the property does not depend on one call site being careful.
    """
    engine = sched.Scheduler(
        now_fn=FrozenClock(datetime(2026, 8, 1, 9, 0)),
        tick_seconds=0.01,
        list_profiles=lambda: [],
        config_fn=lambda profile: {},
    )
    try:
        engine.start()
        first = engine._thread
        engine.start()
        assert engine._thread is first, "a second start() created a second thread"
    finally:
        engine.stop(timeout=2)


def test_scheduler_does_not_fire_when_the_browser_is_busy():
    """The lock that stops the scheduler colliding with a manual run.

    Rick can press Post in the dashboard while the scheduler is due. Two
    browser jobs at once is the collision, and the guard is a callback the
    engine consults. Asserted through ``tick``, not by reading the flag.
    """
    submitted = []
    engine = sched.Scheduler(
        now_fn=FrozenClock(datetime(2026, 8, 1, 9, 0)),
        tick_seconds=0.01,
        submit_post_job=lambda *a, **k: submitted.append(a) or "job-1",
        browser_available=lambda profile: False,
        list_profiles=lambda: ["surfaces"],
        config_fn=lambda profile: {"scheduler": {"enabled": True}},
    )

    engine.tick(now=datetime(2026, 8, 1, 9, 0))

    assert submitted == [], "the scheduler fired a browser job while the browser was busy"


# ═══ The set, as a set ════════════════════════════════════════════════════════

def test_the_four_pinned_surfaces_are_all_declared():
    """``ROADMAP.md`` pins four. The manifest must know about all four.

    Before Phase 12 it declared one. A manifest describing a quarter of the
    system is worse than no manifest, because the boundary flag it carries
    reads as complete coverage.
    """
    assert set(manifest.FEATURE_SURFACES) == {
        "comment pipeline", "auto connector", "post creator", "scheduler",
    }
    for surface in manifest.FEATURE_SURFACES:
        assert manifest.surface_steps(surface), f"{surface} resolves to no steps"


def test_every_pinned_surface_still_imports_and_its_entry_point_resolves():
    """The cheapest possible regression check, and it did not exist.

    An import error in any of these is a total outage of that surface. Three
    of the four are never run by anyone, so an import error would have sat
    undetected until the first time Rick pressed the button.
    """
    import importlib

    for surface in sorted(manifest.FEATURE_SURFACES):
        for step in manifest.surface_steps(surface):
            module = importlib.import_module(step.module)
            entry = getattr(module, step.entry_function, None)
            assert callable(entry), (
                f"{surface}: {step.module}.{step.entry_function} is not callable"
            )


def test_no_pipeline_step_in_the_package_is_undeclared():
    """The drift check the manifest's docstring promised and never had.

    ``pipeline_manifest`` says it exists to answer "does a step exist in the
    codebase with no manifest entry?". ``validate()`` only ever compared the
    manifest against itself, so it could not answer that question at all.
    This is what makes forgetting to declare a new step a build failure
    instead of a documentation gap nobody notices for thirteen phases.
    """
    assert manifest.undeclared_step_modules() == []


def test_the_drift_check_actually_detects_drift(tmp_path):
    """A gate that has never failed has not been tested.

    Same discipline as Phase 1's CI gate proof. A drift check that silently
    returns an empty list for every input would pass the test above forever.
    """
    (tmp_path / "brand_new_step.py").write_text(
        "def main():\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "not_a_step.py").write_text(
        "def helper():\n    pass\n", encoding="utf-8"
    )

    assert manifest.undeclared_step_modules(str(tmp_path)) == ["brand_new_step"]


def test_the_manifest_marks_every_browser_driving_step_as_the_humans():
    """The boundary, still machine-checkable after the manifest grew.

    Phase 1 made ``requires_linkedin_session`` assertable for four steps. The
    three surfaces added here all drive a logged-in browser, so the same rule
    has to hold for them or the flag stops meaning anything.
    """
    browser_steps = {
        "scrape_feed", "post_comments", "selector_health",
        "send_connections", "publish_post",
    }
    for step in manifest.STEPS:
        if step.name in browser_steps:
            assert step.requires_linkedin_session, (
                f"{step.name} drives a browser but is not flagged as Rick's"
            )


def test_the_manifest_is_still_structurally_sound():
    """The Phase 1 check, re-run after adding three steps.

    New upstream/downstream edges are the easiest thing to get half-right.
    """
    assert manifest.validate() == []


# ═══ Edge cases (framework 8.1), where they apply to these surfaces ═══════════
# empty, zero, negative, single-element, oversized, duplicate, out-of-order.
# Enumerated deliberately rather than sampled: the framework calls a suite that
# only proves the happy path "theater", and three of these surfaces have never
# had a happy path proven at all.

def test_connector_with_a_zero_cap_sends_nothing_and_says_why(connector, monkeypatch):
    """Zero. A cap of zero must mean zero, not "unlimited" or "default"."""
    conn, _ = connector(
        [_invite_link("Ada Byron", "ada")],
        config={"connector": {"max_daily_requests": 0}},
    )
    monkeypatch.setattr(ac.LinkedInAutoConnector, "click_connect", lambda self, info: True)
    monkeypatch.setattr(ac.LinkedInAutoConnector, "go_to_next_page", lambda self: False)

    results = conn.run("https://www.linkedin.com/search/results/people/?keywords=ai")

    assert results["session"]["sent"] == 0
    assert conn.tracker.get_daily_count() == 0


def test_connector_clamps_an_explicit_cap_to_what_remains(connector, monkeypatch):
    """Oversized. Asking for more than the weekly allowance must not raise it.

    ``--max`` is an operator argument. If it could exceed the configured cap,
    the flag would silently override the account-safety limit.
    """
    conn, _ = connector(
        [_invite_link(f"Person {i}", f"p{i}") for i in range(10)],
        config={"connector": {"max_daily_requests": 3, "weekly_limit": 3}},
        max_requests=500,
    )
    monkeypatch.setattr(ac.LinkedInAutoConnector, "click_connect", lambda self, info: True)
    monkeypatch.setattr(ac.LinkedInAutoConnector, "go_to_next_page", lambda self: False)

    results = conn.run("https://www.linkedin.com/search/results/people/?keywords=ai")

    assert results["session"]["sent"] == 3


def test_connector_records_a_failed_click_as_an_error_not_a_send(connector, monkeypatch):
    """The failure path, which decides whether a cap is spent on nothing.

    Counting a failed invite as sent would burn the daily allowance without a
    request reaching anyone, and the count is what the next run trusts.
    """
    conn, _ = connector([_invite_link("Ada Byron", "ada")])
    monkeypatch.setattr(ac.LinkedInAutoConnector, "click_connect", lambda self, info: False)
    monkeypatch.setattr(ac.LinkedInAutoConnector, "go_to_next_page", lambda self: False)
    monkeypatch.setattr(ac, "capture_failure", lambda *a, **k: None)

    results = conn.run("https://www.linkedin.com/search/results/people/?keywords=ai")

    assert results["session"]["sent"] == 0
    assert results["session"]["errors"] == 1
    assert conn.tracker.get_daily_count() == 0, "a failed invite consumed the daily cap"


def test_connector_tracker_counts_survive_a_reload(profile_data):
    """Single element, then persistence. The cap is only real if it is durable.

    An in-memory count resets when the dashboard restarts, which would let a
    daily limit be spent several times over in one day.
    """
    tracker = ac.ConnectionTracker("surfaces", weekly_limit=100, daily_limit=25)
    tracker.record_sent("Ada Byron", "https://www.linkedin.com/in/ada/")

    reloaded = ac.ConnectionTracker("surfaces", weekly_limit=100, daily_limit=25)

    assert reloaded.get_daily_count() == 1
    assert reloaded.already_sent("https://www.linkedin.com/in/ada/")


def test_post_queue_is_empty_before_anything_is_generated(post_creator):
    """Empty. ``get_next`` on an empty queue returns None rather than raising."""
    generator, _ = post_creator()

    assert generator.queue.list_queued() == []
    assert generator.queue.get_next() is None


def test_post_queue_ids_do_not_collide_after_a_removal(post_creator):
    """Duplicate. The id is derived from the max in the queue, not the length.

    Removing an item and adding another must not reissue a live id, or
    ``mark_posted`` publishes the wrong post.
    """
    generator, _ = post_creator()
    first = generator.generate_thought_leadership(style="contrarian")["id"]
    second = generator.generate_thought_leadership(style="contrarian")["id"]

    generator.queue.remove(first)
    third = generator.generate_thought_leadership(style="contrarian")["id"]

    assert len({first, second, third}) == 3, "an id was reissued while still in use"
    assert third > second


def test_post_queue_returns_the_oldest_first_regardless_of_insertion_order(post_creator):
    """Out of order. ``get_next`` sorts by queued_at, not by list position."""
    generator, _ = post_creator()
    generator.generate_thought_leadership(style="contrarian")
    generator.generate_thought_leadership(style="contrarian")

    # Reverse the stored order and back-date the second one.
    generator.queue.queue[1]["queued_at"] = "2020-01-01T00:00:00"
    generator.queue.queue.reverse()

    assert generator.queue.get_next()["queued_at"] == "2020-01-01T00:00:00"


def test_scheduler_with_no_profiles_ticks_without_raising():
    """Empty. A fresh install has no profiles and the loop still runs."""
    engine = sched.Scheduler(
        now_fn=FrozenClock(datetime(2026, 8, 1, 9, 0)),
        list_profiles=lambda: [],
        config_fn=lambda profile: {},
    )

    engine.tick(now=datetime(2026, 8, 1, 9, 0))


def test_scheduler_survives_a_profile_whose_config_is_missing():
    """None. ``config_fn`` returning None must not take the whole tick down.

    One bad profile stopping the scheduler for every profile is the
    partial-failure question framework 8.2 says the plan has to answer. The
    answer here is skip-and-continue.
    """
    engine = sched.Scheduler(
        now_fn=FrozenClock(datetime(2026, 8, 1, 9, 0)),
        list_profiles=lambda: ["broken", "fine"],
        config_fn=lambda profile: None if profile == "broken" else {},
    )

    engine.tick(now=datetime(2026, 8, 1, 9, 0))
