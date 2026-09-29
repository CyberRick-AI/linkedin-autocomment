"""The X read path: turn a timeline into store records.

The LinkedIn finder has to *interact* with the page to learn a post's URL — open
the overflow menu, click Copy link, read the clipboard, resolve the lnkd.in
shortlink. None of that is needed here. X puts every card's permalink in a plain
anchor, so the entire X read path is a **pure function from HTML to posts**:

    parse_timeline(html) -> [XPost]

That is not a convenience. It is why this module can be tested offline against
saved DOM, end to end, with no browser and no X session — the live half
(:class:`XTimelineFinder`) only has to fetch HTML and hand it over.

WHAT IS CONFIRMED AND WHAT IS NOT
=================================

Everything below was checked against the four scrubbed captures in
``data/<profile>/x_spike/`` (gitignored; see .dev/AUDIT_x_harvest_state.md), not
inferred from documentation.

Confirmed against saved DOM:

* the card hooks, the action-row hooks, and the permalink anchor
  (:mod:`x_selectors`, all registered in :mod:`selector_health`)
* **engagement counts live in the buttons' own aria-labels** — ``"4 Replies.
  Reply"``, ``"6 reposts. Repost"``, ``"2166 Likes. Like"``
* **cards nest.** A quote-tweet card carries TWO ``User-Name`` blocks and two
  avatars. The outer post is the FIRST of each, which is why every lookup here
  takes the first match inside the card rather than assuming one.
* **some cards carry no addressable permalink.** In the timeline capture exactly
  one did: a paid ad (``utm_medium=paid_social_media``) with no ``<time>`` and
  only the ``/analytics`` variant of a status href.
* ``/i/status/<id>`` occurs — ``i`` is one of X's own routes, not a handle.

NOT confirmed, and marked ``PROVISIONAL`` where it appears below:

* **display-name extraction.** The scrubber replaces every text node with block
  characters, so the captures prove the *structure* of ``User-Name`` but cannot
  validate splitting a display name off the handle. The handle itself is taken
  from the permalink instead, which is confirmed.
* **abbreviated counts** (``2.2K``). Every aria-label in the captures held a
  plain integer, up to ``19023``. The K/M branch is written defensively and has
  never seen real input.
* **search results.** The search page renders the same cards (proved by its
  probe's testid inventory), so ``parse_timeline`` should serve it unchanged —
  but the search dump was BLOCKED by the scrubber at capture time and never
  saved, so there is no saved search DOM to test against. See
  :func:`parse_search_results`.

Nothing here classifies X ads. A card with no canonical permalink is skipped
because it cannot be addressed, which is a fact about the DOM; that it also
happened to drop the one ad in the capture is a side effect, not a claim.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import dom_probe
from . import post_store as ps
from . import x_selectors as xs
from .post_finder import ContentAnalyzer, PostQuality  # noqa: F401  (re-exported)

# ─── X's own first-path-segments, which are routes and not handles ────────────
#
# Mirrors tools/x_dump.py's _NON_PROFILE_PATHS. A permalink under one of these
# is still a real, addressable post (``/i/status/<id>`` appears in the scrolled
# capture) — it just has no handle to read off the URL.
X_ROUTES = frozenset({
    "i", "home", "search", "explore", "notifications", "messages", "settings",
})

X_HOST = "https://x.com"

#: ``/<handle>/status/<id>``, with or without the host. Anchored so the
#: ``/analytics`` and ``/photo/1`` variants do NOT match — those are siblings of
#: the canonical permalink, and treating one as the post's URL would write a
#: store key that no poster can navigate back to.
_PERMALINK_RE = re.compile(
    r"^(?:https?://(?:www\.)?(?:x|twitter)\.com)?"
    r"/(?P<handle>[A-Za-z0-9_]{1,15})/status/(?P<id>\d+)/?$")

#: The leading count in an action button's aria-label: "4 Replies. Reply".
_ARIA_COUNT_RE = re.compile(r"^\s*([\d][\d,\.]*)\s*([KM])?\b", re.IGNORECASE)

#: How short a tweet may be and still be worth analysing.
#:
#: The shared :class:`ContentAnalyzer` floors LinkedIn at 30 characters, which is
#: a LinkedIn-shaped assumption: X's whole register is compressed (see
#: ``platform_policy``'s character-counted length unit), and a 30-char floor
#: would discard substantive short posts wholesale. 15 keeps one-line replies and
#: link-drops out while letting a real sentence through.
MIN_TWEET_LENGTH = 15


@dataclass
class XPost:
    """One tweet, as read off the DOM.

    The X counterpart of :class:`~linkedin_automation.post_finder.LinkedInPost`,
    deliberately NOT a subclass: the fields that differ (a handle instead of a
    profile URL, reposts instead of reshares, no ``activity_urn``) are exactly
    the ones a shared base class would have had to fudge.
    """

    url: str = ""
    tweet_id: str = ""
    author_handle: Optional[str] = None
    author_name: str = ""
    text: str = ""
    replies: int = 0
    reposts: int = 0
    likes: int = 0
    posted_at: Optional[str] = None
    should_engage: bool = False
    relevance_score: int = 0
    quality: PostQuality = PostQuality.SKIP
    post_type: str = ""
    keywords_matched: List[str] = field(default_factory=list)

    def get_identifier(self) -> str:
        """The permalink. Unlike LinkedIn there is no hash fallback.

        A card with no permalink never becomes an XPost — see
        :func:`canonical_permalink` — so identity is always a real URL, and the
        store's URL-keyed dedup works without a content hash standing in.
        """
        return self.url

    def to_store_dict(self) -> Dict:
        """Project onto the keys :meth:`PostStore.upsert_scraped` reads."""
        return {
            "url": self.url,
            "author_name": self.author_name or (
                "@" + self.author_handle if self.author_handle else "Unknown"),
            "text": self.text,
            "post_type": self.post_type,
            "relevance_score": self.relevance_score,
        }


# ─── Pure parsing (no browser, no network) ────────────────────────────────────

def _first(node, selectors) -> Optional[dom_probe.Node]:
    """First element inside ``node`` matching any of ``selectors``.

    FIRST, not only. Cards nest: a quote-tweet carries the quoted account's
    ``User-Name`` and avatar as well as its own, and the outer post's are first
    in document order.
    """
    for sel in selectors:
        found = dom_probe.select_css(node, sel)
        if found:
            return found[0]
    return None


def canonical_permalink(card) -> Optional[str]:
    """The card's addressable URL, or None if it has none.

    Every card's permalink is a plain ``<a href>`` — no menu to open, no
    clipboard to read. Two siblings must be rejected:

    * ``/<handle>/status/<id>/analytics`` — the author's own analytics view
    * ``/<handle>/status/<id>/photo/1`` — a media sub-route

    Both point at the same post but are not the post's URL, and either would
    become a store key that the poster cannot navigate back to.

    Returns None when the card carries no canonical permalink at all. That is a
    real case: the timeline capture held one such card, a paid ad. A caller
    cannot address such a post, so it is not scraped.
    """
    for anchor in dom_probe.select_css(card, "a"):
        href = (anchor.attrs.get("href") or "").strip()
        if "/status/" not in href:
            continue
        m = _PERMALINK_RE.match(href.split("?")[0].split("#")[0])
        if m:
            return "%s/%s/status/%s" % (X_HOST, m.group("handle"), m.group("id"))
    return None


def _split_permalink(url: str) -> Tuple[Optional[str], str]:
    """``(handle, tweet_id)`` from a canonical permalink.

    The handle is None for X's own routes (``/i/status/<id>``), which is a real
    shape in the captures — the post is addressable, it just has no account in
    its URL.
    """
    m = _PERMALINK_RE.match(url)
    if not m:
        return None, ""
    handle = m.group("handle")
    return (None if handle.lower() in X_ROUTES else handle), m.group("id")


def parse_count(aria_label: Optional[str]) -> int:
    """The leading number in an action button's aria-label.

    ``"4 Replies. Reply"`` -> 4, ``"1 Reply. Reply"`` -> 1, ``"Bookmark"`` -> 0.

    PROVISIONAL: the ``K``/``M`` branch. Every count in the captures was a plain
    integer (the largest was 19023), so abbreviated forms have never been seen
    from X and this handling is defensive only.
    """
    if not aria_label:
        return 0
    m = _ARIA_COUNT_RE.match(aria_label)
    if not m:
        return 0
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0
    suffix = (m.group(2) or "").upper()
    if suffix == "K":
        value *= 1_000
    elif suffix == "M":
        value *= 1_000_000
    return int(value)


def _engagement(card) -> Tuple[int, int, int]:
    """``(replies, reposts, likes)`` read from the action row's aria-labels."""
    out = []
    for selectors in (xs.REPLY_BUTTON_SELECTORS,
                      xs.RETWEET_BUTTON_SELECTORS,
                      xs.LIKE_BUTTON_SELECTORS):
        node = _first(card, selectors)
        out.append(parse_count(node.attrs.get("aria-label") if node else None))
    return out[0], out[1], out[2]


