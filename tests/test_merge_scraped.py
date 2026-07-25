"""Tests for merging/deduping scraped posts and generated comments across files
(merge-scraped-posts). Pure helpers are unit-tested; the endpoints are tested
through the Flask client with sample JSON files that have overlapping URLs."""

import json
import os
import time

import pytest

from linkedin_automation import profile_manager as pm
from linkedin_automation import dashboard as dash


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def timeline_dir(tmp_path, monkeypatch):
    """Redirect the profile timeline dir (scrape output) to tmp."""
    tdir = tmp_path / "linkedin_timeline"
    tdir.mkdir(parents=True)
    monkeypatch.setattr(pm, "get_timeline_dir", lambda profile_name=None: str(tdir))
    return tdir


def _post(url=None, author="Author A", text="some interesting body", score=10):
    p = {"author_name": author, "text": text, "relevance_score": score, "quality": "medium"}
    if url is not None:
        p["url"] = url
    return p


def _write_scrape(tdir, name, posts, age_seconds=0):
    path = tdir / name
    path.write_text(json.dumps({"scan_date": "2026-06-29", "total_scanned": len(posts),
                                "quality_posts": posts}), encoding="utf-8")
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(path, (old, old))
    return path


def _write_comments(cdir, name, comments, curated=False, age_seconds=0):
    path = cdir / name
    path.write_text(json.dumps({"curated": curated, "comments": comments}), encoding="utf-8")
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(path, (old, old))
    return path


# ─── merge_posts (pure) ────────────────────────────────────────────────────────

def test_merge_posts_dedupes_by_url_keeping_higher_score():
    newer = {"quality_posts": [_post("u1", score=5), _post("u2", score=10)]}
    older = {"quality_posts": [_post("u1", score=9), _post("u3", score=3)]}
    merged = dash.merge_posts([newer, older])
    by_url = {p["url"]: p for p in merged}
    assert set(by_url) == {"u1", "u2", "u3"}
    assert by_url["u1"]["relevance_score"] == 9          # higher-scored copy wins
    assert [p["url"] for p in merged] == ["u2", "u1", "u3"]  # sorted by score desc


def test_merge_posts_dedupes_urlless_by_author_text_hash():
    a = _post(url=None, author="Jane", text="x" * 200, score=4)
    a_dup = _post(url=None, author="Jane", text="x" * 200, score=4)   # same first 100 chars
    b = _post(url=None, author="Jane", text="totally different body", score=4)
    merged = dash.merge_posts([{"quality_posts": [a, a_dup, b]}])
    assert len(merged) == 2


def test_merge_posts_caps_list():
    posts = [_post(f"u{i}", score=i) for i in range(10)]
    merged = dash.merge_posts([{"quality_posts": posts}], max_posts=3)
    assert len(merged) == 3
    assert [p["url"] for p in merged] == ["u9", "u8", "u7"]   # top scores


def test_merge_posts_ignores_non_dict_and_missing_keys():
    assert dash.merge_posts([None, {}, {"quality_posts": []}]) == []


# ─── merge_comments (pure) ───────────────────────────────────────────────────--

def test_merge_comments_dedupes_keeping_first():
    newer = {"comments": [{"post_url": "c1", "comment": "newest"}, {"post_url": "c2", "comment": "b"}]}
    older = {"comments": [{"post_url": "c1", "comment": "older"}, {"post_url": "c3", "comment": "d"}]}
    merged = dash.merge_comments([newer, older])
    urls = [dash.normalize_comment_fields(c)["url"] for c in merged]
    assert urls == ["c1", "c2", "c3"]
    # First (newest) copy of c1 is kept.
    assert merged[0]["comment"] == "newest"


def test_merge_comments_accepts_bare_list_files():
    merged = dash.merge_comments([[{"url": "c1", "comment": "a"}], {"comments": [{"url": "c1"}]}])
    assert len(merged) == 1


# ─── _files_within_days ──────────────────────────────────────────────────────--

