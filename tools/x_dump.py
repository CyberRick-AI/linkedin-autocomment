"""x_dump.py — THROWAWAY. Probe 1 of the X platform spike.

The live-DOM recon instrument for X (Twitter), modelled on ``tools/feed_dump.py``
and ``tools/connector_dump.py``. Its job is to answer the ten open questions in
``.dev/AUDIT_x_platform.md`` §4 **from observation**, in one human-in-the-loop
session, and to leave behind artifacts that selector work can happen against
offline afterwards.

It writes NO selectors of its own into the codebase. It harvests what is
actually there (``data-testid``, ``data-view-name``, button ``aria-label``s,
class tokens) via ``dom_probe.harvest_hooks`` — the same offline engine the
selector gate uses — so running this also proves whether that engine copes with
X's markup before a single X selector is written.

SAFETY — two hard guards, both deliberate:

1. **Test account only.** The tool refuses to launch unless a profile is
   explicitly marked as an X test account (``"platform": "x"`` in profiles.json,
   or ``X_TEST_PROFILE`` naming such a profile). It will not reuse a LinkedIn
   profile's Chrome session, because that session belongs to a real account.
2. **Nothing unscrubbed is written to disk.** A live X page is full of real
   handles, names and post text. Every saved HTML file is passed through
   :func:`scrub_html` first, then through :func:`pii_residue` — if residue is
   found the file is not written. Fixtures are hand-authored from the *shape* of
   these dumps, never from the dumps themselves (``tests/fixtures/README.md``).

Usage:
    .venv/Scripts/python.exe tools/x_dump.py --selftest      # offline, no browser
    .venv/Scripts/python.exe tools/x_dump.py --preflight     # report readiness only
    .venv/Scripts/python.exe tools/x_dump.py --profile xtest
    .venv/Scripts/python.exe tools/x_dump.py --profile xtest --pages timeline,status

Exit codes:
    0  ok      1  error      2  no X test account configured (see --preflight)
"""

import argparse
import json
import os
import re
import sys
import time
from html.parser import HTMLParser
from html import escape

# Make `import linkedin_automation` resolve when run as `python tools/x_dump.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation import dom_probe
from linkedin_automation import profile_manager as pm

EXIT_OK, EXIT_ERROR, EXIT_NO_TEST_ACCOUNT = 0, 1, 2

# ─── Scrubbing ────────────────────────────────────────────────────────────────

# Tags whose *contents* are dropped wholesale. Script bodies are the single
# biggest PII leak on X — the initial-state JSON carries whole user objects.
_DROP_CONTENT_TAGS = {"script", "noscript", "style", "template", "title"}

# Attributes whose values are structural (they are what a selector would key on)
# and so survive redaction of handles/ids but are otherwise kept verbatim.
_STRUCTURAL_ATTRS = {
    "data-testid", "data-view-name", "role", "class", "type", "dir", "lang",
    "tabindex", "aria-expanded", "aria-haspopup", "aria-modal", "aria-hidden",
    "contenteditable", "placeholder", "name", "rel", "target",
}

# Attributes dropped entirely — media URLs, alt text and meta content are pure
# payload with no selector value.
_DROP_ATTRS = {"src", "srcset", "alt", "title", "content", "value", "poster",
               "style", "background", "data-image-url"}

# ─── data-testid sanitization ─────────────────────────────────────────────────
#
# X EMBEDS REAL HANDLES INSIDE data-testid VALUES. Confirmed live in the spike:
# a single authenticated timeline carried ~12 of them —
# ``UserAvatar-Container-<handle>`` for every avatar on screen — plus base64
# content ids in ``news_sidebar_article_*`` and numeric user ids in
# ``<userId>-follow``.
#
# The scrubber used to treat data-testid as purely structural and copy it
# verbatim, and ``pii_residue`` only ever looked at serialized text, so a saved
# X fixture would have carried real people's handles into the repo. That is the
# same PII-in-fixtures problem this project already had to clean up once.
#
# The structural STEM is what selector discovery needs (an adapter matches
# ``[data-testid^='UserAvatar-Container-']``); the tail is the person. So the
# stem survives and the tail is synthesized.

# Stems whose trailing segment names a person or a specific piece of content.
_TESTID_HANDLE_STEMS = (
    "UserAvatar-Container-",
    "UserCell-",
    "UserName-",
    "confirmationSheetDialog-",
    "news_sidebar_article_",
    "socialContext-",
)

# Suffixes that follow an identifying id, e.g. ``1234567890-follow``.
_TESTID_ID_SUFFIXES = ("-follow", "-unfollow", "-block", "-mute", "-subscribe")

# Testids confirmed structural in the spike, kept verbatim. Not exhaustive by
# design — anything unrecognized is reported rather than assumed safe.
_TESTID_SAFE_EXACT = frozenset({
    "cellInnerDiv", "primaryColumn", "sidebarColumn", "tweet", "tweetText",
    "tweetTextarea_0", "tweetButton", "tweetButtonInline", "reply", "retweet",
    "like", "unlike", "bookmark", "caret", "share", "User-Name",
    "Tweet-User-Avatar", "UserCell", "BottomBar", "google_sign_in_container",
    "loginButton", "signupButton", "AppTabBar_More_Menu", "GrokDrawer",
    "GrokDrawerHeader", "app-text-transition-container", "article-cover-image",
    "bottom-impression-pixel", "left-impression-pixel", "contentDisclosureButton",
    "createPollButton", "fileInput", "geoButton", "gifSearchButton", "grokImgGen",
    "icon-verified", "news_sidebar", "pillLabel", "chat-drawer-root",
    "chat-drawer-main", "float-SkipThumbnail", "ScrollSnap-List",
    "ScrollSnap-SwipeableList", "ScrollSnap-nextButtonWrapper",
    "ScrollSnap-prevButtonWrapper", "ControlBar-containerView",
    "ControlBar-videoThumbnail",
    # Confirmed structural by the first live home-timeline dump (2026-08-18).
    # Every one is X UI chrome carrying no user data; they were blocking the
    # save purely because the allowlist had never seen them.
    "premium-signup-tab", "scheduleOption", "toolBar", "trend",
    "tweet-text-show-more-link", "scrubber",
    # Second live load (2026-08-18) — X served a video and a repost this time,
    # surfacing two more pieces of chrome.
    #
    # `socialContext` is the "reposted" / "you follow X" label above a card. Its
    # VISIBLE TEXT names a person, but the testid itself is a fixed name — and
    # the text is dropped by the scrubber like all text. Note the deliberate
    # asymmetry with `_TESTID_HANDLE_STEMS`, which still carries `socialContext-`:
    # bare is structural, suffixed is treated as identity. Identity is checked
    # first, so a `socialContext-<handle>` variant is still caught.
    "socialContext",
    # Fourth live load (2026-08-23) — the sole residue blocker was `boostCta`,
    # the boost/promote call-to-action button on a post. Structural: it is a
    # fixed button name carrying no user data, and every handle on the page had
    # already been scrubbed (UserAvatar-Container-user4, -user9, ...).
    #
    # EXACT, not a family prefix: it appears once per page and no suffixed
    # variant exists anywhere in the dumps or in X's own JS bundles. If X ever
    # ships `boostCta-<something>`, the fail-safe gate blocks that save and a
    # human triages it — which is the intended direction to be wrong in.
    "boostCta",
    # First live STATUS-page capture (2026-08-23). The timeline never renders
    # either of these, so the page-type change surfaced them; both are chrome
    # and every handle on the page scrubbed correctly.
    #
    # Both EXACT — X's own bundle settles the prefix question:
    #
    #   `inline_reply_offscreen` is a hardcoded literal, `testID:
    #   "inline_reply_offscreen"` on the offscreen-impression wrapper around the
    #   inline reply box. No template, no index, so a family prefix would widen
    #   the allowlist for a variant that cannot exist.
    #
    #   `app-bar-back` IS built from a template — ``testID: o || `app-bar-${t}` ``
    #   — but `t` is `backButtonType`, which indexes a two-member static map
    #   (`{back, close}`) to pick an icon and label. So the family is real but
    #   CLOSED, and enumerating it is strictly narrower than a prefix: a tail X
    #   cannot emit (`app-bar-<handle>`) still blocks the save for triage.
    #
    # `app-bar-close` has not been observed in a dump; it is the other member of
    # that map, added so the first page carrying a close button does not re-block
    # a save the back button already allowed.
    "app-bar-back", "app-bar-close", "inline_reply_offscreen",
    # Timeline scroll (2026-08-24). Both EXACT, again on the bundle's evidence:
    #
    #   `captions` is `testID:"captions"` on the closed-caption toggle in the
    #   video ControlBar — a hardcoded literal, one per player. The bundle DOES
    #   build an indexed captions family, but under a different name entirely
    #   (`immersive-tweet-add-captions-icon-${id}`), so a `captions` prefix would
    #   not even be the rule that covers it — and that family's tail is a post
    #   id, which must keep blocking.
    #
    #   `previewInterstitial` appears in the bundle only as a standalone interned
    #   literal (next to `aspectMode`), never as `previewInterstitial-` or a
    #   template head, so there is no indexed variant to generalize to.
    "captions", "previewInterstitial",
    # First search-page capture (2026-08-28). All four EXACT.
    #
    #   `searchBoxOverflowButton` and `searchFiltersAdvancedSearch` are
    #   hardcoded testID literals in the search components. Nothing to
    #   generalize to.
    #
    #   `radioGroupLocation` / `radioGroupPeople` DO come from a template --
    #   testID: `radioGroup${this.props.name}` on X's generic RadioGroup -- but a
    #   `radioGroup` PREFIX is refused on purpose. That component is used
    #   app-wide, so the prefix would be an open family, and because the family
    #   allowlist is checked BEFORE the embedded-id sweep, `radioGroup<id>` would
    #   come back "safe" without the sweep ever seeing it. People and Location
    #   are the two groups X's search sidebar renders; enumerating them keeps
    #   every other tail on the blocking side.
    "radioGroupLocation", "radioGroupPeople",
    "searchBoxOverflowButton", "searchFiltersAdvancedSearch",
})