def _author_name(card) -> str:
    """The display name from the card's first ``User-Name`` block.

    PROVISIONAL. The block's text runs the display name, the handle and the
    timestamp together (``"Some Account@someaccount·1h"``), and this splits on
    the ``@``. The captures cannot validate it: the scrubber replaces every text
    node with block characters, so they prove the block EXISTS and is first-wins
    on a quote-tweet, but not what splitting it yields on real text.

    The handle does not depend on this — it comes from the permalink, which is
    confirmed. A wrong display name costs a label; it cannot mis-address a post.
    """
    node = _first(card, xs.AUTHOR_SELECTORS)
    if node is None:
        return ""
    raw = " ".join(node.text().split())
    return raw.split("@")[0].strip(" ·-–—") if "@" in raw else raw


def _posted_at(card) -> Optional[str]:
    """The card's own ``<time datetime>``, or None.

    First-wins for the same reason as the author: a quote-tweet carries the
    quoted post's timestamp too. A card with no ``<time>`` at all is the shape
    the one paid ad in the capture had.
    """
    times = dom_probe.select_css(card, "time")
    for node in times:
        stamp = node.attrs.get("datetime")
        if stamp:
            return stamp
    return None


def parse_card(card) -> Optional[XPost]:
    """One card -> one :class:`XPost`, or None if it cannot be addressed."""
    url = canonical_permalink(card)
    if not url:
        return None

    handle, tweet_id = _split_permalink(url)
    text_node = _first(card, xs.TWEET_TEXT_SELECTORS)
    replies, reposts, likes = _engagement(card)

    return XPost(
        url=url,
        tweet_id=tweet_id,
        author_handle=handle,
        author_name=_author_name(card),
        text=" ".join(text_node.text().split()) if text_node is not None else "",
        replies=replies,
        reposts=reposts,
        likes=likes,
        posted_at=_posted_at(card),
    )


