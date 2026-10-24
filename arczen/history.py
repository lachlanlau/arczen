# arczen/history.py
"""Chromium (Arc) browsing history into Firefox/Zen places.sqlite.

Firefox's own Chromium importer (ChromeProfileMigrator.GetHistoryResource) hands
rows to PlacesUtils.history.insertMany and lets C++ plus the SQL triggers fill in
every derived column. Writing to a closed profile from outside the browser means
reproducing that work, so each piece below is pinned to its upstream source:

  url_hash      SQLFunctions.cpp HashURL + mfbt HashString/AddU32ToHash.
                Fully specified and stable, so it is implemented directly rather
                than inferred from the target profile.
  hidden/typed  History.cpp GetHiddenState and the visit-type checks, i.e.
                derived from the visits, not copied from Chromium's columns.
  visit_count   moz_historyvisits_afterinsert_trigger, recomputed by hand only
  last_visit    when that trigger is absent from the target schema.
  frecency      Left at -1 with recalc_frecency = 1 so Zen ranks on startup.
  origin_id     moz_places_afterinsert_trigger, or moz_origins written directly
                when that trigger is absent and the column is NOT NULL.

The trigger bodies call SQL functions Firefox registers at runtime
(store_last_inserted_id, get_prefix, hash, ...). All of them are shimmed here;
one missing name aborts the entire transaction.
"""

from __future__ import annotations

import base64
import logging
import os
import sqlite3
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Sequence
from urllib.parse import urlsplit

from .paths import BackupSet, arc_history_databases

logger = logging.getLogger(__name__)

# Microseconds between the Windows epoch (1601-01-01) and the Unix epoch.
CHROME_EPOCH_OFFSET = 11_644_473_600_000_000

# mfbt/HashFunctions.h kGoldenRatioU32.
GOLDEN_RATIO = 0x9E3779B9

# History.cpp MAX_URL_LENGTH.
MAX_URL_LENGTH = 65_536

# MigrationUtils.HISTORY_MAX_AGE_IN_DAYS default, for callers wanting parity.
FIREFOX_MAX_AGE_DAYS = 180

# nsINavHistoryService transition types.
LINK = 1
TYPED = 2
BOOKMARK = 3
EMBED = 4
REDIRECT_PERMANENT = 5
REDIRECT_TEMPORARY = 6
DOWNLOAD = 7
FRAMED_LINK = 8
RELOAD = 9

# Chromium ui::PageTransition core type (low byte) -> Firefox visit_type.
TRANSITION_MAP = {
    0: LINK,          # LINK
    1: TYPED,         # TYPED
    2: BOOKMARK,      # AUTO_BOOKMARK
    3: EMBED,         # AUTO_SUBFRAME
    4: FRAMED_LINK,   # MANUAL_SUBFRAME
    5: TYPED,         # GENERATED (address bar)
    6: LINK,          # AUTO_TOPLEVEL
    7: LINK,          # FORM_SUBMIT
    8: RELOAD,        # RELOAD
    9: LINK,          # KEYWORD
    10: TYPED,        # KEYWORD_GENERATED
}

# Chromium qualifier bits riding in the high half of `transition`.
CHROMIUM_CLIENT_REDIRECT = 0x4000_0000
CHROMIUM_SERVER_REDIRECT = 0x8000_0000

# History.cpp GetHiddenState: these two never surface in the history UI.
HIDDEN_VISIT_TYPES = frozenset((EMBED, FRAMED_LINK))

# moz_historyvisits_afterinsert_trigger excludes these from visit_count.
UNCOUNTED_VISIT_TYPES = (0, EMBED, DOWNLOAD, FRAMED_LINK, RELOAD)

# Schemes History::CanAddURI refuses, plus the Chromium-internal equivalents.
SKIP_SCHEMES = (
    "about:",
    "blob:",
    "chrome:",
    "chrome-extension:",
    "chrome-untrusted:",
    "arc:",
    "data:",
    "devtools:",
    "imap:",
    "javascript:",
    "mailbox:",
    "moz-anno:",
    "moz-page-thumb:",
    "news:",
    "resource:",
    "view-source:",
    "wyciwyg:",
)

# SQLite caps a statement at 999 bound parameters by default.
CHUNK = 400

@dataclass
class HistoryOptions:
    """Knobs for the import. Defaults import everything Arc holds.

    Firefox's own migrator is lossier than this by default: max_age_days=180,
    skip_hidden=True, and one visit per URL. Those values are available here for
    callers who want exact parity.
    """

    max_age_days: int | None = None
    # Chromium sets urls.hidden for pages only ever reached by redirect or
    # subframe. Firefox's migrator filters them out with `WHERE hidden = 0`.
    skip_hidden: bool = False
    # browser.migrate.chrome.history.limit equivalent; most recent URLs first.
    limit: int | None = None
    # Clear Zen's existing history first, so the import is a straight copy.
    nuke: bool = False
    # Also delete every bookmark. Without this, bookmarked pages survive the
    # wipe because moz_bookmarks.fk points at moz_places.id.
    clear_bookmarks: bool = False
    # Arc profile folder names to read; None reads every profile.
    profiles: frozenset[str] | None = None

# --------------------------------------------------------------------------
# url_hash (SQLFunctions.cpp HashURL, mfbt HashString)
# --------------------------------------------------------------------------

def _rotate_left_5(value: int) -> int:
    return ((value << 5) | (value >> 27)) & 0xFFFFFFFF


def _add_u32(current: int, value: int) -> int:
    """mfbt AddU32ToHash."""
    return (GOLDEN_RATIO * (_rotate_left_5(current) ^ (value & 0xFFFFFFFF))) & 0xFFFFFFFF