# Structural FAMILIES. Allowlisted as patterns, not literals, so a variant seen
# on the next scroll does not re-block a save: X numbers and positions these.
_TESTID_SAFE_SUFFIXES = (
    "-impression-pixel",        # top-/bottom-/left-/right-, and any future edge
)
_TESTID_SAFE_FAMILY_PREFIXES = (
    "tweetTextarea_",           # _0, _1 (threads), _0RichTextInputContainer
    "tweetPhoto",               # tweetPhoto, tweetPhoto-0, ...
    "progressBar",              # progressBar-bar, progressBar-...
    "videoComponent",           # pairs with videoPlayer; both are embed chrome
    "testCondensedMedia",       # condensed media preview; suffixed variants likely
)

# Families whose members are structural regardless of their tail.
_TESTID_SAFE_PREFIXES = (
    "AppTabBar_", "SideNav_", "SearchBox_", "ScrollSnap-", "ControlBar-",
    "card.", "Dropdown", "mask", "placementTracking", "videoPlayer",
)

# Tokens that are identifying wherever they appear inside a testid value.
_TESTID_LONG_ID_RE = re.compile(r"(?=[A-Za-z0-9_+/=]*\d)[A-Za-z0-9_+/=]{12,}")
_TESTID_DIGIT_ID_RE = re.compile(r"\d{4,}")


def classify_testid(value: str):
    """Return ``(status, stem)`` for a ``data-testid`` value.

    * ``"safe"`` — a known structural hook, kept verbatim.
    * ``"handle_stem"`` — a known stem with an identifying tail to synthesize.
    * ``"embedded_id"`` — a long numeric/base64 id anywhere in the value.
    * ``"unknown"`` — not recognized. **Not mutated, but reported**, so a new
      handle-bearing shape surfaces instead of passing silently. The residue
      gate refuses to save a dump containing one until a human triages it and
      either adds it to the safe list or gives it a stem.
    """
    if not value:
        return "safe", ""

    # 1. IDENTITY FIRST, always. Nothing added to the allowlists below can
    #    shadow these, so widening the allowlist can never weaken the scrub.
    for stem in _TESTID_HANDLE_STEMS:
        if value.startswith(stem) and len(value) > len(stem):
            return "handle_stem", stem
    for suffix in _TESTID_ID_SUFFIXES:
        if value.endswith(suffix) and len(value) > len(suffix):
            return "handle_stem", suffix

    # 2. Confirmed-structural hooks and families.
    if value in _TESTID_SAFE_EXACT:
        return "safe", value
    if any(value.endswith(suf) for suf in _TESTID_SAFE_SUFFIXES):
        return "safe", value
    if any(value.startswith(pre) for pre in _TESTID_SAFE_FAMILY_PREFIXES):
        return "safe", value
    if any(value.startswith(pre) for pre in _TESTID_SAFE_PREFIXES):
        return "safe", value

    # 3. An id embedded somewhere unrecognized.
    if _TESTID_DIGIT_ID_RE.search(value) or _TESTID_LONG_ID_RE.search(value):
        return "embedded_id", ""

    return "unknown", ""


_STATUS_RE = re.compile(r"/status/(\d+)")
_HANDLE_RE = re.compile(r"@[A-Za-z0-9_]{2,15}")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_LONG_DIGITS_RE = re.compile(r"\d{6,}")

# A profile path on one of X's own profile hosts, wherever it appears in a
# string — including nested in another URL's query.
_PROFILE_IN_URL_RE = re.compile(
    r"(https?://(?:www\.)?(?:x|twitter)\.com)/([A-Za-z0-9_]{2,15})(?=[/?&#]|$)")

# First path segments that are X's own routes, not somebody's handle.
_NON_PROFILE_PATHS = frozenset({
    "i", "home", "search", "explore", "notifications", "messages", "settings",
})

TEXT_PLACEHOLDER = "█"          # one block char per redacted word