def parse_timeline(html: str) -> List[XPost]:
    """Every addressable post in a saved or live timeline, in document order.

    Deduplicated by permalink within the page: X re-renders the same card in
    more than one row in some states, and a caller counting rows would
    double-count.

    Works unchanged on a status (permalink) page, where the subject post and its
    replies are all cards.
    """
    root = dom_probe.parse_html(html)
    seen, out = set(), []
    for sel in xs.TWEET_CARD_SELECTORS:
        for card in dom_probe.select_css(root, sel):
            post = parse_card(card)
            if post is None or post.url in seen:
                continue
            seen.add(post.url)
            out.append(post)
    return out


def parse_search_results(html: str) -> List[XPost]:
    """Posts from a search-results page.

    **PROVISIONAL — no saved DOM backs this.** The search page's probe showed the
    same card hooks as the timeline (``cellInnerDiv`` / ``tweet`` / ``tweetText``
    / ``User-Name`` / the action row) and 15 plain ``/status/`` hrefs, so one
    parser should serve both and this is a deliberate alias rather than a second
    implementation. But the search dump was BLOCKED by the scrubber's residue
    gate at capture time and never saved, so unlike the timeline there is no
    fixture of a real search page to test against.

    The four testids that blocked it are allowlisted now. Re-running
    ``x_dump.py --profile <p> --pages search`` should save it, at which point
    this alias can be tested the same way the timeline is. Until then, treat a
    search-mode run as unverified.
    """
    return parse_timeline(html)


# ─── Relevance: the shared analyzer, on X's terms ─────────────────────────────

def analyze_posts(posts: List[XPost], analyzer: ContentAnalyzer = None,
                  min_length: int = MIN_TWEET_LENGTH) -> List[XPost]:
    """Score ``posts`` in place with the SAME analyzer LinkedIn uses.

    The keyword tiers, the post-type classifier and the quality bands are about
    what a post SAYS, not which site it is on, so they are shared rather than
    duplicated. The one LinkedIn-shaped assumption is the minimum length, which
    is passed explicitly — see :data:`MIN_TWEET_LENGTH`.
    """
    analyzer = analyzer or ContentAnalyzer()
    for post in posts:
        is_ai, score, keywords, quality, post_type = analyzer.analyze(
            post.text, min_length=min_length)
        post.should_engage = is_ai
        post.relevance_score = score
        post.keywords_matched = keywords
        post.quality = quality
        post.post_type = post_type
    return posts


# ─── The store: the platform split, used as designed ──────────────────────────

