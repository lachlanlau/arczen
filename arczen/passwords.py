# arczen/passwords.py
"""Decrypt Arc's Chromium password store into a Firefox-importable CSV.

Writing straight into Zen's logins.json would mean encrypting against NSS's
key4.db PKCS#11 store, which has no dependable path from Python. A CSV plus one
click in about:logins is the honest trade.
"""

from __future__ import annotations

import csv
import logging
import shutil
import sqlite3
import tempfile
from pathlib import Path

from .chromium_crypto import ChromiumKey, CryptoError, load_key
from .paths import arc_login_databases

logger = logging.getLogger(__name__)

CHROME_EPOCH_OFFSET = 11_644_473_600_000_000

CSV_FIELDS = [
    "url",
    "username",
    "password",
    "httpRealm",
    "formActionOrigin",
    "guid",
    "timeCreated",
    "timeLastUsed",
    "timePasswordChanged",
]


class PasswordError(RuntimeError):
    """Raised when the password store cannot be exported."""


def _chrome_to_ms(timestamp: int | None) -> int:
    if not timestamp:
        return 0
    value = (int(timestamp) - CHROME_EPOCH_OFFSET) // 1000
    return value if value > 0 else 0


def export_passwords(
    arc_root: Path,
    output: Path,
    keychain_service: str | None = None,
    dry_run: bool = False,
    key: ChromiumKey | None = None,
    profiles: set[str] | None = None,
) -> int:
    """Write every Arc credential to a CSV that about:logins can import."""
    databases = arc_login_databases(arc_root, profiles)
    if not databases:
        logger.info("No Arc 'Login Data' databases found; skipping passwords")
        return 0

    try:
        material = key or load_key(arc_root, keychain_service)
    except CryptoError as error:
        raise PasswordError(str(error)) from error

    rows: list[dict[str, object]] = []
    for database in databases:
        # Arc may hold a lock on the live file; work on a copy.
        with tempfile.TemporaryDirectory(prefix="arc-logins-") as temp_dir:
            copy_path = Path(temp_dir) / "Login Data"
            shutil.copy2(database, copy_path)

            connection = sqlite3.connect(str(copy_path))
            connection.row_factory = sqlite3.Row
            try:
                records = connection.execute(
                    "SELECT origin_url, action_url, username_value, password_value, "
                    "signon_realm, date_created, date_last_used, date_password_modified "
                    "FROM logins"
                ).fetchall()
            except sqlite3.Error as error:
                logger.warning("Could not read %s: %s", database, error)
                continue
            finally:
                connection.close()

        for record in records:
            try:
                password = material.decrypt(record["password_value"] or b"")
            except CryptoError as error:
                raise PasswordError(str(error)) from error
            except Exception as error:  # noqa: BLE001 - one bad row must not abort
                logger.warning("Skipping an entry for %s: %s", record["origin_url"], error)
                continue

            if not password:
                continue

            rows.append(
                {
                    "url": record["origin_url"] or record["signon_realm"] or "",
                    "username": record["username_value"] or "",
                    "password": password,
                    "httpRealm": "",
                    "formActionOrigin": record["action_url"] or "",
                    "guid": "",
                    "timeCreated": _chrome_to_ms(record["date_created"]),
                    "timeLastUsed": _chrome_to_ms(record["date_last_used"]),
                    "timePasswordChanged": _chrome_to_ms(record["date_password_modified"]),
                }
            )

    if dry_run:
        logger.info("Dry run: would export %d credential(s)", len(rows))
        return len(rows)

    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    try:
        output.chmod(0o600)
    except OSError:
        pass

    logger.info("Exported %d credential(s) to %s", len(rows), output)
    return len(rows)