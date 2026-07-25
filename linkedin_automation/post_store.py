"""Central per-profile post lifecycle store.

Every scraped post lives in one place — ``data/<profile>/posts_db.json`` — with an
explicit status in its lifecycle instead of being scattered across ``ai_posts_*``,
``comments_*``, ``ready_*`` and ``posting_progress.json``.

Lifecycle:

    NEW ──generate──▶ GENERATED ──post──▶ COMMENTED
     │                    │
     └────reject──────────┴──────▶ TRASH (ad | job_card | low_quality | no_url | manual)
                                      └──restore──▶ NEW / GENERATED

``posting_progress.json`` stays the authoritative ledger of what was actually
posted; this store *reconciles* COMMENTED from it (``sync_with_progress``) rather
than keeping a competing record, so the two can never diverge.

Identity/dedup uses the same rule as the dashboard's ``_dedupe_key`` (URL, else a
hash of author + first 100 chars of text), implemented here as ``post_key`` and
delegated to from the dashboard so dedup stays identical everywhere.
"""

import os
import json
import hashlib
import logging
from datetime import datetime
from typing import Dict, List, Optional

from .comment_fields import normalize_comment_fields

logger = logging.getLogger(__name__)

# ─── Status constants ─────────────────────────────────────────────────────────

NEW = "NEW"
GENERATED = "GENERATED"
COMMENTED = "COMMENTED"
TRASH = "TRASH"

STATUSES = (NEW, GENERATED, COMMENTED, TRASH)

# Trash reasons.
REASON_AD = "ad"
REASON_JOB = "job_card"
REASON_LOW_QUALITY = "low_quality"
# A post with no URL can't be commented on or posted (nothing to link to), so it
# is not actionable and is trashed rather than left stuck in NEW forever. Unlike
# ``manual``, this is a computed/auto reason: if a later scrape produces a URL for
# the same post it can re-enter the pipeline.
REASON_NO_URL = "no_url"
REASON_MANUAL = "manual"
AUTO_REASONS = (REASON_AD, REASON_JOB, REASON_LOW_QUALITY, REASON_NO_URL)

# Rank used so re-scrape / migration never downgrade a post we've acted on. A
# higher rank "wins". Manual trash is handled separately (it is sticky).
_RANK = {TRASH: 0, NEW: 1, GENERATED: 2, COMMENTED: 3}

SCHEMA_VERSION = 1


# ─── Identity ─────────────────────────────────────────────────────────────────

def post_key(item: Dict) -> str:
    """Stable identity for a post/comment dict — its URL, else a content hash.

    Matches the dashboard's historical ``_dedupe_key`` exactly (via
    ``normalize_comment_fields``) so dedup is identical across the store and the
    file-merge helpers.
    """
    norm = normalize_comment_fields(item or {})
    url = (norm["url"] or "").strip()
    if url:
        return url
    text = (item.get("text") or norm["post_text"] or norm["comment"] or "")[:100]
    digest = hashlib.md5(f"{norm['author']}:{text}".encode("utf-8")).hexdigest()
    return f"hash:{digest}"


def _now() -> str:
    return datetime.now().isoformat()


