# arczen/privacy.py
"""Clear Zen's own site data, for replace mode.

Replace rebuilds the profile's records from Arc, so anything identifying the
previous occupant goes with them: cookies, site storage, saved passwords, form
history, permissions and per-site settings. places.sqlite and favicons.sqlite
are not touched here; history.py owns those.

Out of scope, because removing them breaks the profile rather than cleaning it:
  extensions and their storage   extensions/, storage/**/moz-extension+++*
  NSS key and certificate stores key4.db, cert9.db
  preferences                    prefs.js, which arczen writes to directly
  the account signed in to sync  signedInUser.json
The HTTP cache lives outside the profile directory and is not reachable here.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .paths import BackupSet

logger = logging.getLogger(__name__)

# Databases emptied in full. The third column names the table whose row count
# is worth reporting; None means wipe it but say nothing about the size.
DATABASE_JOBS: tuple[tuple[str, str, str | None], ...] = (
    ("cookies.sqlite", "cookies", "moz_cookies"),
    ("permissions.sqlite", "permissions", "moz_perms"),
    ("formhistory.sqlite", "form_entries", "moz_formhistory"),
    ("webappsstore.sqlite", "local_storage", "webappsstore2"),
    ("content-prefs.sqlite", "site_settings", "prefs"),
    ("protections.sqlite", "tracker_events", "events"),
    ("credentialstate.sqlite", "credential_state", None),
    ("tabnotes.sqlite", "tab_notes", None),
)

# Written alongside a database; backed up and removed with it.
COMPANION_SUFFIXES = ("-wal", "-shm", "-journal")

# Files removed outright. Firefox recreates every one of them on demand.
REMOVE_FILES: tuple[str, ...] = (
    "logins-backup.json",          # second copy of the password store
    "signons.sqlite",              # legacy password store
    "signons3.txt",                # older legacy password store
    "autofill-profiles.json",      # saved addresses and payment cards
    "storage.sqlite",              # quota manager index, rebuilt from origins
    "storage.sqlite-wal",
    "storage.sqlite-shm",
    "ls-archive.sqlite",           # legacy localStorage archive
    "ls-archive-tmp.sqlite",
    "SiteSecurityServiceState.bin",  # HSTS, a record of sites visited
    "SiteSecurityServiceState.txt",
)

# Quota manager trees holding per-origin storage.
STORAGE_ROOTS = ("default", "temporary", "permanent")

# Origin directory prefixes that belong to the web. Everything else in those
# trees (moz-extension, about, chrome, resource) is left alone.
WEB_ORIGIN_PREFIXES = ("http+++", "https+++", "file+++", "ftp+++", "ws+++", "wss+++")

# Shape about:logins expects from an empty store.
BLANK_LOGINS: dict[str, object] = {
    "nextId": 1,
    "logins": [],
    "potentiallyVulnerablePasswords": [],
    "dismissedBreachAlertsByLoginGUID": {},
    "version": 3,
}


@dataclass
class SiteDataReport:
    """Counts for the summary, plus anything the caller should surface."""

    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def add(self, key: str, value: int) -> None:
        if value:
            self.counts[key] = self.counts.get(key, 0) + value


def _user_tables(connection: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    ]


def _backup_database(path: Path, backups: BackupSet) -> None:
    backups.save(path)
    for suffix in COMPANION_SUFFIXES:
        backups.save(path.with_name(path.name + suffix))


def _count_rows(path: Path, table: str | None) -> int:
    """Row count from a read-only handle, for the dry run and for reporting."""
    if not table:
        return 0
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return 0
    try:
        return int(connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
    except sqlite3.Error:
        return 0
    finally:
        connection.close()


def _wipe_database(
    path: Path,
    key: str,
    count_table: str | None,
    backups: BackupSet,
    report: SiteDataReport,
    dry_run: bool,
) -> None:
    """Empty every user table, then VACUUM so deleted rows leave the file."""
    if not path.is_file():
        return

    if dry_run:
        report.add(key, _count_rows(path, count_table))
        report.add("databases", 1)
        return

    _backup_database(path, backups)

    # isolation_level None hands transaction control over, which VACUUM needs.
    connection = sqlite3.connect(str(path), timeout=5.0)
    connection.isolation_level = None
    try:
        tables = _user_tables(connection)
        if not tables:
            return

        total = 0
        if count_table and count_table in tables:
            total = int(connection.execute(f'SELECT count(*) FROM "{count_table}"').fetchone()[0])

        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        for table in tables:
            connection.execute(f'DELETE FROM "{table}"')
        connection.execute("COMMIT")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")

        report.add(key, total)
        report.add("databases", 1)
        logger.info("Cleared %s (%d table(s), %d row(s) in %s)",
                    path.name, len(tables), total, count_table or "n/a")
    except sqlite3.Error as error:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        message = f"Could not clear {path.name}: {error}"
        logger.warning("%s", message)
        report.warnings.append(message)
    finally:
        connection.close()

    # A stray journal left behind would still hold the deleted pages.
    for suffix in COMPANION_SUFFIXES:
        companion = path.with_name(path.name + suffix)
        if companion.is_file():
            try:
                companion.unlink()
            except OSError:
                pass


def _clear_logins(
    profile: Path,
    backups: BackupSet,
    report: SiteDataReport,
    dry_run: bool,
) -> None:
    """Blank logins.json, keeping the file so about:logins opens cleanly."""
    path = profile / "logins.json"
    payload = dict(BLANK_LOGINS)
    count = 0

    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            data = {}
        if isinstance(data, dict):
            logins = data.get("logins")
            count = len(logins) if isinstance(logins, list) else 0
            try:
                payload["version"] = int(data.get("version") or 3)
            except (TypeError, ValueError):
                payload["version"] = 3

    report.add("passwords", count)

    if dry_run or not path.is_file():
        return

    backups.save(path)
    try:
        path.write_text(
            json.dumps(payload, separators=(",", ":")), encoding="utf-8"
        )
        logger.info("Blanked logins.json (%d saved password(s) removed)", count)
    except OSError as error:
        message = f"Could not blank logins.json: {error}"
        logger.warning("%s", message)
        report.warnings.append(message)


def _remove_files(
    profile: Path,
    backups: BackupSet,
    report: SiteDataReport,
    dry_run: bool,
) -> None:
    for name in REMOVE_FILES:
        path = profile / name
        if not path.is_file():
            continue
        report.add("files_removed", 1)
        if dry_run:
            continue
        backups.save(path)
        try:
            path.unlink()
            logger.info("Removed %s", name)
        except OSError as error:
            message = f"Could not remove {name}: {error}"
            logger.warning("%s", message)
            report.warnings.append(message)


def _clear_storage(
    profile: Path,
    report: SiteDataReport,
    dry_run: bool,
) -> None:
    """Remove quota-manager origin directories belonging to web origins.

    Not backed up: these trees reach gigabytes, and BackupSet copies files
    rather than directories.
    """
    root = profile / "storage"
    if not root.is_dir():
        return

    removed = 0
    for bucket in STORAGE_ROOTS:
        folder = root / bucket
        if not folder.is_dir():
            continue
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            continue

        for origin in entries:
            if not origin.is_dir():
                continue
            if not origin.name.startswith(WEB_ORIGIN_PREFIXES):
                continue
            removed += 1
            if dry_run:
                continue
            try:
                shutil.rmtree(origin)
            except OSError as error:
                message = f"Could not remove site storage {origin.name}: {error}"
                logger.warning("%s", message)
                report.warnings.append(message)
                removed -= 1

    if removed:
        report.add("storage_origins", removed)
        logger.info(
            "%s %d web origin(s) of site storage",
            "Would remove" if dry_run else "Removed",
            removed,
        )
        if not dry_run:
            report.warnings.append(
                f"{removed} site storage directory(ies) were deleted without a backup; "
                "they are far too large to copy. Everything else cleared is in the "
                "backup folder."
            )


def clear_site_data(
    zen_profile: Path,
    backups: BackupSet,
    dry_run: bool = False,
) -> SiteDataReport:
    """Clear cookies, site storage, passwords and per-site records."""
    report = SiteDataReport()

    for name, key, count_table in DATABASE_JOBS:
        _wipe_database(zen_profile / name, key, count_table, backups, report, dry_run)

    _clear_logins(zen_profile, backups, report, dry_run)
    _remove_files(zen_profile, backups, report, dry_run)
    _clear_storage(zen_profile, report, dry_run)

    if dry_run:
        logger.info(
            "Dry run: would clear %d database(s), %d password(s), %d cookie(s), "
            "%d storage origin(s)",
            report.counts.get("databases", 0),
            report.counts.get("passwords", 0),
            report.counts.get("cookies", 0),
            report.counts.get("storage_origins", 0),
        )
    else:
        logger.info(
            "Site data cleared: %d cookie(s), %d password(s), %d form entry(ies), "
            "%d permission(s), %d storage origin(s)",
            report.counts.get("cookies", 0),
            report.counts.get("passwords", 0),
            report.counts.get("form_entries", 0),
            report.counts.get("permissions", 0),
            report.counts.get("storage_origins", 0),
        )

    return report