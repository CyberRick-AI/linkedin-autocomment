"""End-to-end pipeline-flow tests: save → archive → post file discovery
(ROADMAP Phase 2). Uses the Flask test client; no Selenium, no OpenAI."""

import glob
import json
import os

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import dashboard as linkedin_dashboard
from linkedin_automation.comment_poster import LinkedInCommentPoster

URL = "https://www.linkedin.com/feed/update/urn:li:activity:{}/"


def _generator_comment(i):
    return {
        "post_url": URL.format(i),
        "post_author": f"Author {i}",
        "post_text": f"Interesting post body {i}.",
        "post_category": "AI",
        "comment": f"Great point {i} here, thanks.",
        "word_count": 5,
        "style": "thoughtful",
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Flask test client with the profile data dir redirected into tmp."""
    comments_dir = tmp_path / "quality_comments"
    comments_dir.mkdir()
    monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None: str(comments_dir))
    monkeypatch.setattr(
        pm, "get_progress_file",
        lambda profile_name=None: str(comments_dir / "posting_progress.json"),
    )
    linkedin_dashboard.app.config.update(TESTING=True)
    return linkedin_dashboard.app.test_client(), comments_dir


def _seed_review_file(comments_dir, comments):
    """Write a generator-style comments_*.json so save() has originals to archive."""
    path = comments_dir / "comments_20260624_000000.json"
    path.write_text(json.dumps({"comments": comments}), encoding="utf-8")
    return path


# ─── Save endpoint ────────────────────────────────────────────────────────────

def test_save_writes_both_files_and_count(client):
    c, comments_dir = client
    comments = [_generator_comment(i) for i in range(1, 4)]
    _seed_review_file(comments_dir, comments)

    resp = c.post("/api/comments/jeff/save", json={"comments": comments})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["count"] == 3
    assert glob.glob(str(comments_dir / "ready_*.json"))
    assert glob.glob(str(comments_dir / "daily_comments_curated_*.txt"))


def test_saved_txt_parses_to_three(client):
    c, comments_dir = client
    comments = [_generator_comment(i) for i in range(1, 4)]
    _seed_review_file(comments_dir, comments)

    c.post("/api/comments/jeff/save", json={"comments": comments})
    txt = glob.glob(str(comments_dir / "daily_comments_curated_*.txt"))[0]
    parsed = LinkedInCommentPoster.parse_comments_file(txt)
    assert len(parsed) == 3
    assert [p["url"] for p in parsed] == [URL.format(i) for i in range(1, 4)]


def test_originals_archived_after_save(client):
    c, comments_dir = client
    comments = [_generator_comment(i) for i in range(1, 4)]
    _seed_review_file(comments_dir, comments)

    c.post("/api/comments/jeff/save", json={"comments": comments})
    # No comments_*.json left in the main dir...
    assert not glob.glob(str(comments_dir / "comments_*.json"))
    # ...but present under archived/.
    assert glob.glob(str(comments_dir / "archived" / "comments_*.json"))


def test_review_empty_after_save(client):
    """get_comments globs comments_*.json; once archived, Review is empty."""
    c, comments_dir = client
    comments = [_generator_comment(i) for i in range(1, 4)]
    _seed_review_file(comments_dir, comments)

    c.post("/api/comments/jeff/save", json={"comments": comments})
    resp = c.get("/api/comments/jeff")
    assert resp.status_code == 200
    assert resp.get_json()["comments"] == []


# ─── get_comments filtering ───────────────────────────────────────────────────

def test_get_comments_filters_already_posted(client):
    c, comments_dir = client
    comments = [_generator_comment(i) for i in range(1, 4)]
    _seed_review_file(comments_dir, comments)
    # Mark comment 2 as already posted.
    (comments_dir / "posting_progress.json").write_text(
        json.dumps({"posted_comments": [URL.format(2)]}), encoding="utf-8"
    )
    resp = c.get("/api/comments/jeff")
    urls = [normalized_url(x) for x in resp.get_json()["comments"]]
    assert URL.format(2) not in urls
    assert len(resp.get_json()["comments"]) == 2


def normalized_url(comment):
    return comment.get("post_url") or comment.get("url")


# ─── Post endpoint file discovery ─────────────────────────────────────────────

def test_post_finds_latest_txt_without_explicit_file(client, monkeypatch):
    """Page-refresh case: no comments_file supplied → poster falls back to the
    latest daily_comments_*.txt. We stub run_job so no browser launches."""
    c, comments_dir = client
    comments = [_generator_comment(i) for i in range(1, 4)]
    _seed_review_file(comments_dir, comments)
    c.post("/api/comments/jeff/save", json={"comments": comments})

    captured = {}

    def fake_run_job(job_id, fn, *args, **kwargs):
        captured["args"] = args  # (profile_name, comments_file, count)

    monkeypatch.setattr(linkedin_dashboard, "run_job", fake_run_job)
    monkeypatch.setattr(linkedin_dashboard, "can_start_browser_task", lambda *a, **k: True)

    resp = c.post("/api/post/jeff", json={"count": 3})  # no comments_file
    assert resp.status_code == 200
    assert "job_id" in resp.get_json()
    resolved_file = captured["args"][1]
    assert os.path.basename(resolved_file).startswith("daily_comments_")
    assert os.path.exists(resolved_file)


def test_post_returns_400_when_no_txt(client, monkeypatch):
    c, comments_dir = client
    monkeypatch.setattr(linkedin_dashboard, "run_job", lambda *a, **k: None)
    monkeypatch.setattr(linkedin_dashboard, "can_start_browser_task", lambda *a, **k: True)
    resp = c.post("/api/post/jeff", json={"count": 1})
    assert resp.status_code == 400
    assert "No comments file" in resp.get_json()["error"]
