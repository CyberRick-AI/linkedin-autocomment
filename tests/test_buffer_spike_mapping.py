"""Spike-level checks on the pure parts of tools/buffer_spike.py.

Only the functions that need no network: the CSV -> createPost mapping and the
input builder. The spike's real verdict comes from the live API and the human
gate, not from here - this just stops the mapping from quietly drifting, and
pins the finding that matters most for the CSV design.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.buffer_spike import build_input, csv_row_to_input  # noqa: E402


ROW = {
    "date": "2026-09-08",
    "time_window": "09:00-11:00",
    "post_text": "Testing an automated scheduled post.",
    "topic": "automation",
    "tags": "#automation #python",
    "image_path": "S:/new_comp/test_image.png",
    "first_comment_link": "https://example.com/the-link",
}


def test_all_four_things_ride_on_one_call():
    """text, image, dueAt and firstComment together - the whole spike question."""
    payload = build_input("chan1", "hello #automation",
                          "https://cdn.example.com/x.png",
                          "2026-09-08T09:00:00.000Z", "first comment here")
    assert payload["channelId"] == "chan1"
    assert payload["text"] == "hello #automation"
    assert payload["dueAt"] == "2026-09-08T09:00:00.000Z"
    assert payload["assets"] == [{"image": {"url": "https://cdn.example.com/x.png"}}]
    assert payload["metadata"]["linkedin"]["firstComment"] == "first comment here"


def test_the_required_scalars_match_the_live_schema():
    """Four of these were wrong on the first attempt, and each would have failed
    createPost exactly the way String!/OrganizationId! failed GetChannels."""
    payload = build_input("chan1", "hi", None, "2026-09-08T09:00:00.000Z", None)
    # assets is [AssetInput!]! - required and non-null, so a text-only post
    # sends an empty list rather than omitting the key.
    assert payload["assets"] == []
    # mode is ShareMode!, and customScheduled is the member that honours dueAt.
    assert payload["mode"] == "customScheduled"
    # schedulingType is SchedulingType! whose members are automatic/notification.
    # There is no "custom" member - scheduling is chosen by mode.
    assert payload["schedulingType"] == "automatic"
    # needsApproval is Boolean! with no default.
    assert payload["needsApproval"] is False


def test_metadata_uses_the_lowercase_linkedin_key():
    """PostInputMetaData spells it `linkedin`. `linkedIn` is not a field, and
    would have been rejected as unknown."""
    payload = build_input("c", "t", None, None, "a comment")
    assert "linkedin" in payload["metadata"]
    assert "linkedIn" not in payload["metadata"]


def test_no_due_at_falls_back_to_the_queue():
    payload = build_input("chan1", "hi", None, None, None)
    assert payload["mode"] == "addToQueue"
    assert "dueAt" not in payload
    assert "metadata" not in payload


def test_the_csv_row_maps_date_and_window_onto_dueAt():
    payload, _ = csv_row_to_input(ROW, "chan1",
                                  image_url_override="https://cdn.example.com/x.png")
    assert payload["dueAt"] == "2026-09-08T09:00:00.000Z"   # window START
    assert payload["metadata"]["linkedin"]["firstComment"] == ROW["first_comment_link"]


def test_tags_are_appended_under_a_blank_line():
    payload, _ = csv_row_to_input(ROW, "chan1", "https://cdn.example.com/x.png")
    assert payload["text"].startswith("Testing an automated scheduled post.")
    assert payload["text"].endswith("#automation #python")
    assert "\n\n" in payload["text"]


def test_a_local_image_path_cannot_satisfy_buffer_and_says_so():
    """THE finding. Buffer takes a public URL; the CSV carries a local path, so
    the mapping is incomplete by design and must not pretend otherwise."""
    payload, notes = csv_row_to_input(ROW, "chan1")      # no URL override
    assert payload["assets"] == [], "a local path must never be sent as a URL"
    assert notes, "the gap has to be reported, not silently dropped"
    assert "LOCAL PATH" in notes[0]
    assert "without its image" in notes[0].lower()


def test_supplying_a_hosted_url_completes_the_mapping():
    payload, notes = csv_row_to_input(ROW, "chan1", "https://cdn.example.com/x.png")
    assert payload["assets"] == [{"image": {"url": "https://cdn.example.com/x.png"}}]
    assert notes == []


# ─── Phase 1: targeting a service other than LinkedIn ────────────────────────
#
# Still no network. These pin the two decisions that let the same spike drive an
# X channel: which channel it picks, and what metadata it is allowed to attach.

from tools.buffer_spike import SERVICE_ALIASES, discover  # noqa: E402


def _pick(channels, service):
    """Run discover's matcher over a channel list without touching the API."""
    wanted = SERVICE_ALIASES.get((service or "").lower(), ((service or "").lower(),))
    return [c for c in channels
            if str(c.get("service", "")).lower().startswith(wanted)]


CHANNELS = [
    {"name": "li", "service": "linkedin", "id": "c_li"},
    {"name": "x", "service": "twitter", "id": "c_x"},
]


def test_x_matches_buffers_old_twitter_service_name():
    """Buffer never renamed it: the schema still says Twitter, not X.

    Matching on "x" alone would report "no X channel" against an account that
    has one, which is the kind of wrong answer that sends someone to Buffer's UI
    looking for a bug that is in this file.
    """
    assert [c["id"] for c in _pick(CHANNELS, "x")] == ["c_x"]
    assert [c["id"] for c in _pick(CHANNELS, "twitter")] == ["c_x"]


def test_linkedin_is_unaffected_and_is_still_the_default():
    assert [c["id"] for c in _pick(CHANNELS, "linkedin")] == ["c_li"]
    assert SERVICE_ALIASES["linkedin"] == ("linkedin",)
    import inspect
    assert inspect.signature(discover).parameters["service"].default == "linkedin"


def test_an_unknown_service_matches_on_its_own_name_rather_than_everything():
    """A typo must find nothing, not silently pick the first channel."""
    assert _pick(CHANNELS, "mastodon") == []


def test_a_linkedin_post_still_carries_first_comment():
    """The existing behaviour, unchanged — this is the LinkedIn path."""
    payload = build_input("c_li", "hello", None, "2026-09-19T12:00:00.000Z",
                          "https://example.com/link")
    assert payload["metadata"] == {"linkedin": {"firstComment":
                                                "https://example.com/link"}}


def test_an_x_post_never_carries_linkedin_metadata():
    """Introspection is unambiguous: TwitterPostMetadataInput has no
    firstComment. Its self-reply seam is `thread`, a whole post rather than a
    string (see .dev/FINDING_x_buffer_thread.md).

    Sending {"linkedin": {...}} on an X post is a schema-valid lie: the block
    would be accepted and ignored, or rejected outright. Either way the caller
    would believe a first comment had been scheduled.
    """
    payload = build_input("c_x", "hello", None, "2026-09-19T12:00:00.000Z",
                          "https://example.com/link", service="x")
    assert "metadata" not in payload


def test_the_x_post_still_carries_text_image_and_due_at():
    """Dropping the metadata must not drop the post."""
    payload = build_input("c_x", "hello", "https://img.example/i.png",
                          "2026-09-19T12:00:00.000Z", None, service="x")
    assert payload["channelId"] == "c_x"
    assert payload["text"] == "hello"
    assert payload["assets"] == [{"image": {"url": "https://img.example/i.png"}}]
    assert payload["dueAt"] == "2026-09-19T12:00:00.000Z"
    assert payload["mode"] == "customScheduled"
