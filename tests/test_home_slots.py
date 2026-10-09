"""The Home queue ring reads Buffer's real slot count, drain on or off.

**The bug this exists for.** The ring borrowed the post drain's status, which
only asks Buffer for slots while the drain is switched on. With it off - the
default - the ring sat empty and said "Buffer limit unknown", although the
profile had a channel and a key and Buffer would happily have answered.
"""

import pytest

from linkedin_automation import buffer_client as bc
from linkedin_automation import dashboard


@pytest.fixture
def client():
    dashboard.app.config.update(TESTING=True)
    return dashboard.app.test_client()


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(dashboard, "_scheduled_channel_id",
                        lambda profile, platform="linkedin": "chan-" + platform)
    monkeypatch.setattr(dashboard.pm, "resolve_scheduled",
                        lambda profile, platform: {"api_key": "k-" + platform})


def test_slots_are_read_live_whatever_the_drain_says(client, wired, monkeypatch):
    seen = []

    def fake_slots(channel_id, key=None, **kw):
        seen.append((channel_id, key))
        return {"limit": 10, "used": 6, "used_this_channel": 3, "free": 4}

    monkeypatch.setattr(bc, "scheduled_slots", fake_slots)
    res = client.get("/api/scheduled/Rick/slots")
    assert res.status_code == 200
    body = res.get_json()
    assert body["slots"] == {"limit": 10, "used": 6, "used_this_channel": 3, "free": 4}
    assert seen == [("chan-linkedin", "k-linkedin")]


def test_falls_back_to_x_when_linkedin_has_no_channel(client, monkeypatch):
    monkeypatch.setattr(dashboard, "_scheduled_channel_id",
                        lambda profile, platform="linkedin": "chan-x" if platform == "x" else "")
    monkeypatch.setattr(dashboard.pm, "resolve_scheduled",
                        lambda profile, platform: {"api_key": "k-" + platform})
    monkeypatch.setattr(bc, "scheduled_slots",
                        lambda channel_id, key=None, **kw: {"limit": 10, "used": 1, "free": 9,
                                                            "used_this_channel": 1})
    body = client.get("/api/scheduled/Rick/slots").get_json()
    assert body["slots"]["used"] == 1
    assert body["platform"] == "x"


def test_no_channel_anywhere_is_an_answer_not_an_error(client, monkeypatch):
    monkeypatch.setattr(dashboard, "_scheduled_channel_id",
                        lambda profile, platform="linkedin": "")
    body = client.get("/api/scheduled/Rick/slots").get_json()
    assert body["slots"] is None
    assert "channel" in body["reason"].lower()


def test_a_buffer_failure_is_reported_not_raised(client, wired, monkeypatch):
    def boom(*a, **kw):
        raise bc.BufferError("down")
    monkeypatch.setattr(bc, "scheduled_slots", boom)
    res = client.get("/api/scheduled/Rick/slots")
    assert res.status_code == 200
    assert res.get_json()["slots"] is None


def test_the_home_ring_uses_the_live_endpoint():
    html = open(dashboard.app.root_path + "/templates/dashboard.html").read()
    assert "/slots'" in html or '/slots"' in html
    assert "until the drain is on" not in html