def test_files_within_days_excludes_old_and_sorts_newest_first(tmp_path):
    recent = tmp_path / "ai_posts_recent.json"
    mid = tmp_path / "ai_posts_mid.json"
    old = tmp_path / "ai_posts_old.json"
    for fp in (recent, mid, old):
        fp.write_text("{}", encoding="utf-8")
    now = time.time()
    os.utime(recent, (now, now))
    os.utime(mid, (now - 2 * 86400, now - 2 * 86400))
    os.utime(old, (now - 10 * 86400, now - 10 * 86400))   # outside the 7-day window
    found = dash._files_within_days(str(tmp_path), "ai_posts_*.json", 7, now=now)
    assert [os.path.basename(f) for f in found] == ["ai_posts_recent.json", "ai_posts_mid.json"]


# ─── /api/posts endpoint ───────────────────────────────────────────────────────

def test_get_posts_returns_store_new_bin(api_client, timeline_dir, comments_dir):
    """get_posts is store-driven now: the store is seeded from the scrape/comment/
    progress files, then only its NEW bin is returned (u1 posted→COMMENTED,
    u3 has a draft→GENERATED, u2 stays NEW)."""
    _write_scrape(timeline_dir, "ai_posts_1.json",
                  [_post("u1", score=8), _post("u2", score=5)], age_seconds=3600)
    _write_scrape(timeline_dir, "ai_posts_2.json",
                  [_post("u2", score=9), _post("u3", score=7)])
    (comments_dir / "posting_progress.json").write_text(
        json.dumps({"posted_comments": ["u1"]}), encoding="utf-8")
    _write_comments(comments_dir, "comments_x.json", [{"post_url": "u3", "comment": "hi"}])

    data = api_client.get("/api/posts/jeff").get_json()
    urls = [p["url"] for p in data["posts"]]
    assert urls == ["u2"]                       # only the NEW bin
    assert data["source"] == "lifecycle_store"
    # Invariant: the tab list length equals the NEW lifecycle bin.
    assert len(data["posts"]) == data["counts"]["NEW"] == 1
    assert all(p["status"] == "NEW" for p in data["posts"])


def test_get_posts_empty_when_no_data(api_client, timeline_dir, comments_dir):
    data = api_client.get("/api/posts/jeff").get_json()
    assert data["posts"] == []
    assert data["counts"]["NEW"] == 0


# ─── /api/comments endpoint ─────────────────────────────────────────────────--

def test_get_comments_merges_active_excludes_archived_and_posted(api_client, comments_dir):
    _write_comments(comments_dir, "comments_1.json",
                    [{"post_url": "cu1", "comment": "old one"}, {"post_url": "cu2", "comment": "posted"}],
                    age_seconds=3600)
    _write_comments(comments_dir, "comments_2.json",
                    [{"post_url": "cu1", "comment": "new one"}, {"post_url": "cu3", "comment": "fresh"}])
    # Archived originals must NOT be merged back into review.
    archived = comments_dir / "archived"
    archived.mkdir()
    _write_comments(archived, "comments_old.json", [{"post_url": "cu4", "comment": "archived"}])
    # cu2 already posted.
    (comments_dir / "posting_progress.json").write_text(
        json.dumps({"posted_comments": ["cu2"]}), encoding="utf-8")

    data = api_client.get("/api/comments/jeff").get_json()
    urls = {c["url"] for c in data["comments"]}
    assert urls == {"cu1", "cu3"}          # cu2 posted, cu4 archived, cu1 deduped
    assert all(c["url"] != "cu4" for c in data["comments"])
    # Normalized fields present for the frontend.
    assert all("post_url" in c and "url" in c for c in data["comments"])


def test_get_comments_ignores_ready_and_curated_files(api_client, comments_dir):
    # ready_*.json (curated output) is not part of the review queue.
    _write_comments(comments_dir, "comments_1.json", [{"post_url": "cu1", "comment": "review me"}])
    _write_comments(comments_dir, "ready_1.json", [{"post_url": "cuR", "comment": "already curated"}], curated=True)
    data = api_client.get("/api/comments/jeff").get_json()
    urls = {c["url"] for c in data["comments"]}
    assert urls == {"cu1"}