def _read_json(path):
    """Read a JSON file, returning None (and logging) on any error."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.debug("post_store: could not read %s", path, exc_info=True)
        return None


# ─── Store ────────────────────────────────────────────────────────────────────

class PostStore:
    """Load/mutate/save a profile's ``posts_db.json`` lifecycle store.

    Cheap to construct; reads the file once. Mutations are in-memory until
    ``save()``. The transition helpers (``upsert_scraped``/``mark_generated``/
    ``reject``/``restore``) save by default so callers in separate processes
    (finder, generator) don't have to remember to.
    """

    def __init__(self, profile_name: str = None, path: str = None):
        self.profile_name = profile_name
        self.path = path or self._default_path(profile_name)
        self.data = self._load()

    # ── persistence ──────────────────────────────────────────────────────────

    @staticmethod
    def _default_path(profile_name: str = None) -> str:
        # Imported lazily so post_store stays importable without a configured
        # profile manager (e.g. in unit tests that pass an explicit path).
        from . import profile_manager as pm
        return os.path.join(pm.get_data_dir(profile_name), "posts_db.json")

    def _empty(self) -> Dict:
        return {"version": SCHEMA_VERSION, "updated_at": _now(), "posts": {}}

    def _load(self) -> Dict:
        if not os.path.exists(self.path):
            return self._empty()
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("posts"), dict):
                raise ValueError("posts_db.json is not a valid store object")
            data.setdefault("version", SCHEMA_VERSION)
            return data
        except (OSError, json.JSONDecodeError, ValueError) as e:
            logger.warning("posts_db.json unreadable (%s); starting a fresh store", e)
            return self._empty()

    def save(self):
        """Atomically write the store (temp file + replace)."""
        self.data["updated_at"] = _now()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    # ── helpers ──────────────────────────────────────────────────────────────

    @property
    def posts(self) -> Dict[str, Dict]:
        """The ``key -> record`` map of every post in the store."""
        return self.data["posts"]

    def get(self, key: str) -> Optional[Dict]:
        """Return the record for ``key``, or None if it isn't in the store."""
        return self.posts.get(key)

    @staticmethod
    def _record_from_post(post: Dict) -> Dict:
        """Project a finder/generator post dict onto the store's record fields."""
        norm = normalize_comment_fields(post)
        return {
            "url": norm["url"],
            "author": post.get("author_name") or norm["author"],
            "text": post.get("text") or norm["post_text"] or "",
            "category": post.get("post_type") or norm["category"] or "",
            "relevance_score": post.get("relevance_score", 0) or 0,
        }

    def _is_manual_trash(self, rec: Dict) -> bool:
        return rec.get("status") == TRASH and rec.get("trash_reason") == REASON_MANUAL

    # ── transitions ──────────────────────────────────────────────────────────

    def upsert_scraped(self, post: Dict, status: str = NEW,
                       reason: str = None, save: bool = False) -> str:
        """Insert or refresh a scraped post.

        ``status`` is the computed landing state (NEW for a quality post, TRASH
        with ``reason`` for ad/job/low-quality). Existing records are protected:
        COMMENTED/GENERATED are never downgraded by a re-scrape, and a manual
        TRASH (user reject) is never resurrected. NEW / auto-TRASH records get
        their text/score refreshed and their status set to the new computation.
        Returns the post key.
        """
        key = post_key(post)
        fields = self._record_from_post(post)
        existing = self.posts.get(key)

        if existing is not None:
            # Refresh content either way (text/score can improve between scrapes).
            existing.update(fields)
            existing["key"] = key
            # Protect acted-on / user-decided records from a re-scrape.
            if existing.get("status") in (COMMENTED, GENERATED) or self._is_manual_trash(existing):
                if save:
                    self.save()
                return key
            # Otherwise (NEW or auto-TRASH) adopt the freshly computed status.
            existing["status"] = status
            existing["trash_reason"] = reason if status == TRASH else None
            existing["updated_at"] = _now()
            if save:
                self.save()
            return key

        rec = {
            "key": key,
            **fields,
            "status": status,
            "trash_reason": reason if status == TRASH else None,
            "comment": None,
            "comment_meta": None,
            "scraped_at": _now(),
            "generated_at": None,
            "commented_at": None,
            "updated_at": _now(),
        }
        self.posts[key] = rec
        if save:
            self.save()
        return key

    def mark_generated(self, key_or_url: str, comment: str,
                       meta: Dict = None, save: bool = False) -> bool:
        """Attach a draft comment and move NEW → GENERATED.

        No-op (returns False) for an already-COMMENTED post. If the post isn't in
        the store yet it's created as GENERATED (defensive — the generator should
        have scraped it first, but a stray input file shouldn't lose data).
        """
        rec = self._resolve(key_or_url)
        if rec is None:
            # Create a minimal GENERATED record keyed by URL.
            key = post_key({"url": key_or_url})
            rec = {
                "key": key, "url": key_or_url if "://" in str(key_or_url) else "",
                "author": "", "text": "", "category": "", "relevance_score": 0,
                "status": NEW, "trash_reason": None, "comment": None,
                "comment_meta": None, "scraped_at": _now(), "generated_at": None,
                "commented_at": None, "updated_at": _now(),
            }
            self.posts[key] = rec

        if rec.get("status") == COMMENTED:
            return False

        rec["status"] = GENERATED
        rec["trash_reason"] = None
        rec["comment"] = comment
        rec["comment_meta"] = meta or {}
        rec["generated_at"] = _now()
        rec["updated_at"] = _now()
        if save:
            self.save()
        return True

    def reject(self, key_or_url: str, save: bool = False) -> bool:
        """Move any post → TRASH(manual). Keeps the draft so it can be restored."""
        rec = self._resolve(key_or_url)
        if rec is None:
            return False
        rec["status"] = TRASH
        rec["trash_reason"] = REASON_MANUAL
        rec["updated_at"] = _now()
        if save:
            self.save()
        return True

    def restore(self, key_or_url: str, save: bool = False) -> bool:
        """Move a TRASH post back to GENERATED (if it has a draft) else NEW."""
        rec = self._resolve(key_or_url)
        if rec is None or rec.get("status") != TRASH:
            return False
        rec["status"] = GENERATED if rec.get("comment") else NEW
        rec["trash_reason"] = None
        rec["updated_at"] = _now()
        if save:
            self.save()
        return True

    def trash_urlless_new(self, save: bool = False) -> int:
        """Move NEW posts that have no URL → TRASH(no_url). Returns the count moved.

        A post with no URL can't be commented on or posted (there is nothing to
        link to), so it is not actionable and doesn't belong in NEW. Only records
        currently in NEW are touched, so a post that reached COMMENTED/GENERATED
        (or was manually trashed) is never affected. Idempotent: a post the user
        restores that still has no URL is simply re-trashed on the next call.
        """
        changed = 0
        for rec in self.posts.values():
            if rec.get("status") == NEW and not (rec.get("url") or "").strip():
                rec["status"] = TRASH
                rec["trash_reason"] = REASON_NO_URL
                rec["updated_at"] = _now()
                changed += 1
        if changed and save:
            self.save()
        return changed

    def recover_or_demote_generated(self, drafts: Dict[str, Dict],
                                    save: bool = False):
        """Repair GENERATED records whose draft comment went missing.

        A GENERATED post must carry its draft (that's what the Generated tab
        renders). If a record is GENERATED but its ``comment`` is empty, its
        status and draft got separated. For each such record: if ``drafts`` (a
        ``url -> normalized-comment`` map from the comment files) has its draft,
        re-attach it (stay GENERATED); otherwise demote it to NEW so it will be
        regenerated. Returns ``(recovered, demoted)`` counts.
        """
        recovered = demoted = 0
        for rec in self.posts.values():
            if rec.get("status") != GENERATED or (rec.get("comment") or "").strip():
                continue
            url = (rec.get("url") or "").strip()
            norm = drafts.get(url) if url else None
            if norm:
                rec["comment"] = norm["comment"]
                rec["comment_meta"] = {
                    "style": norm["style"], "approach": norm["approach"],
                    "word_count": norm["word_count"],
                }
                rec["updated_at"] = _now()
                recovered += 1
            else:
                rec["status"] = NEW
                rec["comment"] = None
                rec["comment_meta"] = None
                rec["generated_at"] = None
                rec["updated_at"] = _now()
                demoted += 1
        if (recovered or demoted) and save:
            self.save()
        return recovered, demoted

    def sync_with_progress(self, posted_urls, save: bool = False) -> int:
        """Reconcile COMMENTED from posting_progress.json (the posted source-of-truth).

        Any post whose URL is in ``posted_urls`` and isn't already COMMENTED is
        marked COMMENTED. Returns the number of records changed.
        """
        posted = set(posted_urls or [])
        changed = 0
        for rec in self.posts.values():
            url = (rec.get("url") or "").strip()
            if url and url in posted and rec.get("status") != COMMENTED:
                rec["status"] = COMMENTED
                rec["trash_reason"] = None
                rec["commented_at"] = rec.get("commented_at") or _now()
                rec["updated_at"] = _now()
                changed += 1
        if changed and save:
            self.save()
        return changed

    def _resolve(self, key_or_url: str) -> Optional[Dict]:
        """Find a record by exact key, else by treating the arg as a URL."""
        if key_or_url in self.posts:
            return self.posts[key_or_url]
        key = post_key({"url": key_or_url})
        return self.posts.get(key)

    # ── queries ──────────────────────────────────────────────────────────────

    def counts(self) -> Dict[str, int]:
        """Count of posts in each lifecycle state (all four keys always present)."""
        out = {s: 0 for s in STATUSES}
        for rec in self.posts.values():
            status = rec.get("status")
            if status in out:
                out[status] += 1
        return out

    def trash_reason_counts(self) -> Dict[str, int]:
        """Count of TRASH records grouped by ``trash_reason`` (for diagnostics).

        A large ``no_url`` pile is a signal that URL extraction is missing posts.
        """
        out: Dict[str, int] = {}
        for rec in self.posts.values():
            if rec.get("status") == TRASH:
                reason = rec.get("trash_reason") or "unknown"
                out[reason] = out.get(reason, 0) + 1
        return out

    def by_status(self, status: str) -> List[Dict]:
        """Records in ``status``, newest-relevant first.

        NEW/COMMENTED sort by relevance score; GENERATED/TRASH by recency.
        """
        recs = [r for r in self.posts.values() if r.get("status") == status]
        if status in (NEW, COMMENTED):
            recs.sort(key=lambda r: r.get("relevance_score", 0) or 0, reverse=True)
        else:
            recs.sort(key=lambda r: r.get("updated_at", ""), reverse=True)
        return recs

    # Public alias — the lifecycle store is the source of truth for "what posts
    # are available to act on", so callers (e.g. the comment generator) ask for
    # a status explicitly rather than reading scrape files.
    def get_posts_by_status(self, status: str) -> List[Dict]:
        """Records currently in ``status`` (e.g. all NEW posts awaiting a draft)."""
        return self.by_status(status)


