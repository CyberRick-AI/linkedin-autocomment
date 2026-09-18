"""X (twitter.com) selector constants, harvested from live captures.

The X half of what :class:`~linkedin_automation.post_finder.LinkedInScraper` and
friends are for LinkedIn: the single place the selectors live, so
:mod:`selector_health` can register them and the X finder — when it lands — can
import the same names rather than growing a second copy that drifts.

Every selector here was **observed in a live capture**, not guessed. The dumps
behind them are the four scrubbed pages under ``data/<profile>/x_spike/``
(gitignored, machine-local) and the probe records beside them; the facts they
proved are summarised in ``.dev/AUDIT_x_harvest_state.md``. Where a hook was
seen on some pages and not others, the docstring says which — that asymmetry is
what the registry's ``page`` keys encode.

Provenance, per page (distinct ``data-testid`` values seen):

===========================  =======  =========================================
Capture                      testids  Notes
===========================  =======  =========================================
``timeline_foryou``               81  home timeline, no interaction
``timeline_foryou_scrolled``      90  after 3 scrolls — proves virtualization
``status``                        58  a permalink page, composer untouched
``composer_open``                 58  the same page after clicking the reply box
``search``                        69  ``?q=…&f=live``
===========================  =======  =========================================

Two findings shape everything below.

**Permalinks are plain hrefs.** X puts every card's permalink in an ordinary
anchor (``/<handle>/status/<id>``), 7–18 per timeline load with no interaction
at all. LinkedIn needs the overflow menu opened and the clipboard read; X needs
neither, so there is no menu-gated entry on this side.

**The status page's composer needs no click.** ``status`` and ``composer_open``
captured *identical* structural testid sets: ``tweetTextarea_0`` and
``tweetButtonInline`` are already in the DOM when the permalink finishes
loading. Nothing here is gated behind an interaction, which is why this module
declares no witness selectors — LinkedIn's ``requires_comment_box`` has no X
counterpart to mirror.
"""

# ─── Card hooks: identical on the timeline, a status page, and search ─────────
#
# Confirmed on all five captures. The search results page renders the same card
# shape as the timeline — no search-specific card — so the finder needs one card
# parser, not two.

#: The virtualized row wrapper. Rows leave the DOM as they scroll out (81 -> 90
#: distinct testids and 9 -> 18 status hrefs across the scroll), so a harvester
#: must read as it scrolls rather than once at the end.
TWEET_ROW_SELECTORS = ("[data-testid='cellInnerDiv']",)

#: The card itself.
TWEET_CARD_SELECTORS = ("[data-testid='tweet']",)

#: Body text.
TWEET_TEXT_SELECTORS = ("[data-testid='tweetText']",)

#: Display name + handle block.
AUTHOR_SELECTORS = ("[data-testid='User-Name']",)

#: The author's avatar, inside the card.
AVATAR_SELECTORS = ("[data-testid='Tweet-User-Avatar']",)

#: The per-card overflow menu. Registered because the watchdog should notice it
#: dying, NOT because the permalink needs it — see PERMALINK_SELECTORS.
OVERFLOW_MENU_SELECTORS = ("[data-testid='caret']",)

# ─── The action row ───────────────────────────────────────────────────────────

REPLY_BUTTON_SELECTORS = ("[data-testid='reply']",)
LIKE_BUTTON_SELECTORS = ("[data-testid='like']",)
RETWEET_BUTTON_SELECTORS = ("[data-testid='retweet']",)
BOOKMARK_BUTTON_SELECTORS = ("[data-testid='bookmark']",)

# ─── The permalink: the single most consequential finding of the harvest ──────

#: Every card carries its permalink as a plain anchor. The ``/analytics``
#: variant sits beside it on the author's own posts, so a caller that wants the
#: canonical URL must drop the suffix rather than assume one anchor per card.
#:
#: This is why no X equivalent of LinkedIn's copy-link machinery exists: there
#: is no menu to open and no clipboard to read.
PERMALINK_SELECTORS = ("a[href*='/status/']",)

# ─── Page chrome ──────────────────────────────────────────────────────────────

#: The scrolling column. X virtualizes inside this, so it is the scroll target.
PRIMARY_COLUMN_SELECTORS = ("[data-testid='primaryColumn']",)

#: Opens the standalone (non-reply) composer. Present on every captured page.
NEW_TWEET_BUTTON_SELECTORS = ("[data-testid='SideNav_NewTweet_Button']",)

# ─── The status page (a permalink) ────────────────────────────────────────────

#: The status-page header's back control. Also present on search, so it is NOT
#: proof that a status page loaded — it is registered for the watchdog, not used
#: as a witness.
STATUS_BACK_SELECTORS = ("[data-testid='app-bar-back']",)

#: The offscreen wrapper around the inline reply composer. Seen ONLY on the two
#: status captures, never on the timeline or search, which makes it the one
#: hook that distinguishes "this is a status page" from "this is a feed".
STATUS_WITNESS_SELECTORS = ("[data-testid='inline_reply_offscreen']",)

#: The reply editor.
#:
#: **Scope this to the status page.** ``tweetTextarea_0`` is also present on the
#: TIMELINE (X mounts an inline composer there too), so a bare document-wide
#: lookup will match on a page where replying is not what the caller meant. The
#: registry files this under ``page="x_status"`` for that reason, and
#: ``test_x_selector_registry.py`` pins the overlap so a future finder cannot
#: quietly assume a single match.
REPLY_EDITOR_SELECTORS = (
    "[data-testid='tweetTextarea_0']",
    "[data-testid='tweetTextarea_0RichTextInputContainer']",
)

#: The reply submit button. Named ``…Inline`` because it is the status page's
#: inline composer; the standalone composer's button was never captured.
REPLY_SUBMIT_SELECTORS = ("[data-testid='tweetButtonInline']",)

# ─── Search ───────────────────────────────────────────────────────────────────

SEARCH_INPUT_SELECTORS = ("[data-testid='SearchBox_Search_Input']",)

#: Search's own chrome — the filter rail and the overflow control beside the
#: box. Non-critical: losing them costs filtering, not results.
SEARCH_FILTER_SELECTORS = (
    "[data-testid='searchFiltersAdvancedSearch']",
    "[data-testid='searchBoxOverflowButton']",
)

# ─── Follow / unfollow: an identity-suffixed testid ───────────────────────────
#
# X names these ``<account-id>-follow`` / ``<account-id>-unfollow``, so there is
# no fixed string to match — the selector has to match the SUFFIX. Both were
# observed in the timeline and search sidebars ("Who to follow", trend cells).
#
# THE PROFILE PAGE HAS NEVER BEEN DUMPED. The follow button on a profile is
# presumed to share this shape, but that is an inference, not an observation,
# so these are registered non-critical and scoped to the pages where they were
# actually seen. Do not read a passing check here as profile-page coverage.

FOLLOW_BUTTON_SELECTORS = ("[data-testid$='-follow']",)
UNFOLLOW_BUTTON_SELECTORS = ("[data-testid$='-unfollow']",)


# ─── Deliberately NOT registered ──────────────────────────────────────────────
#
# ``progressBar-bar`` — present on the composer captures, but never tied to the
# character counter (open question Q8 in the harvest notes). Registering it
# would assert a meaning the capture does not support. The X register's
# character limits live in :mod:`platform_policy` and are enforced on the text,
# not read off the page, so nothing depends on it yet.
#
# ``UserAvatar-Container-<handle>`` — carries identity in the testid itself.
# Useful to the scrubber (which synthesizes the tail) and useless as a selector,
# since the handle is exactly the part that varies.