class _Scrubber(HTMLParser):
    """Rewrite a document keeping its structure and dropping its content.

    Tag names, attribute names and structural attribute values survive; text
    nodes, media URLs and script bodies do not. Handles and numeric ids are
    replaced with stable synthetic ones so ``/status/<id>`` keeps its *shape* —
    which is the whole point of the permalink question — without carrying a real
    post id.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self._drop_depth = 0
        self._handles = {}
        self._status_ids = {}
        # What the testid pass did, surfaced to the caller. `unknown_testids` is
        # the one that matters: an unrecognized shape may be carrying a handle,
        # so it blocks the save rather than being quietly trusted.
        self.sanitized_testids = {}
        self.unknown_testids = set()

    # ── synthetic identity ───────────────────────────────────────────────────

    def _fake_handle(self, real: str) -> str:
        return self._handles.setdefault(real, f"@user{len(self._handles) + 1}")

    def _fake_status(self, real: str) -> str:
        """A synthetic id: same length, leading 9s, distinct per real id.

        Length is preserved so length-sensitive selector work still applies;
        distinctness is preserved so "every card carries its own permalink" is
        observable rather than an artifact of redaction; the leading 9 is what
        :func:`pii_residue` keys on to tell synthetic from real (X ids are
        snowflakes and do not start with 9).
        """
        if real in self._status_ids:
            return self._status_ids[real]
        n = len(self._status_ids) + 1
        fake = ("9" * max(len(real) - 4, 1) + f"{n:04d}")[:len(real)] if len(real) > 4 \
            else "9" * len(real)
        self._status_ids[real] = fake
        return fake

    def redact(self, value: str) -> str:
        """Replace handles, emails and long digit runs in an attribute value.

        Status ids are parked behind a digit-free sentinel first, so the blanket
        digit sweep that follows cannot flatten two different permalinks into the
        same string.
        """
        parked = []

        def _park(m):
            parked.append(self._fake_status(m.group(1)))
            return f"/status/\x00{len(parked) - 1}\x01"

        value = _STATUS_RE.sub(_park, value)
        value = _EMAIL_RE.sub("user@example.invalid", value)
        value = _HANDLE_RE.sub(lambda m: self._fake_handle(m.group(0)), value)
        value = _LONG_DIGITS_RE.sub(lambda m: "9" * len(m.group(0)), value)
        for i, fake in enumerate(parked):
            value = value.replace(f"\x00{i}\x01", fake)
        return value

    def _href(self, value: str) -> str:
        """Redact a link, preserving path shape (``/x/status/y`` stays that shape)."""
        value = self.redact(value)
        # A profile path anywhere in the string, not only at the front. X embeds
        # whole permalinks inside query strings —
        # ``publish.x.com/oembed?url=https://x.com/EXAMPLECO/status/<id>`` — and the
        # front-anchored rule below never saw those, so the id was synthesized
        # while the handle rode along into the saved fixture. Restricted to the
        # profile hosts on purpose: `publish.x.com/oembed` is an API path, and a
        # host-blind rule would rename `oembed` to a person.
        value = _PROFILE_IN_URL_RE.sub(
            lambda m: m.group(1) + "/" + (
                m.group(2) if m.group(2).lower() in _NON_PROFILE_PATHS
                else self._fake_handle("@" + m.group(2)).lstrip("@")),
            value)
        # A bare profile path (/someone, /someone/status/..) still names a person.
        m = re.match(r"^(https?://[^/]+)?/([A-Za-z0-9_]{2,15})(/|$)", value)
        if m and m.group(2).lower() not in _NON_PROFILE_PATHS:
            fake = self._fake_handle("@" + m.group(2)).lstrip("@")
            value = (m.group(1) or "") + "/" + fake + value[m.end(2):]
        return value

    def _testid(self, value: str) -> str:
        """Keep the structural stem of a ``data-testid``, synthesize the person.

        ``UserAvatar-Container-exampleperson`` -> ``UserAvatar-Container-user1``:
        the adapter matches on the stem, so selector discovery is unaffected,
        and the handle never reaches disk. Unknown shapes are recorded, not
        mutated — mangling a genuinely structural hook would silently break the
        harvest, so they are reported and the residue gate blocks the write.
        """
        status, stem = classify_testid(value)
        if status == "safe":
            return value
        if status == "unknown":
            self.unknown_testids.add(value)
            return value
        if status == "handle_stem":
            if stem in _TESTID_ID_SUFFIXES:
                real = value[: -len(stem)]
                # Same length, leading 9s: keeps the id's shape without its value.
                new = ("9" * max(len(real) - 1, 1) + str(len(self._handles) + 1))[:len(real)] + stem
                self._handles[real] = new
            else:
                fake = self._fake_handle("@" + value[len(stem):])
                new = stem + fake.lstrip("@")
        else:                                   # embedded_id
            new = _TESTID_LONG_ID_RE.sub("X", _TESTID_DIGIT_ID_RE.sub("9999", value))
        self.sanitized_testids[value] = new
        return new

    # ── HTMLParser hooks ─────────────────────────────────────────────────────

    def _emit_tag(self, tag, attrs, closing=""):
        parts = [tag]
        for name, value in attrs:
            if name in _DROP_ATTRS:
                continue
            if value is None:
                parts.append(name)
                continue
            if name == "href":
                value = self._href(value)
            elif name == "data-testid":
                value = self._testid(value)
            elif name in _STRUCTURAL_ATTRS:
                value = self.redact(value)
            elif name.startswith("aria-") or name.startswith("data-"):
                value = self.redact(value)
            else:
                value = self.redact(value)
            parts.append(f'{name}="{escape(value, quote=True)}"')
        self.out.append(f"<{' '.join(parts)}{closing}>")

    def handle_starttag(self, tag, attrs):
        if self._drop_depth:
            if tag in _DROP_CONTENT_TAGS:
                self._drop_depth += 1
            return
        if tag in _DROP_CONTENT_TAGS:
            self._drop_depth = 1
            self._emit_tag(tag, attrs)
            return
        self._emit_tag(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        if self._drop_depth:
            return
        self._emit_tag(tag, attrs, closing="/")

    def handle_endtag(self, tag):
        if self._drop_depth:
            if tag in _DROP_CONTENT_TAGS:
                self._drop_depth -= 1
                self.out.append(f"</{tag}>")
            return
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if self._drop_depth:
            return
        stripped = data.strip()
        if not stripped:
            return
        # One block char per word: word count and layout survive, content does not.
        self.out.append(TEXT_PLACEHOLDER * min(len(stripped.split()), 40))

    def handle_comment(self, data):
        return  # comments can carry anything; drop them


def scrub_html(html: str, report: dict = None) -> str:
    """Return ``html`` with structure preserved and every payload removed.

    Pure function — this is the piece ``--selftest`` exercises without a browser,
    because a scrubber that is only ever run on live data is a scrubber nobody
    has checked.
    """
    parser = _Scrubber()
    parser.feed(html)
    parser.close()
    if report is not None:
        report["sanitized_testids"] = dict(parser.sanitized_testids)
        report["unknown_testids"] = sorted(parser.unknown_testids)
    return "".join(parser.out)


def pii_residue(html: str) -> dict:
    """Report anything in ``html`` that still looks personally identifying.

    The gate that runs *after* scrubbing: a dump is written only if this is
    empty. Deliberately blunt — false positives cost a re-check, a false
    negative writes a real person's post to disk.
    """
    found = {}
    handles = sorted({h for h in _HANDLE_RE.findall(html)
                      if not re.fullmatch(r"@user\d+", h)})
    emails = sorted({e for e in _EMAIL_RE.findall(html)
                     if not e.endswith("example.invalid")})
    # Every attribute value passes through the digit sweep, so any long run left
    # in the output is either all-9s or a synthetic status id — both start with
    # 9. A run starting with anything else escaped scrubbing.
    digits = sorted({d for d in _LONG_DIGITS_RE.findall(html)
                     if not d.startswith("9")})
    if handles:
        found["handles"] = handles[:10]
    if emails:
        found["emails"] = emails[:10]
    if digits:
        found["long_digit_runs"] = digits[:10]

    # ── inside attribute values, not only the serialized text ────────────────
    # The original gate matched @handles, emails and long digit runs against the
    # whole document. None of those shapes match `UserAvatar-Container-exampleperson`,
    # so a dozen real handles per page sailed through it. X puts identity in
    # attribute VALUES, so the gate has to look there.
    bad_testids, unknown = [], []
    try:
        root = dom_probe.parse_html(html)
        for node in root.walk():
            value = node.attrs.get("data-testid")
            if not value:
                continue
            status, stem = classify_testid(value)
            if status == "unknown":
                unknown.append(value)
            elif status == "handle_stem":
                tail = (value[len(stem):] if not stem.startswith("-")
                        else value[: -len(stem)])
                # A sanitized tail is `user<N>` or an all-9s id. Anything else is
                # a real handle the scrubber did not reach.
                if not re.fullmatch(r"user\d+|9\d*", tail):
                    bad_testids.append(value)
            elif status == "embedded_id":
                bad_testids.append(value)
    except Exception:
        bad_testids.append("<testid scan failed - treat as unsafe>")

    if bad_testids:
        found["identifying_testids"] = sorted(set(bad_testids))[:10]
    if unknown:
        # Not proof of PII, but an unrecognized shape is exactly how the handles
        # got through last time, so it blocks the write until a human looks.
        found["unrecognized_testids"] = sorted(set(unknown))[:10]
    return found


# ─── Everything written to disk ───────────────────────────────────────────────
#
# The testid gate only ever guarded the HTML. The observation record itself was
# writing `url_after_nav` — and the nav log, and `signals["_url"]` — straight to
# disk as observed, so a status dump put a real handle AND a real post id into
# `data/<profile>/x_spike/x_probe_*.json`. Same class of leak as the testids,
# just outside the thing that was being checked.
#
# The fix reuses the scrubber's own `_href`, so a URL on disk is redacted by
# exactly the code that redacts an href inside the document, with the same
# user1/user2 synthesis. Structure is the whole point of keeping these fields at
# all — `x.com/<handle>/status/<id>` stays that shape, so "did the navigation
# land on a status page" is still answerable from the record.

# Only strings that carry a scheme, plus the unambiguous bare permalink shape.
# Deliberately narrow. `blocked_url_markers` holds bare fragments — `/login`,
# `/suspended`, `/account/access` — and a rule as loose as "starts with a slash"
# would rewrite `/suspended` into `/user1` and quietly destroy a block signal.
# A bare `/<handle>` is indistinguishable from those markers, so it is left to
# the HTML scrubber's `_href`, which sees it in an href where it IS a profile
# link; only `/<handle>/status/<id>` is unambiguous enough to scrub here.
_URL_SCHEME_RE = re.compile(r"^https?://\S*$")
_BARE_X_PATH_RE = re.compile(r"^/[A-Za-z0-9_]{2,15}/status/\d+/?$")


def scrub_url(url, scrubber=None):
    """Redact one URL for disk, preserving its path shape.

    ``https://x.com/EXAMPLECO/status/1011001100110011001`` ->
    ``https://x.com/user1/status/9999999999999990001``.

    Pass a shared ``scrubber`` to keep user1/user2 stable across a whole report;
    without one each call numbers from scratch.
    """
    if not isinstance(url, str) or not url:
        return url
    if not (_URL_SCHEME_RE.match(url) or _BARE_X_PATH_RE.match(url)):
        return url
    return (scrubber or _Scrubber())._href(url)


# `signals["_title"]` is `driver.title`, and on a status page that is the whole
# card: `(1) EXAMPLECO on X: "Researchers found that a metabolite ..."`. It reached
# disk by the same route the URLs did, and it is the larger of the two leaks.
#
# NOTE: `redact()` alone does NOT close this one. It matches @handles, emails
# and long digit runs; a display name and a sentence of post text contain none
# of those, so run over the title above it returns the title unchanged. A title
# therefore also gets the rule the scrubber applies to every text node in the
# document: one block char per word. Word count survives, content does not.
_TITLE_ROUTE_RE = re.compile(
    r"^(home|explore|notifications|messages|bookmarks|lists|search|settings|"
    r"log in to x|sign up|x)$", re.I)
_TITLE_COUNT_RE = re.compile(r"^\(\d+\)\s*")
_TITLE_SUFFIX_RE = re.compile(r"\s*/\s*X\s*$")

# Keys whose value is free text observed on the page rather than a URL.
_ON_DISK_TEXT_KEYS = frozenset({"_title", "title", "page_title"})


def scrub_title(value, scrubber=None):
    """Redact a page title, keeping its shape and X's own route names.

    ``(1) EXAMPLECO on X: "Researchers found ..."`` loses every word; ``(1) Home / X``
    names a route rather than a person and survives intact.
    """
    if not isinstance(value, str) or not value.strip():
        return value
    scrubber = scrubber or _Scrubber()
    value = scrubber.redact(value)

    m = _TITLE_COUNT_RE.match(value)
    prefix, rest = (m.group(0), value[m.end():]) if m else ("", value)
    m = _TITLE_SUFFIX_RE.search(rest)
    suffix, body = (m.group(0), rest[:m.start()]) if m else ("", rest)

    if _TITLE_ROUTE_RE.match(body.strip()):
        return prefix + body + suffix
    # A synthetic @user<N> is kept: it names nobody, and where the handle was is
    # the only part of a redacted title worth reading back. Punctuation rides
    # along -- a profile title spells it `(@user1)`, parens and all.
    words = [w if re.fullmatch(r"\W*@user\d+\W*", w) else TEXT_PLACEHOLDER
             for w in body.split()]
    return prefix + " ".join(words[:40]) + suffix


def scrub_for_disk(obj, scrubber=None, key=None):
    """Return a copy of a JSON-able structure that is safe to write out.

    Applied at the WRITE boundary rather than field by field, so a field added
    later that happens to hold a navigated URL or an observed title is covered
    by default instead of leaking until somebody remembers this function exists.
    """
    scrubber = scrubber or _Scrubber()
    if isinstance(obj, str):
        if key in _ON_DISK_TEXT_KEYS:
            return scrub_title(obj, scrubber)
        return scrub_url(obj, scrubber)
    if isinstance(obj, dict):
        return {k: scrub_for_disk(v, scrubber, k) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub_for_disk(v, scrubber, key) for v in obj]
    return obj


def disk_pii_residue(obj) -> dict:
    """Report anything in a record that still names a real person or post.

    The disk-side counterpart to :func:`pii_residue`: a synthetic handle is
    ``user<N>`` and a synthetic id is all-9s-with-a-counter, so anything else in
    a URL position escaped the scrub -- and a title is clean only if every word
    is a block char, a synthetic handle, or one of X's own route names.
    """
    found, titles = [], []

    def visit(value, key=None):
        if isinstance(value, dict):
            for k, v in value.items():
                visit(v, k)
        elif isinstance(value, (list, tuple)):
            for v in value:
                visit(v, key)
        elif isinstance(value, str) and key in _ON_DISK_TEXT_KEYS:
            body = _TITLE_SUFFIX_RE.sub("", _TITLE_COUNT_RE.sub("", value))
            if body.strip() and not _TITLE_ROUTE_RE.match(body.strip()) and any(
                    TEXT_PLACEHOLDER not in w
                    and not re.fullmatch(r"\W*@user\d+\W*", w)
                    for w in body.split()):
                titles.append(value)
        elif isinstance(value, str) and (
                _URL_SCHEME_RE.match(value) or _BARE_X_PATH_RE.match(value)):
            m = re.search(r"/status/(\d+)", value)
            if m and not m.group(1).startswith("9"):
                found.append(value)
                return
            # A profile segment at the front, and any nested in a query string:
            # `publish.x.com/oembed?url=https://x.com/<handle>/status/<id>` put a
            # real handle on disk while its id was correctly synthesized.
            segments = [m.group(1) for m in [re.match(
                r"^(?:https?://[^/]+)?/([A-Za-z0-9_]{2,15})(?:/|$)", value)] if m]
            segments += [m.group(2) for m in _PROFILE_IN_URL_RE.finditer(value)]
            if any(seg.lower() not in _NON_PROFILE_PATHS
                   and not re.fullmatch(r"user\d+", seg) for seg in segments):
                found.append(value)

    visit(obj)
    out = {}
    if found:
        out["identifying_urls"] = sorted(set(found))[:10]
    if titles:
        out["identifying_titles"] = sorted(set(titles))[:10]
    return out


# ─── Analysis (pure, offline-testable) ────────────────────────────────────────

def status_hrefs(html: str) -> list:
    """Every ``/status/<id>`` link in the document — Q2, the permalink question.

    If a timeline card carries one of these *without* a menu being opened, X
    needs none of LinkedIn's overflow-menu + clipboard-intercept machinery.
    """
    root = dom_probe.parse_html(html)
    out = []
    for node in root.walk():
        href = node.attrs.get("href") or ""
        if "/status/" in href:
            out.append(href)
    return out


def summarize(html: str) -> dict:
    """Harvest every hook in a document plus the permalink evidence."""
    hooks = dom_probe.harvest_hooks(html)
    hrefs = status_hrefs(html)
    class_tokens = hooks.get("class_tokens", [])
    return {
        "bytes": len(html),
        "data_testids": hooks["data_testids"],
        "data_testid_count": len(hooks["data_testids"]),
        "data_view_names": hooks["data_view_names"],
        "button_aria_labels": hooks["button_aria_labels"],
        "class_token_count": len(class_tokens),
        "class_tokens_sample": class_tokens[:40],
        "status_href_count": len(hrefs),
        "status_hrefs_sample": hrefs[:10],
        # Uncapped for the same reason as _distinct_testids.
        "candidate_selectors": dom_probe.candidate_selectors(hooks),
    }


def testid_counts(html: str) -> dict:
    """``data-testid -> occurrences`` — the audit's highest-value single output."""
    root = dom_probe.parse_html(html)
    counts = {}
    for node in root.walk():
        tid = node.attrs.get("data-testid")
        if tid:
            counts[tid] = counts.get(tid, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


# ─── Account guard ────────────────────────────────────────────────────────────

def resolve_x_test_profile(profile_name: str = None) -> tuple:
    """Return ``(name, profile_dict)`` for an X **test** profile, or ``(None, reason)``.

    Refuses anything not explicitly marked as an X test account. There is no
    inference here on purpose: a profile that merely exists is a LinkedIn profile
    for a real person, and driving it against X would be automating a real
    account.
    """
    name = profile_name or os.environ.get("X_TEST_PROFILE")
    data = pm.load_profiles()
    profiles = data.get("profiles", {})

    if not name:
        marked = [n for n, p in profiles.items() if p.get("platform") == "x"]
        if len(marked) == 1:
            name = marked[0]
        elif not marked:
            return None, ("no profile is marked as an X test account "
                          "(none has \"platform\": \"x\")")
        else:
            return None, f"several X profiles exist ({', '.join(marked)}); pass --profile"

    profile = profiles.get(name)
    if profile is None:
        return None, (f"profile {name!r} does not exist "
                      f"(have: {', '.join(profiles) or 'none'})")
    if profile.get("platform") != "x":
        return None, (f"profile {name!r} is not marked as an X test account "
                      f"(platform={profile.get('platform', 'linkedin')!r}). Refusing: "
                      f"this session belongs to a real LinkedIn account.")
    return name, profile


def x_session_dir(profile_name: str) -> str:
    """A Chrome ``user-data-dir`` for X, separate from the profile's LinkedIn one.

    Answers audit open question 1 by construction: separate dirs, so an X
    re-login can never disturb a LinkedIn session. Sibling rather than child of
    the LinkedIn dir — a user-data-dir nested inside another confuses Chrome.
    """
    return os.path.join(pm.CHROME_SESSIONS_DIR, f"{profile_name}__x")


def preflight_report(profile_name: str = None) -> tuple:
    """Return ``(message, exit_code)`` describing whether a live run can happen.

    Takes the requested profile so a **rejected** ``--profile`` reports its own
    refusal. Falling back to auto-discovery here would answer a different
    question than the one asked — it would say "RUNNABLE with 'xtest'" to someone
    who asked about `jeff`, and exit 0 while doing it.
    """
    name, info = resolve_x_test_profile(profile_name)
    if name is None:
        return (
            "X live probe: NOT RUNNABLE — " + info + "\n\n"
            "To set one up (dedicated X TEST account only, never a real one):\n"
            "  1. Create the test account on X in a normal browser.\n"
            "  2. Add it as a profile:\n"
            "       python linkedin_profile_manager.py add xtest\n"
            "  3. Mark it as X in data/profiles/profiles.json:\n"
            '       \"xtest\": { ..., \"platform\": \"x\" }\n'
            "  4. Establish the persistent session (a window opens; log in, close it):\n"
            "       python tools/x_dump.py --profile xtest --login-only\n"
            "  5. Then run the probe:\n"
            "       python tools/x_dump.py --profile xtest",
            EXIT_NO_TEST_ACCOUNT,
        )
    session = x_session_dir(name)
    exists = os.path.isdir(session) and os.listdir(session)
    return (
        f"X live probe: RUNNABLE with profile {name!r}.\n"
        f"  X Chrome session dir: {session}\n"
        f"  Session established:  {'yes' if exists else 'no — first run will need a manual login'}",
        EXIT_OK,
    )


# ─── Live probe ───────────────────────────────────────────────────────────────

# Signals that X bounced us, challenged us, or rate-limited us. These are still
# useful — a redirect to /account/access IS a real block — but see the login
# detection below for why they are NOT sufficient to decide "logged in".
BLOCKED_URL_MARKERS = ("/login", "/i/flow/login", "/account/access", "/challenge",
                       "/suspended", "/error", "/i/flow/signup")
BLOCKED_TEXT_MARKERS = ("rate limit", "try again later", "unusual activity",
                        "verify your identity", "something went wrong",
                        "are you a robot", "confirm your")


# ─── Login detection ──────────────────────────────────────────────────────────
#
# URL-ONLY DETECTION IS WRONG ON X. Observed 2026-08-15: a logged-out session
# lands on https://x.com/ and is never redirected to a /login URL, so "the URL
# carries no logged-out marker" is true of BOTH states and decides nothing. That
# is the same false-positive class as the old LinkedIn login bug, which is why
# `profile_manager.is_logged_in_on_page` grew URL markers in the first place —
# but X does not even give us those.
#
# So: decide from the DOM and the cookie jar, and never from the URL alone.
#
# EVERY SELECTOR BELOW IS A CANDIDATE, NOT A CONFIRMED HOOK. They are here to be
# *probed and reported*, so the session that runs this learns which ones actually
# discriminate. The classifier deliberately depends on no single one of them.

# Candidates expected only when AUTHENTICATED. (name, css)
AUTH_ONLY_CANDIDATES = (
    ("sidenav_new_tweet",        "[data-testid='SideNav_NewTweet_Button']"),
    ("sidenav_account_switcher", "[data-testid='SideNav_AccountSwitcher_Button']"),
    ("apptabbar_home",           "[data-testid='AppTabBar_Home_Link']"),
    ("tweet_textarea",           "[data-testid='tweetTextarea_0']"),
    ("primary_column",           "[data-testid='primaryColumn']"),
    ("href_home",                "a[href='/home']"),
    ("href_compose_post",        "a[href*='/compose/post']"),
    ("href_notifications",       "a[href='/notifications']"),
    ("href_messages",            "a[href='/messages']"),
    ("nav_role_navigation",      "nav[role='navigation']"),
    ("article_cards",            "article"),
)

# Candidates expected only when LOGGED OUT. A logged-out home still renders a
# timeline-ish page, so these are what actually separate the two states.
LOGGED_OUT_CANDIDATES = (
    ("login_button",       "[data-testid='loginButton']"),
    ("signup_button",      "[data-testid='signupButton']"),
    ("href_flow_login",    "a[href*='/i/flow/login']"),
    ("href_flow_signup",   "a[href*='/i/flow/signup']"),
    ("href_login",         "a[href='/login']"),
    ("google_signin_frame", "iframe[src*='accounts.google.com']"),
)

# Cookie names probed for presence. X's session cookie is the single most
# reliable candidate signal available — it is set by the server on login, it is
# not a rendering detail, and it cannot be faked by a logged-out page. Still
# probed rather than assumed.
# `gt` (guest token) and `guest_id*` were observed live on a logged-out session
# 2026-08-15; `auth_token`/`ct0`/`twid` are the authenticated candidates and are
# still unconfirmed because no authenticated session has existed yet.
COOKIE_CANDIDATES = ("auth_token", "ct0", "twid", "gt", "guest_id")

# Guest-session cookies. Their presence alongside a MISSING auth cookie, on a
# page that demonstrably rendered, is the fallback logged-out inference.
GUEST_COOKIE_SIGNALS = ("cookie_gt", "cookie_guest_id")

# Body-text markers. Weakest evidence (localised, and marketing copy changes),
# reported but never decisive on their own.
LOGGED_OUT_TEXT_MARKERS = ("sign in", "create account", "log in", "sign up")

# The subset the classifier treats as *strong* evidence of being authenticated:
# the session cookie, plus DOM hooks that only an authenticated shell renders.
# Any ONE is enough, so a single renamed testid cannot produce a false negative.
STRONG_AUTH_SIGNALS = ("cookie_auth_token", "sidenav_new_tweet",
                       "sidenav_account_switcher", "apptabbar_home",
                       "href_compose_post", "href_messages")

# The subset treated as *strong* evidence of being logged out.
STRONG_LOGGED_OUT_SIGNALS = ("login_button", "signup_button", "href_flow_login",
                             "href_flow_signup", "href_login")


def wait_for_app(driver, timeout: int = 25) -> dict:
    """Block until the SPA has actually rendered something, or the timeout.

    Without this a probe can measure an empty React shell and report every
    candidate as absent — which is indistinguishable from "all our selectors are
    stale" and would send selector work chasing ghosts. Returns the render
    diagnostics so the caller can tell those two cases apart afterwards.
    """
    from selenium.webdriver.common.by import By

    deadline = time.time() + timeout
    diag = {}
    while True:
        try:
            diag = {
                "_testid_elements": len(driver.find_elements(By.CSS_SELECTOR, "[data-testid]")),
                "_body_text_len": driver.execute_script(
                    "return (document.body && document.body.innerText || '').length") or 0,
                "_ready_state": driver.execute_script("return document.readyState"),
                "_dom_nodes": driver.execute_script(
                    "return document.getElementsByTagName('*').length") or 0,
            }
            if diag["_testid_elements"] > 0 or diag["_body_text_len"] > 200:
                return diag
        except Exception:
            pass
        if time.time() >= deadline:
            return diag
        time.sleep(1)


def probe_login_signals(driver) -> dict:
    """Evaluate every candidate signal against the page currently loaded.

    Returns a flat ``name -> count/bool`` vector. Each selector is evaluated
    defensively: a candidate that is invalid or unsupported records ``None``
    rather than aborting the probe, because the point is to find out which of
    these exist, and a guess that turns out to be nonsense must not take the
    others down with it.
    """
    from selenium.webdriver.common.by import By

    signals = dict(wait_for_app(driver))

    # Does X use data-testid on this page AT ALL? The single most diagnostic
    # number here: 0 rendered testids means either nothing rendered or the whole
    # data-testid convention is gone, and `_body_text_len` separates those.
    try:
        # SCRUBBED, never raw. This vector is written to
        # data/<profile>/x_spike/*.json, and on a live timeline the raw harvest
        # returned about a dozen `UserAvatar-Container-<real handle>` values
        # straight into that file. Harvesting from the scrubbed copy means the
        # stems (what selector work needs) survive and the people do not.
        page = scrub_html(driver.page_source)
        # NO CAP. The spike capped this at [:60] of 87 distinct testids and the
        # alphabetical slice cut everything after `news_sidebar` — which is
        # exactly `reply`, `retweet`, `tweet` and `tweetTextarea_0`, the hooks
        # the adapter is built from. A display cap made the harvest look empty
        # where it mattered most.
        counts = testid_counts(page)
        signals["_distinct_testids"] = sorted(counts)
        signals["_distinct_testid_count"] = len(counts)
    except Exception:
        signals["_distinct_testids"] = None

    def count(css):
        try:
            return len(driver.find_elements(By.CSS_SELECTOR, css))
        except Exception:
            return None

    for name, css in AUTH_ONLY_CANDIDATES:
        signals[name] = count(css)
    for name, css in LOGGED_OUT_CANDIDATES:
        signals[name] = count(css)

    try:
        cookies = {c["name"] for c in driver.get_cookies()}
    except Exception:
        cookies = set()
    for name in COOKIE_CANDIDATES:
        signals[f"cookie_{name}"] = name in cookies
    signals["_cookie_names"] = sorted(cookies)

    try:
        body = driver.find_element(By.TAG_NAME, "body").text.lower()[:20000]
    except Exception:
        body = ""
    for marker in LOGGED_OUT_TEXT_MARKERS:
        signals[f"text_{marker.replace(' ', '_')}"] = marker in body

    try:
        signals["_url"] = driver.current_url
        signals["_title"] = driver.title
    except Exception:
        pass
    return signals


def classify_login(signals: dict) -> tuple:
    """Decide ``("logged_in"|"logged_out"|"uncertain", evidence)`` from a vector.

    Pure function over the signal dict — no driver — so the decision logic is
    checkable offline against hand-written vectors, which is the part the old
    URL-only check never had.

    Conservative by construction: it reports ``uncertain`` when signals conflict
    or when nothing fires, rather than defaulting to either state. A probe that
    says "I don't know" is recoverable; one that says "logged in" when it is not
    wastes a whole recon session, which is exactly what happened.
    """
    fired_in = [n for n in STRONG_AUTH_SIGNALS if signals.get(n)]
    fired_out = [n for n in STRONG_LOGGED_OUT_SIGNALS if signals.get(n)]
    evidence = {"auth_signals_fired": fired_in, "logged_out_signals_fired": fired_out}

    if fired_in and not fired_out:
        return "logged_in", evidence
    if fired_out and not fired_in:
        return "logged_out", evidence
    if fired_in and fired_out:
        # X renders a login CTA over an authenticated shell during some
        # challenge flows, so this combination is meaningful, not impossible.
        evidence["note"] = ("both authenticated and logged-out signals present — "
                            "possible challenge/interstitial over a live session")
        return "uncertain", evidence

    # Fallback, used only when no DOM candidate fired at all. Observed live
    # 2026-08-15: every guessed DOM hook read 0 on a logged-out session, but the
    # cookie jar was unambiguous — guest cookies, no auth cookie. That is
    # evidence, so it is used; it is weaker than a DOM hit, so it is last.
    rendered = bool(signals.get("_testid_elements")) or \
        (signals.get("_body_text_len") or 0) > 200
    guest = [n for n in GUEST_COOKIE_SIGNALS if signals.get(n)]
    if rendered and guest and signals.get("cookie_auth_token") is False:
        evidence["note"] = (f"no DOM candidate fired; inferring from cookies — page "
                            f"rendered, guest cookies {guest}, no auth_token")
        evidence["inferred_from"] = "cookies"
        return "logged_out", evidence

    evidence["note"] = (
        f"no strong signal in either direction (rendered={rendered}, "
        f"testid_elements={signals.get('_testid_elements')}, "
        f"body_text_len={signals.get('_body_text_len')}) — page may not have "
        f"finished rendering, or every candidate selector is stale")
    return "uncertain", evidence


def login_state(driver) -> tuple:
    """``(state, signals, evidence)`` for the page currently loaded."""
    signals = probe_login_signals(driver)
    state, evidence = classify_login(signals)
    return state, signals, evidence


def print_signals(signals: dict, evidence: dict = None):
    """Print the signal vector in a form a human can read a verdict out of."""
    print(f"    render: testid_elements={signals.get('_testid_elements')} "
          f"distinct_testids={signals.get('_distinct_testid_count')} "
          f"body_text_len={signals.get('_body_text_len')} "
          f"dom_nodes={signals.get('_dom_nodes')} "
          f"ready={signals.get('_ready_state')}")
    if signals.get("_distinct_testids"):
        print(f"    testids present: {signals['_distinct_testids']}")
    print("    auth-only candidates:")
    for name, _ in AUTH_ONLY_CANDIDATES:
        v = signals.get(name)
        mark = "+" if v else ("?" if v is None else " ")
        print(f"      {mark} {str(v):>5}  {name}")
    print("    logged-out candidates:")
    for name, _ in LOGGED_OUT_CANDIDATES:
        v = signals.get(name)
        mark = "+" if v else ("?" if v is None else " ")
        print(f"      {mark} {str(v):>5}  {name}")
    print("    cookies:")
    for name in COOKIE_CANDIDATES:
        v = signals.get(f"cookie_{name}")
        print(f"      {'+' if v else ' '} {str(v):>5}  cookie_{name}")
    print(f"      (all cookie names: {signals.get('_cookie_names')})")
    print("    body text:")
    for marker in LOGGED_OUT_TEXT_MARKERS:
        v = signals.get(f"text_{marker.replace(' ', '_')}")
        print(f"      {'+' if v else ' '} {str(v):>5}  text_{marker}")
    if evidence:
        print(f"    evidence: {evidence}")


def discriminating_signals(out_vec: dict, in_vec: dict) -> dict:
    """Which signals actually separate the two observed states.

    This is the finding the session exists to produce. A signal only earns the
    name if it is absent/zero when logged out and present when logged in (or the
    reverse) — anything present in both is useless no matter how plausible it
    looked in the candidate list.
    """
    def truthy(v):
        return bool(v) and v is not None

    auth_discriminators, out_discriminators, useless = [], [], []
    names = [n for n, _ in AUTH_ONLY_CANDIDATES] + [n for n, _ in LOGGED_OUT_CANDIDATES] \
        + [f"cookie_{c}" for c in COOKIE_CANDIDATES]
    for name in names:
        was_out, was_in = truthy(out_vec.get(name)), truthy(in_vec.get(name))
        if was_in and not was_out:
            auth_discriminators.append(name)
        elif was_out and not was_in:
            out_discriminators.append(name)
        elif was_in and was_out:
            useless.append(name)
    return {
        "authenticated_only": auth_discriminators,
        "logged_out_only": out_discriminators,
        "present_in_both_useless": useless,
    }

PAGES = {
    "timeline_foryou":   "https://x.com/home",
    "timeline_following": "https://x.com/home",     # requires clicking the tab
    "search":            "https://x.com/search?q=python&f=live",
    "explore":           "https://x.com/explore",
    "notifications":     "https://x.com/notifications",
}

# Pages that need an operator-supplied URL, because a permalink and a profile
# are specific to a tweet and an account. Phase B needs BOTH: the status page is
# where the permalink question (§B.1) is settled, and the profile page is where
# the follow button lives. Supplied via --status-url / --profile-url.
PARAMETERIZED_PAGES = ("status", "profile")


def create_x_driver(profile_name: str, headless: bool = False):
    """Chrome with a persistent X-only ``user-data-dir``.

    Mirrors ``profile_manager.create_driver``'s options rather than calling it,
    because that function is bound to the profile's LinkedIn session dir and the
    spike must not change production code to find something out.
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    session_dir = x_session_dir(profile_name)
    os.makedirs(session_dir, exist_ok=True)

    options = Options()
    options.add_argument(f"--user-data-dir={os.path.abspath(session_dir)}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--log-level=3")
    if headless:
        options.add_argument("--headless=new")

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()),
                              options=options)
    driver.maximize_window()
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return driver


def observe(driver, label: str, out_dir: str, nav_log: list) -> dict:
    """Record one page state: scrub, save, harvest, and note any block signal."""
    html = driver.page_source
    url = driver.current_url
    text_lower = ""
    try:
        text_lower = driver.find_element("tag name", "body").text.lower()[:20000]
    except Exception:
        pass

    blocked_url = [m for m in BLOCKED_URL_MARKERS if m in url]
    blocked_text = [m for m in BLOCKED_TEXT_MARKERS if m in text_lower]
    state, signals, evidence = login_state(driver)

    scrubbed = scrub_html(html)
    residue = pii_residue(scrubbed)
    path = os.path.join(out_dir, f"{label}.scrubbed.html")
    if residue:
        path = None
        print(f"    !! NOT SAVED — PII residue after scrubbing: {residue}")
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write(scrubbed)

    record = {
        "label": label,
        "url_after_nav": url,
        "raw_bytes": len(html),
        "scrubbed_bytes": len(scrubbed),
        "saved_to": path,
        "pii_residue": residue,
        "blocked_url_markers": blocked_url,
        "blocked_text_markers": blocked_text,
        "login_state": state,
        "login_evidence": evidence,
        "login_signals": signals,
        "testid_counts": testid_counts(scrubbed),
        "summary": summarize(scrubbed),
        "observed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    nav_log.append({"label": label, "url": url, "at": record["observed_at"],
                    "login_state": state})

    s = record["summary"]
    print(f"    url={url}")
    print(f"    login_state={state} {evidence}")
    print(f"    {record['raw_bytes']:,} raw -> {record['scrubbed_bytes']:,} scrubbed bytes")
    print(f"    data-testid: {s['data_testid_count']} distinct | "
          f"/status/ hrefs: {s['status_href_count']} | "
          f"button aria-labels: {len(s['button_aria_labels'])}")
    if blocked_url or blocked_text:
        print(f"    !! BLOCK SIGNAL url={blocked_url} text={blocked_text}")
    if state != "logged_in":
        print("    !! not authenticated — this page is the logged-out shell, "
              "its hooks are NOT the adapter's hooks")
    return record


def run_probe(profile_name: str, pages: list, scrolls: int, login_only: bool,
              headless: bool, extra_pages: dict = None,
              want_composer: bool = False) -> int:
    """Drive the live session and write the observation record. Returns exit code."""
    out_dir = pm.get_data_dir(profile_name, "x_spike")
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    nav_log, records = [], []

    driver = None
    try:
        driver = create_x_driver(profile_name, headless=headless)

        print("Navigating to https://x.com/home ...")
        driver.get("https://x.com/home")
        time.sleep(6)
        state, signals, evidence = login_state(driver)
        print(f"  landed on: {driver.current_url}")
        print(f"  login_state: {state}")
        print_signals(signals, evidence)

        if state != "logged_in":
            print("\nNot authenticated. Log in to the X TEST account in the open\n"
                  "window (complete any phone/email/captcha challenge), then press\n"
                  "Enter here. For a first login prefer:\n"
                  f"  python tools/x_dump.py --profile {profile_name} --manual-login")
            input("  press Enter when logged in... ")
            driver.get("https://x.com/home")
            time.sleep(6)
            state, signals, evidence = login_state(driver)
            print(f"  login_state: {state} {evidence}")

        if login_only:
            print(f"\n--login-only: login_state={state}. "
                  f"{'Session established.' if state == 'logged_in' else 'NOT established.'}")
            return EXIT_OK if state == "logged_in" else EXIT_NO_TEST_ACCOUNT

        if state != "logged_in":
            print("\nRefusing to dump: an unauthenticated page yields the logged-out\n"
                  "shell, whose hooks are not the adapter's hooks. Recon against it\n"
                  "would produce confident, wrong selectors — the exact failure this\n"
                  "spike exists to avoid.")
            return EXIT_NO_TEST_ACCOUNT

        for label in pages:
            target = dict(PAGES, **(extra_pages or {})).get(label)
            if target is None:
                print(f"\n[{label}] unknown page, skipping")
                continue
            print(f"\n[{label}] -> {target}")
            driver.get(target)
            time.sleep(6)
            records.append(observe(driver, label, out_dir, nav_log))

            # Q7 — virtualization: are earlier cards still in the DOM after scrolling?
            if scrolls and label.startswith("timeline"):
                for i in range(scrolls):
                    driver.execute_script("window.scrollBy(0, 1200);")
                    time.sleep(2.5)
                print(f"  after {scrolls} scrolls:")
                records.append(observe(driver, f"{label}_scrolled", out_dir, nav_log))

        # Q6 — the reply composer, and Q8 — the character counter. Driven by
        # --composer rather than by which page happened to be listed: the
        # composer that matters for Phase D opens on a STATUS page, not home.
        if want_composer:
            print("\n[composer] opening the reply composer on the first card")
            print("  MANUAL STEP: click reply on a post in the open window, then press Enter.")
            input("  press Enter with the composer open (or just Enter to skip)... ")
            records.append(observe(driver, "composer_open", out_dir, nav_log))

        report = {
            "spike": "x-spike Probe 1",
            "profile": profile_name,
            "session_dir": x_session_dir(profile_name),
            "started": stamp,
            "navigations": nav_log,
            "navigation_count": len(nav_log),
            "records": records,
        }
        # Every URL in the record is redacted on the way to disk. The console
        # above showed the live ones — that is the operator watching their own
        # session — but the FILE is the thing that outlives the run.
        report = scrub_for_disk(report)
        left = disk_pii_residue(report)
        if left:
            print(f"    !! residue after disk scrubbing: {left}")
        report_path = os.path.join(out_dir, f"x_probe_{stamp}.json")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print(f"\nWrote {report_path}")
        print(f"Scrubbed dumps in {out_dir} (data/ is gitignored)")
        return EXIT_OK

    except Exception as e:
        print(f"ERROR during probe: {type(e).__name__}: {e}")
        return EXIT_ERROR
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def run_signals_only(profile_name: str, url: str, headless: bool) -> int:
    """Load one URL, print the signal vector, quit. Non-interactive.

    The cheap half of the discrimination experiment: it captures whichever state
    the session is currently in without touching the account. Run it before a
    manual login to bank a logged-out baseline, and again afterwards.
    """
    out_dir = pm.get_data_dir(profile_name, "x_spike")
    os.makedirs(out_dir, exist_ok=True)
    driver = None
    try:
        driver = create_x_driver(profile_name, headless=headless)
        print(f"Navigating to {url} ...")
        driver.get(url)
        time.sleep(6)
        state, signals, evidence = login_state(driver)
        print(f"  landed on: {driver.current_url}")
        print(f"  login_state: {state}\n")
        print_signals(signals, evidence)

        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(out_dir, f"x_signals_{state}_{stamp}.json")
        payload = scrub_for_disk({"url": url, "state": state,
                                   "evidence": evidence, "signals": signals,
                                   "at": stamp})
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"\nWrote {path}")
        print("  (bank this as the baseline for the other state, then compare with"
              " --manual-login)")
        return EXIT_OK
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        return EXIT_ERROR
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def run_manual_login(profile_name: str, headless: bool = False) -> int:
    """Human-in-the-loop first login, then verify it took and that it persists.

    Automated login on a fresh X account is not a realistic path — X challenges
    new accounts with phone/email/captcha steps that a script cannot clear, and
    trying would look exactly like the automation X is watching for. So this
    mirrors LinkedIn's first-login flow: open the persistent-session window,
    stand back, and let a human log in.

    Three things are measured, in order, and each is a finding:

    1. the **logged-out** signal vector, banked *before* the login,
    2. the **logged-in** vector afterwards → their difference is the answer to
       "which signal actually discriminates",
    3. whether the session survives a **full Chrome restart** — the driver is
       quit and a second one launched against the same ``user-data-dir``. That
       is the real persistence question, and it cannot be answered without
       actually tearing the browser down.
    """
    out_dir = pm.get_data_dir(profile_name, "x_spike")
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    session_dir = x_session_dir(profile_name)
    record = {"spike": "x-spike manual login", "profile": profile_name,
              "session_dir": session_dir, "started": stamp}

    driver = None
    try:
        # ── 1. baseline: whatever state the persistent session is in now ─────
        print("=" * 68)
        print("STEP 1 — baseline (before you log in)")
        print("=" * 68)
        driver = create_x_driver(profile_name, headless=headless)
        driver.get("https://x.com/home")
        time.sleep(6)
        before_state, before, before_ev = login_state(driver)
        print(f"  url:         {driver.current_url}")
        print(f"  login_state: {before_state}")
        print_signals(before, before_ev)
        record["before"] = {"state": before_state, "evidence": before_ev,
                            "signals": before, "url": before.get("_url")}

        if before_state == "logged_in":
            print("\n  Already authenticated — nothing to do. (No logged-out baseline\n"
                  "  from this run, so signal discrimination cannot be computed here;\n"
                  "  run --signals-only from a fresh session dir for that.)")

        # ── 2. the human logs in ─────────────────────────────────────────────
        print("\n" + "=" * 68)
        print("STEP 2 — log in by hand, in the Chrome window that is open")
        print("=" * 68)
        driver.get("https://x.com/i/flow/login")
        time.sleep(4)
        print(f"  login page: {driver.current_url}")
        print("\n  Log in to the X TEST account now. Complete any phone / email /\n"
              "  captcha challenge X throws at a fresh account. Take as long as you\n"
              "  need — nothing is timing you.\n")
        print("  Do NOT close the Chrome window; this script owns it.\n")
        input("  press Enter when you are logged in and looking at the timeline... ")

        # ── 3. verify with the fixed detection ───────────────────────────────
        print("\n" + "=" * 68)
        print("STEP 3 — verify (DOM + cookies, never the URL alone)")
        print("=" * 68)
        driver.get("https://x.com/home")
        time.sleep(6)
        after_state, after, after_ev = login_state(driver)
        print(f"  url:         {driver.current_url}")
        print(f"  login_state: {after_state}")
        print_signals(after, after_ev)
        record["after"] = {"state": after_state, "evidence": after_ev,
                           "signals": after, "url": after.get("_url")}

        # THE finding: what actually separates the two states.
        if before_state == "logged_out" and after_state == "logged_in":
            disc = discriminating_signals(before, after)
            record["discriminating_signals"] = disc
            print("\n  SIGNALS THAT ACTUALLY DISCRIMINATE (observed, not assumed):")
            print(f"    authenticated-only : {disc['authenticated_only']}")
            print(f"    logged-out-only    : {disc['logged_out_only']}")
            print(f"    present in BOTH (useless): {disc['present_in_both_useless']}")
            same_url = (record["before"]["url"] or "").rstrip("/") == \
                       (record["after"]["url"] or "").rstrip("/")
            record["url_identical_in_both_states"] = same_url
            note = "  <- exactly why URL-only detection was fooled" if same_url else ""
            print(f"    URL identical in both states: {same_url}{note}")
        else:
            print(f"\n  No clean before/after pair ({before_state} -> {after_state}), "
                  f"so no discrimination table from this run.")

        if after_state != "logged_in":
            print("\n  LOGIN DID NOT TAKE. Not writing a session-persists result.")
            record["result"] = "login_failed_or_uncertain"
            return EXIT_NO_TEST_ACCOUNT

        # ── 4. persistence across a FULL Chrome restart ──────────────────────
        print("\n" + "=" * 68)
        print("STEP 4 — does the session survive a full Chrome restart?")
        print("=" * 68)
        print("  Quitting Chrome entirely...")
        driver.quit()
        driver = None
        time.sleep(3)

        print("  Relaunching against the same user-data-dir...")
        driver = create_x_driver(profile_name, headless=headless)
        driver.get("https://x.com/home")
        time.sleep(7)
        relaunch_state, relaunch, relaunch_ev = login_state(driver)
        print(f"  url:         {driver.current_url}")
        print(f"  login_state: {relaunch_state}")
        print_signals(relaunch, relaunch_ev)
        record["after_restart"] = {"state": relaunch_state, "evidence": relaunch_ev,
                                   "signals": relaunch}
        record["session_persists_across_restart"] = relaunch_state == "logged_in"

        print("\n" + "=" * 68)
        if relaunch_state == "logged_in":
            print("  RESULT: session PERSISTS across a full Chrome restart —\n"
                  "  same model as LinkedIn. Subsequent x_dump runs reuse it.")
            record["result"] = "persists"
        else:
            print(f"  RESULT: session did NOT survive the restart "
                  f"(state={relaunch_state}).\n"
                  "  X forces re-auth per launch. This is a MAJOR viability finding:\n"
                  "  every run would need a human, which rules out scheduled automation.")
            record["result"] = "does_not_persist"
        print("=" * 68)
        return EXIT_OK if relaunch_state == "logged_in" else EXIT_NO_TEST_ACCOUNT

    except Exception as e:
        print(f"ERROR during manual login: {type(e).__name__}: {e}")
        record["result"] = f"error: {type(e).__name__}: {e}"
        return EXIT_ERROR
    finally:
        path = os.path.join(out_dir, f"x_manual_login_{stamp}.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(scrub_for_disk(record), f, indent=2, ensure_ascii=False)
            print(f"\nWrote {path}")
        except Exception:
            pass
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


# ─── Selftest (offline, no browser, no account) ───────────────────────────────

# FABRICATED markup. This is NOT a claim about X's real DOM — it is a made-up
# document with the *kinds* of constructs a scrubber and a hook harvester must
# handle (a testid, an aria-labelled button, a /status/ permalink, a handle, an
# email, a script body full of payload). Its only job is to prove the offline
# pipeline works before anyone points it at a live page.
_SELFTEST_HTML = """
<html><head><title>Real Person (@realhandle) / X</title>
<script>window.__INITIAL_STATE__={"user":{"screen_name":"realhandle","email":"real.person@example.com","id":"1234567890123456789"}};</script>
</head><body>
<div data-testid="cellInnerDiv"><article data-testid="fabricatedCard" role="article">
  <a href="/realhandle" role="link"><span>Real Person</span></a>
  <a href="/realhandle/status/1846273618273618273"><time datetime="2026-08-01T10:00:00Z">Aug 1</time></a>
  <div data-testid="fabricatedText" lang="en">Contact me at real.person@example.com about @otherhandle</div>
  <button data-testid="reply" aria-label="12 Replies. Reply"><span>12</span></button>
  <button data-testid="like" aria-label="340 Likes. Like"></button>
  <img src="https://pbs.twimg.com/profile_images/1234567890/abc.jpg" alt="Real Person avatar">
</article></div>
<div data-testid="cellInnerDiv"><article data-testid="fabricatedCard" role="article">
  <a href="/secondperson/status/1846273618273699999"><time datetime="2026-08-02T10:00:00Z">Aug 2</time></a>
  <button data-testid="reply" aria-label="3 Replies. Reply"></button>
</article></div>
</body></html>
"""


def selftest() -> int:
    """Exercise scrub + residue + harvest offline. Returns an exit code."""
    results = []

    def check(label, ok, detail=""):
        results.append(ok)
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))

    print("x_dump selftest (offline — no browser, no account, fabricated HTML)\n")
    scrubbed = scrub_html(_SELFTEST_HTML)

    print("1. scrubbing")
    check("script body removed", "__INITIAL_STATE__" not in scrubbed)
    check("email removed", "real.person@example.com" not in scrubbed)
    check("handle removed", "realhandle" not in scrubbed)
    check("second handle removed", "otherhandle" not in scrubbed)
    check("post text removed", "Contact me at" not in scrubbed)
    check("display name removed", "Real Person" not in scrubbed)
    check("media URL removed", "pbs.twimg.com" not in scrubbed)
    check("real status id removed", "1846273618273618273" not in scrubbed)
    residue = pii_residue(scrubbed)
    # No IDENTITY residue. `unrecognized_testids` is expected here and is the
    # gate working: this document's hooks are named `fabricated*` precisely so
    # they are not in the safe list, and an unrecognized shape must block a real
    # write until a human triages it.
    check("no identity residue after scrubbing",
          set(residue) <= {"unrecognized_testids"}, str(residue))
    check("unknown testids are reported, not silently trusted",
          residue.get("unrecognized_testids") == ["fabricatedCard", "fabricatedText"],
          str(residue.get("unrecognized_testids")))

    print("\n2. structure survived (this is what makes the dump useful)")
    check("data-testid values kept", 'data-testid="cellInnerDiv"' in scrubbed)
    check("aria-label kept (redacted)", "aria-label" in scrubbed and "Reply" in scrubbed)
    hrefs = status_hrefs(scrubbed)
    check("permalink shape kept", "/status/" in scrubbed, str(hrefs))
    check("permalink id is synthetic and same length",
          all(len(h.split("/status/")[1]) == 19 for h in hrefs), str(hrefs))
    check("two cards keep two DISTINCT permalinks (redaction did not flatten them)",
          len(hrefs) == 2 and len(set(hrefs)) == 2, str(hrefs))
    check("second handle also synthetic", "secondperson" not in scrubbed)
    check("role attribute kept", 'role="article"' in scrubbed)

    print("\n3. dom_probe copes with the markup (the offline gate's engine)")
    hooks = dom_probe.harvest_hooks(scrubbed)
    check("harvest_hooks returns testids", len(hooks["data_testids"]) >= 4,
          str(hooks["data_testids"]))
    counts = testid_counts(scrubbed)
    check("testid_counts counts them", counts.get("reply") == 2, str(counts))
    check("candidate_selectors produced", len(dom_probe.candidate_selectors(hooks)) >= 4)
    try:
        count = dom_probe.make_counter(scrubbed)
        # The offline engine must agree with the harvester, or the selector gate
        # would report a different number than the recon did.
        check("make_counter agrees with testid_counts",
              count("[data-testid='reply']") == counts["reply"] == 2)
        check("make_counter runs a descendant selector",
              count("article button[data-testid='like']") == 1)
        check("make_counter runs an href-contains selector",
              count("a[href*='/status/']") == 2)
    except dom_probe.UnsupportedSelector as e:
        check("make_counter handles the markup", False, str(e))

    print("\n4. login classifier (the bug that started this)")
    # The exact vector that fooled the old check: landed on x.com/ with no
    # logged-out URL marker, but nothing authenticated is on the page.
    logged_out_vec = {
        "_url": "https://x.com/", "login_button": 1, "href_flow_login": 2,
        "href_flow_signup": 1, "cookie_auth_token": False, "cookie_guest_id": True,
        "article_cards": 3, "primary_column": 1, "nav_role_navigation": 1,
        "text_sign_in": True,
    }
    logged_in_vec = {
        "_url": "https://x.com/home", "sidenav_new_tweet": 1, "apptabbar_home": 1,
        "href_messages": 1, "cookie_auth_token": True, "cookie_ct0": True,
        "article_cards": 12, "primary_column": 1, "nav_role_navigation": 1,
    }
    state_out, ev_out = classify_login(logged_out_vec)
    state_in, ev_in = classify_login(logged_in_vec)
    check("a logged-out page classifies as logged_out", state_out == "logged_out",
          f"{state_out} {ev_out}")
    check("a logged-in page classifies as logged_in", state_in == "logged_in",
          f"{state_in} {ev_in}")
    check("URL alone would NOT have separated them (the original bug)",
          "x.com" in logged_out_vec["_url"] and "x.com" in logged_in_vec["_url"]
          and not any(m in logged_out_vec["_url"] for m in BLOCKED_URL_MARKERS),
          "both are x.com URLs with no logged-out marker")
    check("an empty page is uncertain, never a confident logged_in",
          classify_login({})[0] == "uncertain")
    check("conflicting signals are uncertain, not a coin flip",
          classify_login({"cookie_auth_token": True, "login_button": 1})[0]
          == "uncertain")
    check("no single hook is load-bearing (one testid alone suffices)",
          classify_login({"sidenav_new_tweet": 1})[0] == "logged_in"
          and classify_login({"cookie_auth_token": True})[0] == "logged_in")
    # The live logged-out vector observed 2026-08-15: every DOM candidate read 0,
    # only the cookie jar told the truth.
    cookie_only_vec = {"_testid_elements": 0, "_body_text_len": 1200,
                       "cookie_auth_token": False, "cookie_gt": True,
                       "cookie_guest_id": True}
    state_cookie, ev_cookie = classify_login(cookie_only_vec)
    check("cookies alone resolve logged_out when no DOM candidate fires",
          state_cookie == "logged_out" and ev_cookie.get("inferred_from") == "cookies",
          f"{state_cookie} {ev_cookie}")
    check("the cookie inference does NOT fire on an unrendered page",
          classify_login({"_testid_elements": 0, "_body_text_len": 0,
                          "cookie_auth_token": False, "cookie_gt": True})[0]
          == "uncertain")

    disc = discriminating_signals(logged_out_vec, logged_in_vec)
    check("discrimination table finds the auth-only signals",
          "cookie_auth_token" in disc["authenticated_only"]
          and "sidenav_new_tweet" in disc["authenticated_only"], str(disc))
    check("discrimination table calls out signals present in BOTH as useless",
          set(disc["present_in_both_useless"]) >= {"article_cards", "primary_column",
                                                   "nav_role_navigation"}, str(disc))

    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} checks passed")
    return EXIT_ERROR if failed else EXIT_OK


def verify_dump(path: str) -> int:
    """Re-run the PII gate over a saved dump and list every testid for a human.

    B.0's own principle: a mechanical scan is only as good as its allowlist. So
    this prints what the classifier DECIDED, grouped, rather than just a verdict
    — a real handle that the allowlist happened to accept is visible here even
    though the gate passed it.
    """
    try:
        with open(path, encoding="utf-8") as f:
            html = f.read()
    except OSError as e:
        print(f"cannot read {path}: {e}")
        return EXIT_ERROR

    residue = pii_residue(html)
    counts = testid_counts(html)
    groups = {}
    for value in counts:
        status, _ = classify_testid(value)
        groups.setdefault(status, []).append(value)

    print(f"{path}\n{'=' * 70}")
    print(f"{len(html):,} chars | {len(counts)} distinct data-testid values\n")
    for status in ("handle_stem", "embedded_id", "unknown", "safe"):
        vals = sorted(groups.get(status, []))
        if not vals:
            continue
        label = {"handle_stem": "SANITIZED (stem kept, identity synthesized)",
                 "embedded_id": "SANITIZED (embedded id)",
                 "unknown": "UNRECOGNIZED — read these yourself",
                 "safe": "structural"}[status]
        print(f"[{status}] {label} — {len(vals)}")
        for v in vals:
            print(f"    {v}")
        print()
    print("=" * 70)
    if residue:
        print(f"GATE: FAIL — {residue}")
        print("This file must NOT become a fixture until this is empty.")
        return EXIT_ERROR
    print("GATE: pass — no identifying residue found.")
    print("Now read the [unknown] and [handle_stem] lists above by eye: the scan")
    print("is only as good as its allowlist, and a handle it does not recognize")
    print("as a handle is exactly what it would miss.")
    return EXIT_OK


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    """Parse args and dispatch. Returns the process exit code."""
    parser = argparse.ArgumentParser(description="X live-DOM recon probe (spike)")
    parser.add_argument("--profile", default=None, help="X TEST profile name")
    parser.add_argument("--pages", default="timeline_foryou,search",
                        help=f"comma-separated: {','.join(PAGES)}")
    parser.add_argument("--scroll", type=int, default=3, help="scrolls per timeline")
    parser.add_argument("--search-url", default=None,
                        help="a search results URL to dump. The finder scrapes "
                             "YOUR keywords, so capture the real query, not the "
                             "built-in placeholder.")
    parser.add_argument("--composer", action="store_true",
                        help="pause after the last page so you can open the "
                             "reply composer by hand, then dump it")
    parser.add_argument("--verify-dump", default=None, metavar="FILE",
                        help="re-run the PII gate over a SAVED dump and list "
                             "every data-testid value for a human read")
    parser.add_argument("--status-url", default=None,
                        help="a tweet permalink to dump (settles the permalink "
                             "question, ROADMAP B.1). Adds the 'status' page.")
    parser.add_argument("--profile-url", default=None,
                        help="a profile page to dump (the follow button lives "
                             "here). Adds the 'profile' page.")
    parser.add_argument("--login-only", action="store_true",
                        help="check the persistent session and exit")
    parser.add_argument("--manual-login", action="store_true",
                        help="human-in-the-loop first login, then verify it took "
                             "and test whether it survives a Chrome restart")
    parser.add_argument("--signals-only", action="store_true",
                        help="load one URL, print the login-signal vector, quit "
                             "(non-interactive; banks a baseline for comparison)")
    parser.add_argument("--url", default="https://x.com/home",
                        help="URL for --signals-only")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--preflight", action="store_true",
                        help="report whether a live run is possible; touch nothing")
    parser.add_argument("--selftest", action="store_true",
                        help="offline check of the scrub/harvest pipeline")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    if args.verify_dump:
        return verify_dump(args.verify_dump)

    if args.preflight:
        message, code = preflight_report(args.profile)
        print(message)
        return code

    # The account guard gates EVERY live mode, --manual-login included. A mode
    # that opens a browser against a profile is a mode that must pass it.
    name, info = resolve_x_test_profile(args.profile)
    if name is None:
        message, code = preflight_report(args.profile)
        print(message)
        return code

    if args.manual_login:
        return run_manual_login(name, headless=args.headless)

    if args.signals_only:
        return run_signals_only(name, args.url, args.headless)

    pages = [p.strip() for p in args.pages.split(",") if p.strip()]
    extra = {}
    if args.search_url:
        extra["search"] = args.search_url
        if "search" not in pages:
            pages.append("search")
    if args.status_url:
        extra["status"] = args.status_url
        pages.append("status")
    if args.profile_url:
        extra["profile"] = args.profile_url
        pages.append("profile")

    return run_probe(name, pages, args.scroll, args.login_only, args.headless,
                     extra_pages=extra, want_composer=args.composer)


if __name__ == "__main__":
    sys.exit(main())
