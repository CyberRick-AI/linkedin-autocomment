"""The X read path, offline: HTML in, store records out.

No browser, no network, no X session. X exposes every permalink as a plain
anchor, so the whole read path is a pure function over saved DOM — which is
what makes this suite possible at all. The LinkedIn finder cannot be tested
this way because it has to open a menu and read the clipboard to learn a URL.

The fixtures are hand-authored, but the SHAPES in x_timeline_mixed.html were
found by parsing the real scrubbed captures: the quote-tweet nesting, the
/analytics and /photo/1 siblings, the paid ad with no addressable permalink,
and the /i/status/ route. Each is a case that was really there.

NOT covered here, by construction: navigation and the scroll gesture. Those
need a live browser and are the human go/no-go.
"""

import io
import os

import pytest

from linkedin_automation import post_store as ps
from linkedin_automation import x_finder as xf
from linkedin_automation.post_finder import ContentAnalyzer

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(name: str) -> str:
    with io.open(os.path.join(FIXTURE_DIR, name + ".html"), encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def store(tmp_path):
    return ps.PostStore(path=str(tmp_path / "x_posts_db.json"), platform=ps.X)


# ─── The permalink: where X is simpler than LinkedIn ──────────────────────────

def test_the_permalink_comes_straight_off_the_anchor():
    """No menu opened, no clipboard read, no shortlink resolved."""
    posts = xf.parse_timeline(fixture("x_timeline_healthy"))
    assert [p.url for p in posts] == [
        "https://x.com/user1/status/9999999999999990001",
        "https://x.com/user2/status/9999999999999990002",
        "https://x.com/user3/status/9999999999999990003",
    ]


def test_the_analytics_and_photo_siblings_are_not_mistaken_for_the_permalink():
    """Both sit beside the real one in live DOM and point at the same post.

    Either would produce a store key the poster cannot navigate back to, so
    this is a correctness bug that would only surface at posting time.
    """
    posts = xf.parse_timeline(fixture("x_timeline_mixed"))
    urls = [p.url for p in posts]
    assert "https://x.com/user1/status/9999999999999990001" in urls
    assert not any(u.endswith("/analytics") for u in urls)
    assert not any("/photo/" in u for u in urls)


def test_a_permalink_is_normalised_to_an_absolute_url():
    """The DOM carries relative hrefs; the store needs something navigable."""
    for post in xf.parse_timeline(fixture("x_timeline_healthy")):
        assert post.url.startswith("https://x.com/")


def test_an_x_route_is_not_read_as_a_handle():
    """`/i/status/<id>` is addressable but has no account in its URL."""
    posts = {p.url: p for p in xf.parse_timeline(fixture("x_timeline_mixed"))}
    routed = posts["https://x.com/i/status/9999999999999990003"]
    assert routed.author_handle is None
    assert routed.tweet_id == "9999999999999990003"


# ─── The shapes the captures actually contained ───────────────────────────────

def test_a_card_with_no_addressable_permalink_is_skipped():
    """The paid ad in the timeline capture had exactly this shape.

    It is skipped because it cannot be ADDRESSED, which is a fact about the
    DOM. That this also drops an ad is a side effect — nothing here claims to
    classify X ads.
    """
    posts = xf.parse_timeline(fixture("x_timeline_mixed"))
    assert not any("user5" in p.url for p in posts)
    assert not any(p.url.endswith("9999999999999990005") for p in posts)


def test_a_quote_tweet_reads_the_outer_account_not_the_quoted_one():
    """Cards nest: 3 of 5 cards in the timeline capture had two User-Name blocks."""
    posts = {p.url: p for p in xf.parse_timeline(fixture("x_timeline_mixed"))}
    quote = posts["https://x.com/user2/status/9999999999999990002"]
    assert quote.author_handle == "user2"
    assert quote.author_name == "Outer Synthetic Account"
    assert quote.posted_at == "2026-09-17T09:00:00.000Z"


def test_a_repeated_card_is_deduped_within_the_page():
    """X re-renders a card across rows, so counting rows double-counts."""
    html = fixture("x_timeline_mixed")
    assert html.count("9999999999999990001") >= 4, "fixture must repeat the card"
    posts = xf.parse_timeline(html)
    assert [p.url for p in posts].count(
        "https://x.com/user1/status/9999999999999990001") == 1


def test_the_mixed_timeline_yields_exactly_the_addressable_posts():
    posts = xf.parse_timeline(fixture("x_timeline_mixed"))
    assert len(posts) == 3


# ─── Engagement, from the buttons' own aria-labels ────────────────────────────

def test_engagement_counts_come_from_the_aria_labels():
    posts = {p.url: p for p in xf.parse_timeline(fixture("x_timeline_mixed"))}
    first = posts["https://x.com/user1/status/9999999999999990001"]
    assert (first.replies, first.reposts, first.likes) == (4, 6, 48)


def test_the_singular_phrasings_parse():
    """X writes "1 Reply. Reply" and "1 repost. Repost", not "1 Replies"."""
    posts = {p.url: p for p in xf.parse_timeline(fixture("x_timeline_mixed"))}
    quote = posts["https://x.com/user2/status/9999999999999990002"]
    assert (quote.replies, quote.reposts, quote.likes) == (1, 1, 1)


@pytest.mark.parametrize("label,expected", [
    ("4 Replies. Reply", 4),
    ("1 Reply. Reply", 1),
    ("0 reposts. Repost", 0),
    ("2166 Likes. Like", 2166),
    ("19023 Likes. Like", 19023),
    ("1,234 Likes. Like", 1234),
    ("Bookmark", 0),
    (None, 0),
    ("", 0),
])
def test_parse_count_reads_the_leading_number(label, expected):
    assert xf.parse_count(label) == expected


def test_abbreviated_counts_are_provisional_but_do_not_crash():
    """PROVISIONAL: no capture ever held a K/M form — every count was an integer.

    Pinned so the defensive branch is at least defined behaviour rather than an
    accident, and so a future live run that DOES see one has something to
    compare against.
    """
    assert xf.parse_count("2.2K Likes. Like") == 2200
    assert xf.parse_count("1M Likes. Like") == 1_000_000


# ─── Relevance: the shared analyzer, on X's terms ─────────────────────────────

def test_the_x_path_reuses_the_linkedin_analyzer():
    posts = xf.analyze_posts(xf.parse_timeline(fixture("x_timeline_mixed")))
    by_url = {p.url: p for p in posts}
    relevant = by_url["https://x.com/user1/status/9999999999999990001"]
    assert relevant.should_engage is True
    assert "llm" in relevant.keywords_matched
    assert relevant.relevance_score > 0


def test_an_off_topic_tweet_is_not_marked_for_engagement():
    posts = xf.analyze_posts(xf.parse_timeline(fixture("x_timeline_mixed")))
    by_url = {p.url: p for p in posts}
    off_topic = by_url["https://x.com/i/status/9999999999999990003"]
    assert off_topic.should_engage is False
    assert off_topic.keywords_matched == []


def test_linkedins_minimum_length_is_unchanged():
    """The default must stay exactly what it was — 30 characters.

    The X finder passes its own floor instead of moving this, because X's
    register is compressed and a LinkedIn-shaped floor would discard
    substantive posts wholesale.
    """
    analyzer = ContentAnalyzer()
    assert analyzer.MIN_TEXT_LENGTH == 30
    short = "llm stuff"                       # 9 chars: under both floors
    assert analyzer.analyze(short)[0] is False
    # And the default path still refuses a 20-char text, as it always has.
    assert analyzer.analyze("llm agents are neat!")[0] is False


def test_the_x_floor_lets_a_short_tweet_through_that_linkedins_would_drop():
    """The one LinkedIn-shaped assumption in the shared analyzer."""
    analyzer = ContentAnalyzer()
    tweet = "llm agents are neat!"             # 20 chars
    assert len(tweet) < analyzer.MIN_TEXT_LENGTH
    assert len(tweet) >= xf.MIN_TWEET_LENGTH
    assert analyzer.analyze(tweet, min_length=xf.MIN_TWEET_LENGTH)[0] is True


# ─── The store: the platform split, used as designed ──────────────────────────

def test_harvest_writes_to_the_x_store_with_permalinks(store):
    summary = xf.harvest_html(fixture("x_timeline_mixed"), store)
    assert summary["parsed"] == 3
    keys = set(store.posts)
    assert "https://x.com/user1/status/9999999999999990001" in keys
    for rec in store.posts.values():
        assert rec["url"].startswith("https://x.com/")


def test_a_relevant_tweet_lands_new_and_an_irrelevant_one_lands_trash(store):
    xf.harvest_html(fixture("x_timeline_mixed"), store)
    relevant = store.get("https://x.com/user1/status/9999999999999990001")
    off_topic = store.get("https://x.com/i/status/9999999999999990003")
    assert relevant["status"] == ps.NEW
    assert off_topic["status"] == ps.TRASH
    assert off_topic["trash_reason"] == ps.REASON_LOW_QUALITY


def test_re_harvesting_the_same_timeline_does_not_duplicate(store):
    first = xf.harvest_html(fixture("x_timeline_mixed"), store)
    before = dict(store.counts())
    second = xf.harvest_html(fixture("x_timeline_mixed"), store)
    assert first["parsed"] == second["parsed"] == 3
    assert second["existing"] == 3 and second["new"] == 0
    assert store.counts() == before
    assert len(store.posts) == 3


def test_a_re_harvest_never_downgrades_a_post_already_acted_on(store):
    """The store's protection, exercised through the X path.

    A second pass over the same timeline must not drag a COMMENTED post back to
    NEW — the failure that would make the poster comment twice.
    """
    url = "https://x.com/user1/status/9999999999999990001"
    xf.harvest_html(fixture("x_timeline_mixed"), store)
    store.mark_generated(url, "a draft reply")
    store.posts[url]["status"] = ps.COMMENTED
    xf.harvest_html(fixture("x_timeline_mixed"), store)
    assert store.get(url)["status"] == ps.COMMENTED


def test_the_x_store_is_a_different_file_from_linkedins():
    """Two platforms are two files — the isolation the store was split for.

    The data root is already redirected to a tmp dir by conftest's autouse
    `_isolate_data_root`, so this reads the real path logic without touching
    the real data directory.
    """
    x_path = ps.PostStore._default_path("someprofile", ps.X)
    li_path = ps.PostStore._default_path("someprofile", ps.LINKEDIN)
    assert x_path != li_path
    assert os.path.join("someprofile", "x") in x_path
    # LinkedIn's path is unchanged: no migration, the working side does not move.
    assert li_path.endswith(os.path.join("someprofile", "posts_db.json"))


def test_open_store_uses_the_x_platform(tmp_path):
    s = xf.open_store(path=str(tmp_path / "db.json"))
    assert s.platform == ps.X


# ─── Search mode: honest about what is not verified ───────────────────────────

def test_search_parsing_is_the_same_parser_not_a_second_one():
    """The search page renders the same cards, proved by its probe inventory.

    PROVISIONAL: the search dump was blocked by the scrubber at capture time and
    never saved, so there is no real search DOM to test against. This asserts
    only that search does not get a divergent implementation — the live
    confirmation is still outstanding.
    """
    html = fixture("x_search_healthy")
    assert xf.parse_search_results(html) == xf.parse_timeline(html)


def test_the_search_fixture_still_yields_addressable_posts():
    posts = xf.parse_search_results(fixture("x_search_healthy"))
    assert len(posts) == 2
    assert all(p.url.startswith("https://x.com/") for p in posts)


# ─── A status page is just cards ──────────────────────────────────────────────

def test_a_status_page_parses_with_the_same_parser():
    """The subject post and its replies are all cards, so nothing special."""
    posts = xf.parse_timeline(fixture("x_status_healthy"))
    assert len(posts) == 2
    assert posts[0].url == "https://x.com/user1/status/9999999999999990001"


# ─── The live half: only what can honestly be checked offline ─────────────────

class _FakeDriver:
    """Serves a different page per read, so scroll accumulation is observable."""

    def __init__(self, pages):
        self._pages = list(pages)
        self.gets = []
        self.scripts = []

    def get(self, url):
        self.gets.append(url)

    def execute_script(self, script):
        self.scripts.append(script)
        if len(self._pages) > 1:
            self._pages.pop(0)

    @property
    def page_source(self):
        return self._pages[0]


def test_the_finder_reads_after_every_scroll_because_x_virtualizes():
    """Rows LEAVE the DOM as they scroll out.

    The captures went 9 -> 18 permalinks across three scrolls. A finder that
    scrolls first and reads once would silently miss everything that scrolled
    past, and would look like it was working.
    """
    driver = _FakeDriver([fixture("x_timeline_healthy"),
                          fixture("x_timeline_mixed")])
    finder = xf.XTimelineFinder(driver)
    posts = finder.collect(scrolls=1)

    urls = {p.url for p in posts}
    # Page one only.
    assert "https://x.com/user3/status/9999999999999990003" in urls
    # Page two only — reachable solely because it read again after scrolling.
    assert "https://x.com/i/status/9999999999999990003" in urls
    assert len(driver.scripts) == 1


def test_the_finder_navigates_to_the_timeline():
    driver = _FakeDriver([fixture("x_timeline_healthy")])
    xf.XTimelineFinder(driver).collect(scrolls=0)
    assert driver.gets == ["https://x.com/home"]


def test_a_post_seen_twice_across_scrolls_is_kept_once(store):
    driver = _FakeDriver([fixture("x_timeline_mixed"),
                          fixture("x_timeline_mixed")])
    summary = xf.XTimelineFinder(driver).run(store=store, scrolls=2)
    assert summary["parsed"] == 3
    assert len(store.posts) == 3