# ─── Mapping helpers ──────────────────────────────────────────────────────────

def ad_reason_to_trash_reason(ad_reason: str) -> str:
    """Map ``classify_ad`` output to a store trash reason.

    classify_ad returns strings like "promoted/sponsored", "blocklisted
    advertiser", "job/recommendation card", "recommendation card", "likely ad".
    Job/recommendation cards become ``job_card``; everything else is ``ad``.
    """
    low = (ad_reason or "").lower()
    if "job" in low or "recommendation" in low:
        return REASON_JOB
    return REASON_AD


# ─── Migration ────────────────────────────────────────────────────────────────

def migrate_from_legacy(profile_name: str = None, store: PostStore = None,
                        window_days: int = 7) -> Dict[str, int]:
    """Seed a store from existing scrape/comment/progress files (idempotent).

    Precedence COMMENTED > GENERATED > NEW > TRASH(low_quality):
      1. ai_posts_*.json (within ``window_days``): quality_posts → NEW;
         all_posts with should_engage == False → TRASH(low_quality).
      2. comments_*.json + ready_*.json: each post → GENERATED with its draft.
      3. posting_progress.json posted_comments → COMMENTED.

    Returns a summary dict of how many records landed in each state. Safe to run
    repeatedly: the transition guards prevent downgrades, so a second run is a
    no-op for already-classified posts.
    """
    import glob
    import time as _time
    from . import profile_manager as pm

    store = store or PostStore(profile_name)

    timeline_dir = pm.get_timeline_dir(profile_name)
    comments_dir = pm.get_comments_dir(profile_name)
    cutoff = _time.time() - window_days * 86400

    def _recent(dirpath, pattern):
        files = []
        for fp in glob.glob(os.path.join(dirpath, pattern)):
            try:
                if os.path.getmtime(fp) >= cutoff:
                    files.append(fp)
            except OSError:
                continue
        return files

    def _read(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            logger.debug("migrate: could not read %s", path, exc_info=True)
            return None

    # 1. Scrape files → NEW / TRASH(low_quality).
    for fp in _recent(timeline_dir, "ai_posts_*.json"):
        data = _read(fp)
        if not isinstance(data, dict):
            continue
        for post in data.get("quality_posts", []) or []:
            store.upsert_scraped(post, status=NEW)
        for post in data.get("all_posts", []) or []:
            if not post.get("should_engage", False):
                store.upsert_scraped(post, status=TRASH, reason=REASON_LOW_QUALITY)

    # 2. Generated/curated comments → GENERATED (attach the draft).
    for pattern in ("comments_*.json", "ready_*.json"):
        for fp in glob.glob(os.path.join(comments_dir, pattern)):
            data = _read(fp)
            if data is None:
                continue
            comments = data if isinstance(data, list) else data.get("comments", []) or []
            for c in comments:
                norm = normalize_comment_fields(c)
                # Seed a record so a comment whose post wasn't in a scrape file
                # still appears; upsert is a no-op-content refresh if it exists.
                key = store.upsert_scraped({
                    "url": norm["url"], "author_name": norm["author"],
                    "text": norm["post_text"], "post_type": norm["category"],
                }, status=NEW)
                if norm["comment"]:
                    store.mark_generated(key, norm["comment"], meta={
                        "style": norm["style"], "approach": norm["approach"],
                        "word_count": norm["word_count"],
                    })

    # 3. posting_progress.json → COMMENTED (authoritative).
    progress_file = pm.get_progress_file(profile_name)
    progress = _read(progress_file) or {}
    store.sync_with_progress(progress.get("posted_comments", []) or [])

    store.save()
    return store.counts()


def _collect_drafts(comments_dir: str) -> Dict[str, Dict]:
    """Build ``url -> normalized-comment`` from every comment file for a profile.

    Scans active files (``comments_*.json``, ``ready_*.json``) *and* the
    ``archived/`` originals, so a draft can be recovered even after the
    save-workflow archived its source file. Later, more-authoritative sources
    overwrite earlier ones (archived < generated < curated).
    """
    import glob

    drafts: Dict[str, Dict] = {}
    patterns = (
        os.path.join(comments_dir, "archived", "*.json"),   # lowest priority
        os.path.join(comments_dir, "comments_*.json"),
        os.path.join(comments_dir, "ready_*.json"),          # curated, highest
    )
    for pattern in patterns:
        for fp in sorted(glob.glob(pattern)):
            data = _read_json(fp)
            if data is None:
                continue
            comments = data if isinstance(data, list) else data.get("comments", []) or []
            for c in comments:
                norm = normalize_comment_fields(c)
                if norm["url"] and norm["comment"]:
                    drafts[norm["url"]] = norm
    return drafts


def reconcile(profile_name: str = None, store: PostStore = None,
              stats: Dict[str, int] = None) -> Dict[str, int]:
    """Fix drifted bins for an existing store against the authoritative files.

    The store's bin counts are the source of truth for the dashboard; this keeps
    them honest so a tab that reads the store always agrees with its bin:

      1. **COMMENTED** — any post whose URL is in ``posting_progress.json`` and
         isn't already COMMENTED (``sync_with_progress``, authoritative).
      2. **GENERATED (from files)** — any *currently-NEW* post that already has a
         draft in a comment file is moved NEW → GENERATED (its draft attached).
      3. **GENERATED integrity** — a GENERATED post whose draft comment is missing
         (status/draft got separated) has its draft *recovered* from a comment
         file if one exists, else is *demoted* back to NEW so it regenerates.
      4. **no_url** — any post still NEW with no URL is not actionable → TRASH.

    Idempotent and conservative: steps 2 and 4 only touch records still NEW, so a
    manually-trashed post is never resurrected and COMMENTED/GENERATED are never
    downgraded. Returns the reconciled bin counts; if ``stats`` is passed it is
    filled with per-rule change counts (for the diagnostic).
    """
    from . import profile_manager as pm

    store = store or PostStore(profile_name)
    if stats is None:
        stats = {}
    changed = 0

    # 1. posting_progress.json → COMMENTED (the posted ledger is authoritative).
    progress_file = pm.get_progress_file(profile_name)
    posted = []
    if os.path.exists(progress_file):
        posted = (_read_json(progress_file) or {}).get("posted_comments", []) or []
    commented = store.sync_with_progress(posted)
    changed += commented

    # Draft lookup shared by steps 2 and 3.
    drafts = _collect_drafts(pm.get_comments_dir(profile_name))

    # 2. NEW post with a draft on disk → GENERATED (only currently-NEW records).
    generated = 0
    for url, norm in drafts.items():
        rec = store._resolve(url)
        if rec is not None and rec.get("status") == NEW:
            if store.mark_generated(url, norm["comment"], meta={
                "style": norm["style"], "approach": norm["approach"],
                "word_count": norm["word_count"],
            }):
                generated += 1
    changed += generated

    # 3. GENERATED whose draft went missing → recover from a file, else demote.
    recovered, demoted = store.recover_or_demote_generated(drafts)
    changed += recovered + demoted

    # 4. Any post STILL NEW with no URL is not actionable → TRASH(no_url). Done
    #    last, so a record that reconciled to COMMENTED/GENERATED above (or was
    #    demoted then still has a URL) is never wrongly trashed here.
    trashed_no_url = store.trash_urlless_new()
    changed += trashed_no_url

    stats.update({
        "commented": commented, "generated_from_files": generated,
        "recovered_drafts": recovered, "demoted_generated": demoted,
        "trashed_no_url": trashed_no_url,
    })

    if changed:
        store.save()
        logger.info(
            "reconcile(%s): commented=%d generated=%d recovered=%d demoted=%d "
            "no_url=%d; counts now %s", profile_name, commented, generated,
            recovered, demoted, trashed_no_url, store.counts())
    return store.counts()


def load_synced_store(profile_name: str = None, migrate_if_empty: bool = True) -> PostStore:
    """Return a store reconciled with the authoritative files, seeding on first use.

    The canonical entry point for readers (the dashboard) and the comment
    generator: if the store file doesn't exist yet it's seeded from legacy files;
    then :func:`reconcile` corrects any drifted bins (falsely-NEW posts that were
    actually already commented or generated) so the counts reflect reality.
    """
    path = PostStore._default_path(profile_name)
    if migrate_if_empty and not os.path.exists(path):
        store = PostStore(profile_name, path=path)
        if not store.posts:
            migrate_from_legacy(profile_name, store=store)
    else:
        store = PostStore(profile_name, path=path)

    reconcile(profile_name, store=store)
    return store