def open_store(profile_name: str = None, path: str = None) -> ps.PostStore:
    """The X store for ``profile_name``.

    ``platform="x"`` puts this at ``data/<profile>/x/posts_db.json`` while
    LinkedIn keeps the legacy path it has always had. Two platforms are two
    files: a write here cannot touch a LinkedIn record, which is the isolation
    the store was split for.

    No reconciler is injected. LinkedIn's reconciles against the comment-drafts
    directory, which is a LinkedIn-shaped concept and simply wrong for X.
    """
    return ps.PostStore(profile_name=profile_name, path=path, platform=ps.X)


def harvest_to_store(posts: List[XPost], store: ps.PostStore,
                     save: bool = True) -> Dict[str, int]:
    """Write analysed ``posts`` into ``store``. Returns a per-outcome summary.

    Landing state mirrors the LinkedIn path: a post worth engaging with lands
    NEW, one that is not lands TRASH with a reason, and
    :meth:`PostStore.upsert_scraped` protects anything already acted on — a
    re-scrape never downgrades COMMENTED or GENERATED, and never resurrects a
    post the user rejected by hand.

    Dedup is the store's, keyed on the permalink. Re-running a harvest over an
    overlapping timeline refreshes records rather than duplicating them.
    """
    summary = {"seen": 0, "new": 0, "trashed": 0, "existing": 0}
    for post in posts:
        summary["seen"] += 1
        existed = store.get(post.url) is not None
        if post.should_engage:
            status, reason = ps.NEW, None
        else:
            status, reason = ps.TRASH, ps.REASON_LOW_QUALITY
        store.upsert_scraped(post.to_store_dict(), status=status, reason=reason)
        if existed:
            summary["existing"] += 1
        elif status == ps.NEW:
            summary["new"] += 1
        else:
            summary["trashed"] += 1
    if save:
        store.save()
    return summary


def harvest_html(html: str, store: ps.PostStore,
                 analyzer: ContentAnalyzer = None,
                 save: bool = True) -> Dict[str, int]:
    """parse -> analyze -> store, for one page of HTML.

    The whole read path in one call, with no browser anywhere in it. The live
    finder is this function plus a way to get ``html``.
    """
    posts = analyze_posts(parse_timeline(html), analyzer)
    summary = harvest_to_store(posts, store, save=save)
    summary["parsed"] = len(posts)
    return summary


# ─── The live half ────────────────────────────────────────────────────────────

class XTimelineFinder:
    """Fetch timeline HTML from a browser and hand it to the pure parser.

    Deliberately thin. Everything that can be decided from a saved page is
    decided in the functions above, which are tested offline; this class owns
    only what genuinely needs a live browser — navigation and scrolling.

    **Scrolling is not optional.** X virtualizes the timeline: rows leave the
    DOM as they scroll out. The captures went 9 -> 18 permalinks and 81 -> 90
    distinct testids across three scrolls, so a harvester that scrolls first and
    reads once will silently miss everything that scrolled past. This reads
    after EVERY scroll and accumulates by permalink.

    NOT COVERED OFFLINE: navigation and the scroll gesture themselves. Those are
    the live go/no-go.
    """

    TIMELINE_URL = X_HOST + "/home"
    SCROLL_JS = "window.scrollTo(0, document.body.scrollHeight);"

    def __init__(self, driver, logger=None, analyzer: ContentAnalyzer = None):
        self.driver = driver
        self.logger = logger
        self.analyzer = analyzer or ContentAnalyzer()

    def _log(self, msg, *args):
        if self.logger:
            self.logger.info(msg, *args)

    def collect(self, scrolls: int = 3, pause=None) -> List[XPost]:
        """Read the timeline across ``scrolls`` scrolls, accumulating by URL.

        Returns analysed posts in first-seen order.
        """
        by_url: Dict[str, XPost] = {}

        def absorb():
            before = len(by_url)
            for post in parse_timeline(self.driver.page_source):
                by_url.setdefault(post.url, post)
            return len(by_url) - before

        self.driver.get(self.TIMELINE_URL)
        self._log("x: timeline loaded, %d posts on first read", absorb())

        for i in range(max(0, scrolls)):
            self.driver.execute_script(self.SCROLL_JS)
            if pause:
                pause()
            gained = absorb()
            self._log("x: scroll %d/%d added %d posts", i + 1, scrolls, gained)

        return analyze_posts(list(by_url.values()), self.analyzer)

    def run(self, profile_name: str = None, scrolls: int = 3,
            store: ps.PostStore = None, pause=None) -> Dict[str, int]:
        """Collect and write to the X store. Returns the harvest summary."""
        store = store if store is not None else open_store(profile_name)
        posts = self.collect(scrolls=scrolls, pause=pause)
        summary = harvest_to_store(posts, store)
        summary["parsed"] = len(posts)
        self._log("x: harvest %s", summary)
        return summary