def hash_string(text: str) -> int:
    """mfbt HashString over a nsACString.

    HashString casts the buffer to `const unsigned char*`, so bytes are mixed
    unsigned, one at a time, over the UTF-8 encoding.
    """
    result = 0
    for byte in text.encode("utf-8"):
        result = _add_u32(result, byte)
    return result


def _scheme_prefix(spec: str) -> str:
    """The text before the first colon, as HashURL takes it."""
    index = spec.find(":")
    return spec[:index] if index != -1 else ""


def url_hash(spec: str, mode: str | None = None) -> int:
    """Firefox's hash() SQL function.

    Default mode mixes the 16-bit scheme hash above the 32-bit full-spec hash.
    The prefix modes hash the whole string they are given, because callers pass
    the bare scheme: `hash("place", "prefix_lo")`.
    """
    if mode == "prefix_lo":
        return ((hash_string(spec) & 0xFFFF) << 32) + 0
    if mode == "prefix_hi":
        return ((hash_string(spec) & 0xFFFF) << 32) + 0xFFFFFFFF
    return ((hash_string(_scheme_prefix(spec)) & 0xFFFF) << 32) + hash_string(spec)


# Hashes written by other migration tools, so a poisoned row can be named
# rather than guessed at. arc2zen writes crc32(url) & 0xFFFFFFFF.
def _crc32(spec: str) -> int:
    return zlib.crc32(spec.encode("utf-8")) & 0xFFFFFFFF


MATCH = "match"
FOREIGN_CRC32 = "crc32"
NO_PREFIX = "no-prefix"
PREFIX_MISMATCH = "prefix-mismatch"
UNKNOWN = "unknown"

CLASS_DETAIL = {
    FOREIGN_CRC32: "crc32(url), the formula arc2zen writes - not a Firefox hash",
    NO_PREFIX: "below 2^32, so it carries no scheme-prefix bits at all",
    PREFIX_MISMATCH: "low 32 bits correct, scheme-prefix bits differ",
    UNKNOWN: "unrecognised formula",
}


def classify_url_hash(spec: str, stored: int) -> str:
    """Name the formula that produced one stored url_hash value."""
    if stored == url_hash(spec):
        return MATCH
    if stored == _crc32(spec):
        return FOREIGN_CRC32
    if stored < (1 << 32):
        return NO_PREFIX
    if (stored & 0xFFFFFFFF) == hash_string(spec):
        return PREFIX_MISMATCH
    return UNKNOWN


@dataclass
class HashCheck:
    """Result of asserting url_hash against the target profile's own rows."""

    checked: int = 0
    matched: int = 0
    verified: bool = True
    detail: str = "no existing rows to check against"
    counts: dict[str, int] = field(default_factory=dict)
    examples: list[tuple[str, str, int, int]] = field(default_factory=list)

    @property
    def foreign(self) -> int:
        return self.checked - self.matched

    def hash(self, spec: str, mode: str | None = None) -> int:
        return url_hash(spec, mode)


