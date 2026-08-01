"""linkedin_automation/atomic_io.py — write JSON without the risk of a half-written file.

A plain ``open(path, 'w')`` truncates the file *before* the new content is
written. If the process dies in that window — a crash, a Ctrl-C, a killed
subprocess, a full disk — what remains is a truncated file that is no longer
valid JSON, and the previous contents are gone.

That matters unevenly across this project. Losing a scrape result costs a
re-scrape. Losing ``posting_progress.json`` costs a duplicate comment on
somebody's post, because that file is the record of what has already been
posted and is the only thing standing between a re-run and a double post.
Losing the connection tracker resets the daily and weekly caps that exist to
keep the account out of trouble.

The fix is the pattern ``profile_manager.save_profiles`` and ``post_store.save``
already used: write a temp file in the same directory, then ``os.replace`` it
over the target. ``os.replace`` is atomic, so a reader sees either the whole old
file or the whole new one, never a partial write. This module makes that the
single implementation instead of a habit some writers happened to follow.

Two things it adds over the pattern it replaces:

* **A unique temp name.** A fixed ``path + ".tmp"`` collides when two writers
  touch the same file at once, and then they corrupt each other's temp rather
  than the target — a subtler version of the bug being fixed.
* **A retry around the replace.** On POSIX, replacing a file another process
  holds open succeeds. Windows refuses with ``PermissionError``, which is what
  produced the observed 500 when a scrape and a dashboard page load collided
  (AUDIT A3). Windows is not a target any more, but it is in CI and it is the
  maintainer's platform upstream.

**What this does not promise.** ``fsync`` on the temp file means the bytes have
reached the disk before the rename, so a crash cannot leave a half-written
replacement. The directory entry itself is not synced, so a power loss in the
narrow window around the rename can still lose the *most recent* write. The old
file survives either way, which is the property that actually matters here.
"""

import json
import logging
import os
import tempfile
import time

logger = logging.getLogger(__name__)

# Windows refuses os.replace while another process holds the destination open,
# and the holder is usually a reader that will be gone in milliseconds. Five
# attempts with exponential backoff spans roughly 1.5s, which covers a dashboard
# page load without turning a genuine lock into a long stall.
REPLACE_ATTEMPTS = 5
REPLACE_INITIAL_DELAY = 0.1


def replace_with_retry(src: str, dst: str, attempts: int = REPLACE_ATTEMPTS,
                       initial_delay: float = REPLACE_INITIAL_DELAY,
                       sleep=time.sleep) -> None:
    """``os.replace(src, dst)``, retried while the destination is locked.

    Raises the final ``PermissionError`` rather than leaving the caller to think
    the write succeeded. ``sleep`` is injectable so tests need no real delay.
    """
    delay = initial_delay
    for attempt in range(1, attempts + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts:
                logger.warning(
                    f"Could not replace {dst} after {attempts} attempts: another "
                    f"process is holding it open. The previous file is intact."
                )
                raise
            sleep(delay)
            delay *= 2


def write_json_atomic(path: str, data, indent: int = 2,
                      ensure_ascii: bool = False, **replace_kwargs) -> None:
    """Serialise ``data`` to ``path`` as JSON, atomically.

    On any failure the target is left exactly as it was, and the temp file is
    cleaned up. Serialisation happens before the rename, so a value that cannot
    be encoded raises without touching the target at all.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    # Same directory as the target: os.replace is only atomic within a
    # filesystem, and a temp dir may well be on a different one.
    fd, tmp = tempfile.mkstemp(
        dir=directory, prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii)
            f.flush()
            os.fsync(f.fileno())
        replace_with_retry(tmp, path, **replace_kwargs)
    except BaseException:
        # BaseException, not Exception: a KeyboardInterrupt mid-write is one of
        # the exact scenarios this module exists for, and it must not leave a
        # stray temp file behind either.
        try:
            os.unlink(tmp)
        except OSError:
            logger.debug(f"Could not remove temp file {tmp}", exc_info=True)
        raise


def read_json(path: str, default=None):
    """Read JSON from ``path``, returning ``default`` when it is absent.

    Uses ``utf-8-sig`` so a byte order mark does not make the file unreadable;
    a BOM silently emptying the profile store was finding B6 in Phase 3.
    """
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)
