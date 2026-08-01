"""Phase 7 — a crash mid-write must not corrupt state, and a re-run must not double up.

The property that matters is not "the new file is correct". It is **the old file
survived**. A plain ``open(path, 'w')`` truncates before it writes, so the window
between those two events is one in which the previous contents are already gone.
For ``posting_progress.json`` that window costs a comment posted twice on
somebody else's post.
"""

import json
import os
import re
from pathlib import Path

import pytest

from linkedin_automation import atomic_io
from linkedin_automation import post_store


REPO = Path(__file__).parent.parent
PACKAGE_FILES = sorted(REPO.glob("linkedin_automation/*.py")) + sorted(REPO.glob("tools/*.py"))


# ─── The helper ───────────────────────────────────────────────────────────────

def test_a_normal_write_round_trips(tmp_path):
    target = tmp_path / "store.json"
    atomic_io.write_json_atomic(str(target), {"a": 1, "b": ["x", "y"]})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1, "b": ["x", "y"]}


def test_it_creates_missing_directories(tmp_path):
    target = tmp_path / "deep" / "nested" / "store.json"
    atomic_io.write_json_atomic(str(target), {"ok": True})
    assert target.exists()


def test_an_interrupted_write_leaves_the_previous_file_intact(tmp_path, monkeypatch):
    """The gate. Not 'the new file is correct' — 'the old file survived'."""
    target = tmp_path / "progress.json"
    original = {"posted_comments": ["https://example.test/a", "https://example.test/b"]}
    atomic_io.write_json_atomic(str(target), original)

    def die(*a, **k):
        raise KeyboardInterrupt("interrupted mid-write")

    monkeypatch.setattr(atomic_io.os, "replace", die)
    with pytest.raises(KeyboardInterrupt):
        atomic_io.write_json_atomic(str(target), {"posted_comments": []})

    # Intact, parseable, and still the OLD content.
    assert json.loads(target.read_text(encoding="utf-8")) == original