def verify_url_hash(connection: sqlite3.Connection, limit: int = 600) -> HashCheck:
    """Confirm the documented url_hash formula against this profile's own rows.

    Advisory only. One exact match settles it: a 48-bit value does not agree by
    coincidence. Rows that disagree are therefore not evidence against the
    formula, they are rows some other tool wrote with a different one -
    invisible to Zen, which looks places up as `url_hash = hash(:url) AND
    url = :url`, while still holding the UNIQUE index on url. Those get named,
    not voted on, and --nuke clears them.
    """
    rows = connection.execute(
        "SELECT url, url_hash FROM moz_places "
        "WHERE url IS NOT NULL AND url_hash IS NOT NULL AND url_hash > 0 "
        "ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()

    if not rows:
        result = HashCheck()
        logger.info("url_hash: %s; using the documented formula", result.detail)
        return result

    counts: dict[str, int] = {}
    examples: list[tuple[str, str, int, int]] = []
    for url, stored in rows:
        spec = str(url)
        value = int(stored)
        kind = classify_url_hash(spec, value)
        counts[kind] = counts.get(kind, 0) + 1
        if kind != MATCH and len(examples) < 5:
            examples.append((kind, spec, value, url_hash(spec)))

    matched = counts.get(MATCH, 0)
    summary = ", ".join(f"{kind}={count}" for kind, count in sorted(counts.items()))

    if matched:
        detail = f"matches {matched}/{len(rows)} sampled row(s)"
        if matched != len(rows):
            detail += f"; {len(rows) - matched} row(s) written by another tool ({summary})"
        result = HashCheck(
            checked=len(rows),
            matched=matched,
            verified=True,
            detail=detail,
            counts=counts,
            examples=examples,
        )
        logger.info("url_hash: %s", result.detail)
        for kind, spec, stored, computed in examples:
            logger.warning(
                "url_hash foreign row (%s): stored=%d computed=%d for %s",
                CLASS_DETAIL.get(kind, kind),
                stored,
                computed,
                spec[:70],
            )
        if matched != len(rows):
            logger.warning(
                "Those rows are invisible to Zen. Migrate with --nuke to clear them, "
                "or run 'arczen hashes --repair' to rewrite them in place."
            )
        return result

    spec, stored = str(rows[0][0]), int(rows[0][1])
    result = HashCheck(
        checked=len(rows),
        matched=0,
        verified=False,
        detail=(
            f"not one of {len(rows)} sampled row(s) matches ({summary}); "
            f"stored={stored} computed={url_hash(spec)} for {spec[:60]!r}"
        ),
        counts=counts,
        examples=examples,
    )
    logger.error("url_hash: %s", result.detail)
    return result


def audit_url_hashes(
    connection: sqlite3.Connection,
) -> tuple[dict[str, int], list[tuple[str, str, int, int]]]:
    """Classify every row in moz_places. Returns (counts, up to 10 examples)."""
    counts: dict[str, int] = {}
    examples: list[tuple[str, str, int, int]] = []

    for url, stored in connection.execute(
        "SELECT url, url_hash FROM moz_places WHERE url IS NOT NULL ORDER BY id"
    ):
        spec = str(url)
        value = int(stored or 0)
        kind = classify_url_hash(spec, value)
        counts[kind] = counts.get(kind, 0) + 1
        if kind != MATCH and len(examples) < 10:
            examples.append((kind, spec, value, url_hash(spec)))

    return counts, examples


def repair_url_hashes(
    connection: sqlite3.Connection,
    dry_run: bool = False,
) -> int:
    """Rewrite url_hash on every row that does not match Firefox's formula.

    The index on url_hash is not unique and the UNIQUE constraint is on url, so
    correcting the value cannot collide. Rows become reachable from Zen again.
    """
    broken: list[tuple[int, int]] = []
    for place_id, url, stored in connection.execute(
        "SELECT id, url, url_hash FROM moz_places WHERE url IS NOT NULL"
    ):
        spec = str(url)
        computed = url_hash(spec)
        if int(stored or 0) != computed:
            broken.append((computed, int(place_id)))

    if dry_run or not broken:
        return len(broken)

    recalc = "recalc_frecency" in _columns(connection, "moz_places")
    assignment = "url_hash = ?" + (", recalc_frecency = 1" if recalc else "")

    connection.execute("BEGIN IMMEDIATE")
    connection.executemany(f"UPDATE moz_places SET {assignment} WHERE id = ?", broken)
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    logger.info("Rewrote url_hash on %d row(s)", len(broken))
    return len(broken)


# --------------------------------------------------------------------------
# Firefox URL helpers (mirror mozilla::places)
# --------------------------------------------------------------------------

def _get_prefix(spec: str | None) -> str:
    if not spec:
        return ""
    index = spec.find("://")
    if index != -1:
        return spec[: index + 3]
    index = spec.find(":")
    return spec[: index + 1] if index != -1 else ""


def _get_host_and_port(spec: str | None) -> str:
    if not spec:
        return ""
    try:
        parts = urlsplit(spec)
    except ValueError:
        return ""
    netloc = parts.netloc
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    return netloc.lower().rstrip(".")


def _strip_prefix_and_userinfo(spec: str | None) -> str:
    if not spec:
        return ""
    rest = spec[len(_get_prefix(spec)) :]
    head, separator, tail = rest.partition("/")
    if "@" in head:
        head = head.rsplit("@", 1)[1]
    return head + separator + tail


def _get_unreversed_host(rev_host: str | None) -> str:
    if not rev_host:
        return ""
    return rev_host.rstrip(".")[::-1]


def _fixup_url(host: str | None) -> str:
    if not host:
        return ""
    return host[4:] if host.startswith("www.") else host


def _rev_host(spec: str) -> str:
    host = _get_host_and_port(spec).split(":")[0]
    return (host[::-1] + ".") if host else "."


def _guid() -> str:
    return base64.urlsafe_b64encode(os.urandom(9)).decode("ascii")


def _chrome_to_firefox(timestamp: object) -> int | None:
    """Chromium microseconds-since-1601 to Firefox microseconds-since-1970."""
    try:
        raw = int(timestamp or 0)
    except (TypeError, ValueError):
        return None
    if not raw:
        return None
    value = raw - CHROME_EPOCH_OFFSET
    return value if value > 0 else None


def _visit_type(transition: object) -> int:
    """Chromium transition word -> Firefox visit_type.

    Chromium binds the transition as a signed int, so the qualifier bits can
    arrive negative; mask to 32 bits before reading them.
    """
    try:
        word = int(transition or 0) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return LINK

    if word & CHROMIUM_SERVER_REDIRECT:
        return REDIRECT_PERMANENT
    if word & CHROMIUM_CLIENT_REDIRECT:
        return REDIRECT_TEMPORARY
    return TRANSITION_MAP.get(word & 0xFF, LINK)


def _can_add_url(spec: str) -> bool:
    """Mirror History::CanAddURI."""
    if not spec or len(spec) > MAX_URL_LENGTH:
        return False
    return not spec.lower().startswith(SKIP_SCHEMES)


# --------------------------------------------------------------------------
# SQL function shims
# --------------------------------------------------------------------------

def _register_functions(
    connection: sqlite3.Connection,
    hash_check: HashCheck | None = None,
) -> None:
    """Register the SQL functions Firefox's triggers expect.

    Plain sqlite3 has none of them, so an INSERT into moz_places fails with
    'no such function' and takes the whole transaction with it. Every function
    is registered variadically so argument-count differences between schema
    versions cannot reintroduce that failure.
    """

    def _hash(*args: object) -> int:
        spec = str(args[0] or "") if args else ""
        mode = str(args[1]) if len(args) > 1 and args[1] else None
        return url_hash(spec, mode)

    def _one_arg(function: Callable[[str | None], str]) -> Callable[..., str]:
        def wrapper(*args: object) -> str:
            return function(str(args[0]) if args and args[0] is not None else None)
        return wrapper

    connection.create_function("hash", -1, _hash)
    connection.create_function("get_prefix", -1, _one_arg(_get_prefix))
    connection.create_function("get_host_and_port", -1, _one_arg(_get_host_and_port))
    connection.create_function(
        "strip_prefix_and_userinfo", -1, _one_arg(_strip_prefix_and_userinfo)
    )
    connection.create_function("get_unreversed_host", -1, _one_arg(_get_unreversed_host))
    connection.create_function("fixup_url", -1, _one_arg(_fixup_url))

    # Bookkeeping hooks. Firefox uses these to notify observers and to hand the
    # last inserted rowid back to C++. Outside the browser they are no-ops, but
    # they must exist: moz_places_afterinsert_trigger calls
    # store_last_inserted_id on its first line.
    connection.create_function("store_last_inserted_id", -1, lambda *_: None)
    connection.create_function("note_sync_change", -1, lambda *_: None)
    connection.create_function("invalidate_days_of_history", -1, lambda *_: None)
    connection.create_function("set_should_start_frecency_recalculation", -1, lambda *_: None)
    connection.create_function("should_start_frecency_recalculation", -1, lambda *_: 0)
    connection.create_function("is_frecency_decaying", -1, lambda *_: 0)
    # -1 is Places' "needs recalculation" sentinel, which is what we want.
    connection.create_function("calculate_frecency", -1, lambda *_: -1)
    connection.create_function("autocomplete_match", -1, lambda *_: 0)
    connection.create_function("generate_guid", -1, lambda *_: _guid())
    connection.create_function("make_guid", -1, lambda *_: _guid())
    connection.create_function("get_query_param", -1, lambda *_: None)


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


# --------------------------------------------------------------------------
# Target schema
# --------------------------------------------------------------------------

@dataclass
class _Schema:
    place_columns: set[str] = field(default_factory=set)
    place_notnull: set[str] = field(default_factory=set)
    visit_columns: set[str] = field(default_factory=set)
    origin_columns: set[str] = field(default_factory=set)
    triggers: set[str] = field(default_factory=set)

    @classmethod
    def read(cls, connection: sqlite3.Connection) -> "_Schema":
        place_columns: set[str] = set()
        place_notnull: set[str] = set()
        for row in connection.execute("PRAGMA table_info(moz_places)"):
            place_columns.add(row[1])
            if row[3]:
                place_notnull.add(row[1])

        origin_columns: set[str] = set()
        if cls._table_exists(connection, "moz_origins"):
            origin_columns = _columns(connection, "moz_origins")

        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }

        return cls(
            place_columns=place_columns,
            place_notnull=place_notnull,
            visit_columns=_columns(connection, "moz_historyvisits"),
            origin_columns=origin_columns,
            triggers=triggers,
        )

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
        return bool(
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
        )

    @property
    def place_insert_trigger(self) -> bool:
        return any(name.startswith("moz_places_afterinsert") for name in self.triggers)

    @property
    def visit_insert_trigger(self) -> bool:
        return any(
            name.startswith("moz_historyvisits_afterinsert") for name in self.triggers
        )

    @property
    def needs_manual_origin(self) -> bool:
        """origin_id is mandatory and no trigger will fill it in."""
        return (
            "origin_id" in self.place_columns
            and "origin_id" in self.place_notnull
            and not self.place_insert_trigger
            and bool(self.origin_columns)
        )


def _chunks(values: Sequence[int], size: int = CHUNK) -> Iterator[Sequence[int]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


# --------------------------------------------------------------------------
# Clearing
# --------------------------------------------------------------------------

# Tables that hold history-derived rows and nothing else. Zen's own Clear
# History empties all of them.
HISTORY_ONLY_TABLES = (
    "moz_historyvisits",
    "moz_inputhistory",
    "moz_places_metadata",
    "moz_places_metadata_search_queries",
    "moz_previews_tombstones",
)

# Tables keyed to a place that only matter while the place survives.
PLACE_DEPENDENT_TABLES = (
    ("moz_annos", "place_id"),
    ("moz_places_extra", "place_id"),
)

# Bookmark roots Firefox recreates on sight. Every other moz_bookmarks row
# chains back to one of these through `parent`, so they stay and are emptied.
BOOKMARK_ROOT_GUIDS = (
    "root________",
    "menu________",
    "toolbar_____",
    "unfiled_____",
    "mobile______",
    "tags________",
)

# Tables that hold bookmark-derived rows and nothing else.
BOOKMARK_ONLY_TABLES = (
    "moz_keywords",
    "moz_tags_relation",
    "moz_tags",
    "moz_bookmarks_deleted",
)

# favicons.sqlite, cleared alongside a full wipe so no icon outlives its page.
FAVICON_TABLES = ("moz_icons_to_pages", "moz_pages_w_icons", "moz_icons")


def clear_bookmarks(connection: sqlite3.Connection, schema: _Schema) -> int:
    """Delete every bookmark, keeping the six roots in place but empty.

    Mirrors PlacesUtils.bookmarks.eraseEverything. Must run before the place
    keep-set is built, since it is moz_bookmarks.fk that protects places.
    Returns the number of bookmark rows removed.
    """
    marks = ", ".join("?" for _ in BOOKMARK_ROOT_GUIDS)

    roots = [
        int(row[0])
        for row in connection.execute(
            f"SELECT id FROM moz_bookmarks WHERE guid IN ({marks})",
            BOOKMARK_ROOT_GUIDS,
        )
    ]
    if not roots:
        logger.warning(
            "moz_bookmarks has none of the expected root GUIDs; leaving bookmarks "
            "alone rather than risking the tree"
        )
        return 0

    doomed = connection.execute(
        f"SELECT count(*) FROM moz_bookmarks WHERE guid NOT IN ({marks})",
        BOOKMARK_ROOT_GUIDS,
    ).fetchone()[0]

    # Annotations on the roots themselves are kept; everything else goes.
    if _Schema._table_exists(connection, "moz_items_annos"):
        root_marks = ", ".join("?" for _ in roots)
        connection.execute(
            f"DELETE FROM moz_items_annos WHERE item_id NOT IN ({root_marks})",
            tuple(roots),
        )

    for table in BOOKMARK_ONLY_TABLES:
        if _Schema._table_exists(connection, table):
            connection.execute(f"DELETE FROM {table}")

    connection.execute(
        f"DELETE FROM moz_bookmarks WHERE guid NOT IN ({marks})",
        BOOKMARK_ROOT_GUIDS,
    )

    # The afterdelete trigger decrements this, but force it so no place stays
    # protected on a schema without the trigger.
    if "foreign_count" in schema.place_columns:
        connection.execute("UPDATE moz_places SET foreign_count = 0")

    # The deletes above refill the sync tombstone table; empty it again.
    if _Schema._table_exists(connection, "moz_bookmarks_deleted"):
        connection.execute("DELETE FROM moz_bookmarks_deleted")

    logger.info("Cleared %d bookmark(s), leaving the roots empty", doomed)
    return int(doomed)


def clear_history(
    connection: sqlite3.Connection,
    schema: _Schema,
    bookmarks: bool = False,
) -> tuple[int, int, int]:
    """Delete every visit and place, optionally every bookmark with them.

    Without `bookmarks`, pages referenced by moz_bookmarks.fk or
    moz_keywords.place_id are kept and have their counters reset, because
    deleting them would leave those bookmarks pointing at nothing.

    Must be called inside the caller's transaction. Returns
    (places deleted, visits deleted, bookmarks deleted).
    """
    visits = connection.execute("SELECT count(*) FROM moz_historyvisits").fetchone()[0]

    # Bookmarks first: they are what protects places from deletion.
    removed_bookmarks = clear_bookmarks(connection, schema) if bookmarks else 0

    # Protected place ids, gathered from the referencing tables directly rather
    # than trusting foreign_count, which can drift.
    connection.execute(
        "CREATE TEMP TABLE IF NOT EXISTS arczen_keep (id INTEGER PRIMARY KEY)"
    )
    connection.execute("DELETE FROM arczen_keep")

    if "foreign_count" in schema.place_columns:
        connection.execute(
            "INSERT OR IGNORE INTO arczen_keep "
            "SELECT id FROM moz_places WHERE foreign_count > 0"
        )
    for table, column in (("moz_bookmarks", "fk"), ("moz_keywords", "place_id")):
        if _Schema._table_exists(connection, table):
            connection.execute(
                f"INSERT OR IGNORE INTO arczen_keep "
                f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL"
            )

    doomed = connection.execute(
        "SELECT count(*) FROM moz_places WHERE id NOT IN (SELECT id FROM arczen_keep)"
    ).fetchone()[0]

    for table in HISTORY_ONLY_TABLES:
        if _Schema._table_exists(connection, table):
            connection.execute(f"DELETE FROM {table}")

    for table, column in PLACE_DEPENDENT_TABLES:
        if _Schema._table_exists(connection, table):
            connection.execute(
                f"DELETE FROM {table} WHERE {column} NOT IN (SELECT id FROM arczen_keep)"
            )

    connection.execute(
        "DELETE FROM moz_places WHERE id NOT IN (SELECT id FROM arczen_keep)"
    )

    # Reset any survivors so they carry no stale visit history.
    resets = ["visit_count = 0", "last_visit_date = NULL"]
    if "frecency" in schema.place_columns:
        resets.append("frecency = -1")
    for name in ("recalc_frecency", "recalc_alt_frecency"):
        if name in schema.place_columns:
            resets.append(f"{name} = 1")
    connection.execute(f"UPDATE moz_places SET {', '.join(resets)}")

    # Survivors keep whatever url_hash wrote them, and a foreign value makes the
    # page invisible to Zen, which matches on `url_hash = hash(:url) AND
    # url = :url`. The index on url_hash is not unique, so rewriting is safe.
    stale: list[tuple[int, int]] = []
    for place_id, url, stored in connection.execute(
        "SELECT id, url, url_hash FROM moz_places WHERE url IS NOT NULL"
    ):
        computed = url_hash(str(url))
        if int(stored or 0) != computed:
            stale.append((computed, int(place_id)))
    if stale:
        connection.executemany("UPDATE moz_places SET url_hash = ? WHERE id = ?", stale)
        logger.info(
            "Rewrote url_hash on %d surviving page(s) that another tool had written "
            "with a different formula",
            len(stale),
        )

    # Drop origins nothing points at any more. The afterdelete trigger does
    # this when it exists, in which case there is nothing left to find.
    if schema.origin_columns:
        connection.execute(
            "DELETE FROM moz_origins WHERE id NOT IN "
            "(SELECT origin_id FROM moz_places WHERE origin_id IS NOT NULL)"
        )

    connection.execute("DROP TABLE IF EXISTS arczen_keep")

    logger.info(
        "Cleared Zen history: %d place(s), %d visit(s), %d bookmark(s)",
        doomed,
        visits,
        removed_bookmarks,
    )
    return int(doomed), int(visits), removed_bookmarks


def clear_favicons(
    zen_profile: Path,
    backups: BackupSet,
    dry_run: bool = False,
) -> int:
    """Empty favicons.sqlite, so no cached icon outlives the page it came from."""
    database = zen_profile / "favicons.sqlite"
    if not database.is_file():
        return 0

    if dry_run:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            return int(connection.execute("SELECT count(*) FROM moz_icons").fetchone()[0])
        except sqlite3.Error:
            return 0
        finally:
            connection.close()

    for name in ("favicons.sqlite", "favicons.sqlite-wal", "favicons.sqlite-shm"):
        backups.save(zen_profile / name)

    connection = sqlite3.connect(str(database))
    try:
        total = int(connection.execute("SELECT count(*) FROM moz_icons").fetchone()[0])
        connection.execute("BEGIN IMMEDIATE")
        for table in FAVICON_TABLES:
            if _Schema._table_exists(connection, table):
                connection.execute(f"DELETE FROM {table}")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        logger.info("Cleared %d cached favicon(s)", total)
        return total
    except sqlite3.Error as error:
        connection.rollback()
        logger.warning("Could not clear favicons.sqlite: %s", error)
        return 0
    finally:
        connection.close()


def nuke_places(
    zen_profile: Path,
    backups: BackupSet,
    bookmarks: bool = True,
    dry_run: bool = False,
) -> dict[str, int]:
    """Standalone wipe, for when the history import is disabled.

    migrate_history does its own clearing inside the import transaction; this
    is the path for `--nuke --no-history`.
    """
    result = {"cleared_places": 0, "cleared_visits": 0, "cleared_bookmarks": 0}

    places = zen_profile / "places.sqlite"
    if not places.is_file():
        logger.info("No places.sqlite to clear")
        return result

    if dry_run:
        connection = sqlite3.connect(f"file:{places}?mode=ro", uri=True)
        try:
            result["cleared_visits"] = int(
                connection.execute("SELECT count(*) FROM moz_historyvisits").fetchone()[0]
            )
            if bookmarks:
                result["cleared_places"] = int(
                    connection.execute("SELECT count(*) FROM moz_places").fetchone()[0]
                )
                marks = ", ".join("?" for _ in BOOKMARK_ROOT_GUIDS)
                result["cleared_bookmarks"] = int(
                    connection.execute(
                        f"SELECT count(*) FROM moz_bookmarks WHERE guid NOT IN ({marks})",
                        BOOKMARK_ROOT_GUIDS,
                    ).fetchone()[0]
                )
            else:
                result["cleared_places"] = int(
                    connection.execute(
                        "SELECT count(*) FROM moz_places "
                        "WHERE coalesce(foreign_count, 0) = 0"
                    ).fetchone()[0]
                )
        except sqlite3.Error as error:
            logger.warning("Could not read %s: %s", places, error)
        finally:
            connection.close()
        logger.info(
            "Dry run: would clear %d place(s), %d visit(s), %d bookmark(s)",
            result["cleared_places"],
            result["cleared_visits"],
            result["cleared_bookmarks"],
        )
        return result

    for name in ("places.sqlite", "places.sqlite-wal", "places.sqlite-shm"):
        backups.save(zen_profile / name)

    connection = sqlite3.connect(str(places))
    try:
        _register_functions(connection)
        schema = _Schema.read(connection)
        if not schema.place_columns:
            logger.warning("places.sqlite has no moz_places table; nothing cleared")
            return result

        connection.execute("BEGIN IMMEDIATE")
        cleared = clear_history(connection, schema, bookmarks=bookmarks)
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        result["cleared_places"], result["cleared_visits"], result["cleared_bookmarks"] = cleared
        return result
    except sqlite3.Error as error:
        connection.rollback()
        logger.error("Clearing places.sqlite rolled back: %s", error)
        return result
    finally:
        connection.close()

# --------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------

def _new_history_stats() -> dict[str, object]:
    return {
        "inserted": 0,
        "updated": 0,
        "visits": 0,
        "skipped": 0,
        "duplicate_visits": 0,
        "cleared_places": 0,
        "cleared_visits": 0,
        "cleared_bookmarks": 0,
        "cleared_icons": 0,
        "aborted": False,
        "detail": "",
    }

def migrate_history(
    arc_root: Path,
    zen_profile: Path,
    backups: BackupSet,
    dry_run: bool = False,
    options: HistoryOptions | None = None,
) -> dict[str, object]:
    """Copy Arc browsing history into Zen's places.sqlite."""
    options = options or HistoryOptions()
    stats = _new_history_stats()

    databases = arc_history_databases(arc_root, options.profiles)
    if not databases:
        stats["aborted"] = True
        stats["detail"] = "no Arc History databases were found"
        logger.info("No Arc History databases found; skipping history")
        return stats

    places = zen_profile / "places.sqlite"
    if not places.is_file():
        stats["aborted"] = True
        stats["detail"] = (
            "the Zen profile has no places.sqlite; launch Zen once and quit it first"
        )
        logger.warning("No places.sqlite in the Zen profile; skipping history")
        return stats

    if dry_run:
        for database in databases:
            source = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            try:
                urls = source.execute("SELECT count(*) FROM urls").fetchone()[0]
                visits = source.execute("SELECT count(*) FROM visits").fetchone()[0]
            except sqlite3.Error as error:
                logger.warning("Could not read %s: %s", database, error)
                continue
            finally:
                source.close()
            stats["inserted"] = int(stats["inserted"]) + urls
            stats["visits"] = int(stats["visits"]) + visits

        if options.nuke:
            planned = nuke_places(
                zen_profile, backups, bookmarks=options.clear_bookmarks, dry_run=True
            )
            stats.update(planned)

        stats["detail"] = "dry run: counts are Arc source rows, not rows that would be written"
        logger.info(
            "Dry run: would consider %d URL(s) and %d visit(s) from %d Arc profile(s)",
            stats["inserted"],
            stats["visits"],
            len(databases),
        )
        return stats

    for name in ("places.sqlite", "places.sqlite-wal", "places.sqlite-shm"):
        backups.save(zen_profile / name)

    target = sqlite3.connect(str(places))
    try:
        _register_functions(target)
        schema = _Schema.read(target)

        if not schema.place_columns:
            stats["aborted"] = True
            stats["detail"] = "places.sqlite has no moz_places table"
            logger.error("No moz_places table in %s", places)
            return stats

        if schema.needs_manual_origin:
            logger.info(
                "moz_places.origin_id is NOT NULL with no afterinsert trigger; "
                "moz_origins will be populated directly"
            )

        touched: set[int] = set()

        # The wipe and the import share one transaction, so a failed import
        # cannot leave the profile with its history deleted and nothing written.
        target.execute("BEGIN IMMEDIATE")

        if options.nuke:
            cleared = clear_history(target, schema, bookmarks=options.clear_bookmarks)
            stats["cleared_places"] = cleared[0]
            stats["cleared_visits"] = cleared[1]
            stats["cleared_bookmarks"] = cleared[2]

        # Advisory only. The formula comes from SQLFunctions.cpp and is not in
        # question; this just names rows some other tool wrote, which Zen cannot
        # see. After a wipe the only rows left are Firefox's own.
        check = verify_url_hash(target)
        if not check.verified:
            logger.warning(
                "No sampled row matched Firefox's url_hash (%s). Importing anyway; "
                "run 'arczen hashes' to see what wrote them.",
                check.detail,
            )

        # moz_places.url is UNIQUE, so a blind insert of a URL Zen already
        # holds would roll the whole transaction back. Bookmarked pages survive
        # a wipe, so this map is needed even with --nuke.
        existing: dict[str, int] = {
            row[1]: row[0] for row in target.execute("SELECT id, url FROM moz_places")
        }
        logger.info("Zen holds %d place(s) going into the import", len(existing))

        for database in databases:
            _ingest(database, target, existing, schema, options, stats, touched)

        _finalise(target, schema, touched)

        target.commit()
        target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error as error:
        target.rollback()
        logger.error("History import rolled back: %s", error)
        failed = _new_history_stats()
        failed["aborted"] = True
        failed["detail"] = f"places.sqlite write failed and was rolled back: {error}"
        return failed
    finally:
        target.close()

    stats["detail"] = f"url_hash {check.detail}"
    logger.info(
        "History: %d new URL(s), %d already present, %d visit(s), %d duplicate visit(s) "
        "skipped, %d URL(s) skipped",
        stats["inserted"],
        stats["updated"],
        stats["visits"],
        stats["duplicate_visits"],
        stats["skipped"],
    )
    return stats


def _ensure_origin(
    target: sqlite3.Connection,
    schema: _Schema,
    url: str,
    cache: dict[tuple[str, str], int],
) -> int | None:
    """Create or find the moz_origins row for a URL when no trigger will."""
    prefix = _get_prefix(url)
    host = _get_host_and_port(url)
    key = (prefix, host)
    if key in cache:
        return cache[key]

    row = target.execute(
        "SELECT id FROM moz_origins WHERE prefix = ? AND host = ?", (prefix, host)
    ).fetchone()
    if row:
        cache[key] = int(row[0])
        return cache[key]

    values = {
        "prefix": prefix,
        "host": host,
        "frecency": -1,
        "recalc_frecency": 1,
        "alt_frecency": None,
        "recalc_alt_frecency": 1,
    }
    usable = {name: value for name, value in values.items() if name in schema.origin_columns}
    names = ", ".join(usable)
    marks = ", ".join("?" for _ in usable)
    cursor = target.execute(
        f"INSERT INTO moz_origins ({names}) VALUES ({marks})", tuple(usable.values())
    )
    cache[key] = int(cursor.lastrowid)
    return cache[key]


def _read_arc(
    database: Path,
) -> tuple[dict[int, dict[str, object]], list[sqlite3.Row]] | None:
    """Pull the urls and visits tables out of one Arc profile."""
    source = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        available = {row[1] for row in source.execute("PRAGMA table_info(urls)")}
        wanted = [
            name
            for name in ("id", "url", "title", "typed_count", "last_visit_time", "hidden")
            if name in available
        ]
        if "id" not in wanted or "url" not in wanted:
            logger.warning("Unexpected urls schema in %s", database)
            return None

        urls = {
            int(row["id"]): dict(row)
            for row in source.execute(f"SELECT {', '.join(wanted)} FROM urls")
        }
        visits = source.execute(
            "SELECT id, url, visit_time, transition, from_visit FROM visits "
            "ORDER BY visit_time, id"
        ).fetchall()
        return urls, visits
    except sqlite3.Error as error:
        logger.warning("Could not read %s: %s", database, error)
        return None
    finally:
        source.close()


def _ingest(
    database: Path,
    target: sqlite3.Connection,
    existing: dict[str, int],
    schema: _Schema,
    options: HistoryOptions,
    stats: dict[str, object],
    touched: set[int],
) -> None:
    read = _read_arc(database)
    if read is None:
        return
    urls, visits = read

    now_microseconds = int(time.time() * 1_000_000)
    cutoff = (
        now_microseconds - options.max_age_days * 86_400 * 1_000_000
        if options.max_age_days
        else None
    )

    # Group visits by Chromium url id, converting and filtering as we go, so
    # that hidden and typed can be derived from them the way History.cpp does.
    by_url: dict[int, list[tuple[int, int, int, int]]] = {}
    for row in visits:
        visit_date = _chrome_to_firefox(row["visit_time"])
        if visit_date is None:
            continue
        if cutoff is not None and visit_date < cutoff:
            continue
        # insertVisitsWrapper clamps future dates to now.
        visit_date = min(visit_date, now_microseconds)
        by_url.setdefault(int(row["url"]), []).append(
            (
                int(row["id"]),
                visit_date,
                _visit_type(row["transition"]),
                int(row["from_visit"] or 0),
            )
        )

    # Firefox's migrator synthesises exactly one visit per URL from
    # last_visit_time. Do the same for URLs whose visit rows Chromium expired.
    for chromium_id, row in urls.items():
        if chromium_id in by_url:
            continue
        visit_date = _chrome_to_firefox(row.get("last_visit_time"))
        if visit_date is None:
            continue
        if cutoff is not None and visit_date < cutoff:
            continue
        kind = TYPED if (row.get("typed_count") or 0) > 0 else LINK
        by_url[chromium_id] = [(-chromium_id, min(visit_date, now_microseconds), kind, 0)]

    candidates = [chromium_id for chromium_id in urls if chromium_id in by_url]

    if options.limit:
        candidates.sort(key=lambda key: max(entry[1] for entry in by_url[key]), reverse=True)
        dropped = len(candidates) - options.limit
        if dropped > 0:
            stats["skipped"] = int(stats["skipped"]) + dropped
        candidates = candidates[: options.limit]

    origin_cache: dict[tuple[str, str], int] = {}
    id_map: dict[int, int] = {}
    reused: set[int] = set()

    for chromium_id in candidates:
        row = urls[chromium_id]
        url = str(row.get("url") or "")

        if not _can_add_url(url):
            stats["skipped"] = int(stats["skipped"]) + 1
            continue
        if options.skip_hidden and int(row.get("hidden") or 0):
            stats["skipped"] = int(stats["skipped"]) + 1
            continue

        if url in existing:
            place_id = existing[url]
            id_map[chromium_id] = place_id
            reused.add(place_id)
            touched.add(place_id)
            stats["updated"] = int(stats["updated"]) + 1
            continue

        kinds = {entry[2] for entry in by_url[chromium_id]}
        values: dict[str, object] = {
            "url": url,
            "title": row.get("title") or None,
            "rev_host": _rev_host(url),
            # GetHiddenState: a place is hidden only while every visit is an
            # embed or a framed link.
            "hidden": 1 if kinds and kinds <= HIDDEN_VISIT_TYPES else 0,
            "typed": 1 if TYPED in kinds else 0,
            "frecency": -1,
            "guid": _guid(),
            "url_hash": url_hash(url),
            "recalc_frecency": 1,
            "recalc_alt_frecency": 1,
        }

        if schema.needs_manual_origin:
            values["origin_id"] = _ensure_origin(target, schema, url, origin_cache)

        usable = {name: value for name, value in values.items() if name in schema.place_columns}
        names = ", ".join(usable)
        marks = ", ".join("?" for _ in usable)
        cursor = target.execute(
            f"INSERT INTO moz_places ({names}) VALUES ({marks})", tuple(usable.values())
        )
        place_id = int(cursor.lastrowid)
        existing[url] = place_id
        id_map[chromium_id] = place_id
        touched.add(place_id)
        stats["inserted"] = int(stats["inserted"]) + 1

    if not id_map:
        return

    # Only places that already existed can already hold visits, so that is the
    # only set worth de-duplicating against. Without this a second run doubles
    # every visit_count.
    seen: set[tuple[int, int]] = set()
    for chunk in _chunks(sorted(reused)):
        placeholders = ", ".join("?" for _ in chunk)
        for place_id, visit_date in target.execute(
            f"SELECT place_id, visit_date FROM moz_historyvisits "
            f"WHERE place_id IN ({placeholders})",
            tuple(chunk),
        ):
            seen.add((int(place_id), int(visit_date)))

    visit_id_map: dict[int, int] = {}

    # Chronological across the whole profile so from_visit can resolve.
    ordered = sorted(
        (
            (entry, chromium_id)
            for chromium_id in id_map
            for entry in by_url[chromium_id]
        ),
        key=lambda item: (item[0][1], item[0][0]),
    )

    for (visit_id, visit_date, kind, from_visit), chromium_id in ordered:
        place_id = id_map[chromium_id]

        key = (place_id, visit_date)
        if key in seen:
            stats["duplicate_visits"] = int(stats["duplicate_visits"]) + 1
            continue
        seen.add(key)

        values = {
            "place_id": place_id,
            "visit_date": visit_date,
            "visit_type": kind,
            # Preserve Chromium's redirect chain where the referring visit has
            # already been written; 0 is Firefox's "no referring visit".
            "from_visit": visit_id_map.get(from_visit, 0),
            "session": 0,
            "source": 0,
            "triggeringPlaceId": None,
        }
        usable = {name: value for name, value in values.items() if name in schema.visit_columns}
        names = ", ".join(usable)
        marks = ", ".join("?" for _ in usable)
        cursor = target.execute(
            f"INSERT INTO moz_historyvisits ({names}) VALUES ({marks})",
            tuple(usable.values()),
        )
        visit_id_map[visit_id] = int(cursor.lastrowid)
        stats["visits"] = int(stats["visits"]) + 1


def _finalise(target: sqlite3.Connection, schema: _Schema, touched: set[int]) -> None:
    """Fill in the counters the triggers would normally maintain, then flag recalc."""
    if not touched:
        return

    ids = sorted(touched)

    if not schema.visit_insert_trigger:
        uncounted = ", ".join(str(value) for value in UNCOUNTED_VISIT_TYPES)
        for chunk in _chunks(ids):
            placeholders = ", ".join("?" for _ in chunk)
            target.execute(
                f"""
                UPDATE moz_places SET
                    visit_count = (
                        SELECT count(*) FROM moz_historyvisits
                        WHERE place_id = moz_places.id
                          AND visit_type NOT IN ({uncounted})
                    ),
                    last_visit_date = (
                        SELECT max(visit_date) FROM moz_historyvisits
                        WHERE place_id = moz_places.id
                    )
                WHERE id IN ({placeholders})
                """,
                tuple(chunk),
            )

    # Let Firefox rank the imported rows itself on next startup.
    recalc = [
        name
        for name in ("recalc_frecency", "recalc_alt_frecency")
        if name in schema.place_columns
    ]
    if recalc:
        assignments = ", ".join(f"{name} = 1" for name in recalc)
        for chunk in _chunks(ids):
            placeholders = ", ".join("?" for _ in chunk)
            target.execute(
                f"UPDATE moz_places SET {assignments} WHERE id IN ({placeholders})",
                tuple(chunk),
            )

    if "recalc_frecency" in schema.origin_columns:
        for chunk in _chunks(ids):
            placeholders = ", ".join("?" for _ in chunk)
            target.execute(
                f"UPDATE moz_origins SET recalc_frecency = 1 WHERE id IN "
                f"(SELECT origin_id FROM moz_places WHERE id IN ({placeholders}) "
                f"AND origin_id IS NOT NULL)",
                tuple(chunk),
            )