def test_a_failed_write_leaves_no_temp_file_behind(tmp_path, monkeypatch):
    target = tmp_path / "store.json"
    atomic_io.write_json_atomic(str(target), {"v": 1})

    monkeypatch.setattr(atomic_io.os, "replace",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    with pytest.raises(OSError):
        atomic_io.write_json_atomic(str(target), {"v": 2})

    assert [p.name for p in tmp_path.iterdir()] == ["store.json"]


def test_unencodable_data_never_touches_the_target(tmp_path):
    """Serialisation happens in the temp file, so a bad value cannot destroy
    a good file on its way to failing."""
    target = tmp_path / "store.json"
    atomic_io.write_json_atomic(str(target), {"v": 1})

    with pytest.raises(TypeError):
        atomic_io.write_json_atomic(str(target), {"bad": object()})

    assert json.loads(target.read_text(encoding="utf-8")) == {"v": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["store.json"]


def test_the_temp_file_is_unique_per_write(tmp_path, monkeypatch):
    """A fixed `path + '.tmp'` collides when two writers touch one file, and
    then they corrupt each other's temp — the same bug one level down."""
    target = tmp_path / "store.json"
    seen = []
    real_replace = atomic_io.os.replace

    def spy(src, dst):
        seen.append(os.path.basename(src))
        real_replace(src, dst)

    monkeypatch.setattr(atomic_io.os, "replace", spy)
    for i in range(3):
        atomic_io.write_json_atomic(str(target), {"i": i})

    assert len(set(seen)) == 3
    assert all(name.endswith(".tmp") for name in seen)


def test_the_temp_file_sits_beside_the_target(tmp_path, monkeypatch):
    """os.replace is only atomic within one filesystem, so the temp cannot live
    in the system temp directory."""
    target = tmp_path / "sub" / "store.json"
    captured = {}
    real_replace = atomic_io.os.replace
    monkeypatch.setattr(atomic_io.os, "replace",
                        lambda src, dst: (captured.update(src=src), real_replace(src, dst)))
    atomic_io.write_json_atomic(str(target), {"v": 1})
    assert os.path.dirname(captured["src"]) == str(tmp_path / "sub")


def test_the_bytes_are_flushed_before_the_rename(tmp_path, monkeypatch):
    """Without fsync the rename can land before the content does."""
    synced = []
    monkeypatch.setattr(atomic_io.os, "fsync", lambda fd: synced.append(fd))
    atomic_io.write_json_atomic(str(tmp_path / "store.json"), {"v": 1})
    assert synced


# ─── The Windows-locked-destination retry (AUDIT A3) ──────────────────────────

def test_replace_gives_up_and_raises_rather_than_reporting_success(monkeypatch):
    """A silent failure here means the caller believes it saved and did not."""
    def always_locked(src, dst):
        raise PermissionError("[WinError 32] file in use")

    monkeypatch.setattr(atomic_io.os, "replace", always_locked)
    slept = []
    with pytest.raises(PermissionError):
        atomic_io.replace_with_retry("src", "dst", attempts=4,
                                     initial_delay=0.1, sleep=slept.append)
    assert len(slept) == 3          # three waits between four attempts


def test_the_retry_backs_off_rather_than_spinning(monkeypatch):
    def always_locked(src, dst):
        raise PermissionError("locked")

    monkeypatch.setattr(atomic_io.os, "replace", always_locked)
    slept = []
    with pytest.raises(PermissionError):
        atomic_io.replace_with_retry("s", "d", attempts=5, initial_delay=0.1,
                                     sleep=slept.append)
    assert slept == [0.1, 0.2, 0.4, 0.8]


def test_a_transient_lock_is_survived(monkeypatch):
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("locked")

    monkeypatch.setattr(atomic_io.os, "replace", flaky)
    atomic_io.replace_with_retry("s", "d", sleep=lambda s: None)
    assert calls["n"] == 3


# ─── No plain JSON writer is left in the package ──────────────────────────────

def test_no_module_writes_json_without_the_helper():
    """The gate that stops this regressing one writer at a time.

    AUDIT C2 counted eight. There were sixteen by the time this phase ran,
    because Phases 8, 8b, 11b and 5b each added more while the finding sat
    open. A grep-style guard is the only thing that keeps the count at zero.
    """
    offenders = []
    for path in PACKAGE_FILES:
        source = path.read_text(encoding="utf-8")
        if path.name == "atomic_io.py":
            continue
        for match in re.finditer(r"json\.dump\(", source):
            line_no = source[:match.start()].count("\n") + 1
            window = "\n".join(source.split("\n")[max(0, line_no - 4):line_no])
            if not re.search(r"\.tmp|tmp_path|mkstemp", window):
                offenders.append(f"{path.relative_to(REPO)}:{line_no}")
    assert offenders == []


def test_the_reference_implementations_still_write_atomically():
    """profile_manager.save_profiles and post_store.save were already correct
    and are the pattern the helper generalises. They must not drift."""
    for path, func in [("linkedin_automation/profile_manager.py", "def save_profiles"),
                       ("linkedin_automation/post_store.py", "    def save(self)")]:
        source = (REPO / path).read_text(encoding="utf-8")
        body = source.split(func)[1][:600]
        assert "os.replace" in body, path


# ─── Reads must not write (AUDIT A3, second half) ─────────────────────────────

def test_a_pure_read_does_not_rewrite_the_store(tmp_path, monkeypatch):
    """AUDIT A3 claimed every dashboard page load rewrites posts_db.json.

    It does not: ``reconcile`` has always guarded its save with ``if changed``.
    The finding was wrong on this point, and the property is worth a test
    precisely because nothing was enforcing it.
    """
    from linkedin_automation import profile_manager as pm

    monkeypatch.setattr(pm, "get_data_dir", lambda profile_name=None, subdir=None: str(tmp_path))
    monkeypatch.setattr(pm, "get_progress_file",
                        lambda profile_name=None: str(tmp_path / "posting_progress.json"))
    monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None: str(tmp_path))

    store = post_store.load_synced_store("t")
    store.upsert_scraped({"url": "https://www.linkedin.com/feed/update/urn:li:activity:1/",
                          "text": "body", "author": "someone"})
    store.save()

    before = os.stat(store.path).st_mtime_ns
    payload_before = Path(store.path).read_bytes()
    for _ in range(3):
        post_store.load_synced_store("t")

    assert Path(store.path).read_bytes() == payload_before
    assert os.stat(store.path).st_mtime_ns == before


def test_reconcile_writes_when_it_actually_changes_something(tmp_path, monkeypatch):
    """The other half: the guard must not have been achieved by never saving."""
    from linkedin_automation import profile_manager as pm

    url = "https://www.linkedin.com/feed/update/urn:li:activity:2/"
    monkeypatch.setattr(pm, "get_data_dir", lambda profile_name=None, subdir=None: str(tmp_path))
    monkeypatch.setattr(pm, "get_progress_file",
                        lambda profile_name=None: str(tmp_path / "posting_progress.json"))
    monkeypatch.setattr(pm, "get_comments_dir", lambda profile_name=None: str(tmp_path))

    store = post_store.load_synced_store("t")
    store.upsert_scraped({"url": url, "text": "body", "author": "someone"})
    store.save()

    # The posted ledger now says this one is done, which is real drift.
    atomic_io.write_json_atomic(str(tmp_path / "posting_progress.json"),
                                {"posted_comments": [url]})
    payload_before = Path(store.path).read_bytes()
    reloaded = post_store.load_synced_store("t")

    assert Path(store.path).read_bytes() != payload_before
    assert reloaded.get(url)["status"] == post_store.COMMENTED
