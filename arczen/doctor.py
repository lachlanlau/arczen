# arczen/doctor.py
"""Precondition checks for the history importer.

Nothing here writes to a profile. Every check reports pass, warn or fail with
the reason, so a failed import can be traced to a cause instead of a guess.
"""

from __future__ import annotations

import importlib
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

RULE = "-" * 72

PASS = "pass"
WARN = "warn"
FAIL = "fail"

MARKS = {PASS: "  ok  ", WARN: " warn ", FAIL: " FAIL "}


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""


class Report:
    def __init__(self, title: str):
        self.title = title
        self.checks: list[Check] = []

    def add(self, name: str, status: str, detail: str = "") -> Check:
        check = Check(name, status, detail)
        self.checks.append(check)
        return check

    def ok(self, name: str, detail: str = "") -> Check:
        return self.add(name, PASS, detail)

    def warn(self, name: str, detail: str = "") -> Check:
        return self.add(name, WARN, detail)

    def fail(self, name: str, detail: str = "") -> Check:
        return self.add(name, FAIL, detail)

    @property
    def failed(self) -> bool:
        return any(check.status == FAIL for check in self.checks)

    def render(self) -> None:
        print(f"\n{RULE}")
        print(self.title)
        print(RULE)
        width = max((len(check.name) for check in self.checks), default=0)
        for check in self.checks:
            print(f"[{MARKS[check.status]}] {check.name.ljust(width)}  {check.detail}")


# --------------------------------------------------------------------------
# Module integrity
# --------------------------------------------------------------------------

def check_modules() -> Report:
    """Catch half-applied edits before blaming the browsers."""
    report = Report("Module integrity")

    try:
        paths = importlib.import_module("arczen.paths")
    except Exception as error:  # noqa: BLE001
        report.fail("arczen.paths imports", f"{type(error).__name__}: {error}")
        return report
    report.ok("arczen.paths imports")

    for symbol in ("arc_history_databases", "arc_favicon_databases", "arc_login_databases"):
        if hasattr(paths, symbol):
            report.ok(f"paths.{symbol}")
        else:
            report.fail(
                f"paths.{symbol}",
                "missing - the file is likely truncated; replace it in full",
            )

    for module_name, symbols in (
        ("arczen.chromium_crypto", ("load_key", "ChromiumKey")),
        (
            "arczen.history",
            ("migrate_history", "url_hash", "verify_url_hash", "hash_string"),
        ),
        ("arczen.passwords", ("export_passwords",)),
    ):
        try:
            module = importlib.import_module(module_name)
        except Exception as error:  # noqa: BLE001
            report.fail(f"{module_name} imports", f"{type(error).__name__}: {error}")
            continue
        report.ok(f"{module_name} imports")
        for symbol in symbols:
            if hasattr(module, symbol):
                report.ok(f"{module_name.split('.')[-1]}.{symbol}")
            else:
                report.fail(
                    f"{module_name.split('.')[-1]}.{symbol}",
                    "missing - the file is likely truncated; replace it in full",
                )

    try:
        importlib.import_module("lz4.block")
        report.ok("lz4 available")
    except ImportError:
        report.fail("lz4 available", "pip install lz4")

    try:
        importlib.import_module("cryptography")
        report.ok("cryptography available")
    except ImportError:
        report.fail(
            "cryptography available",
            "pip install cryptography - the password export needs it",
        )

    return report


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------

def check_history(arc_root: Path, zen_profile: Path) -> Report:
    report = Report("History")

    try:
        from .paths import arc_history_databases
    except ImportError as error:
        report.fail("history discovery", f"{error}")
        return report

    databases = arc_history_databases(arc_root)
    if not databases:
        report.fail("Arc History databases", f"none found under {arc_root / 'User Data'}")
    for database in databases:
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2.0)
            try:
                urls = connection.execute("SELECT count(*) FROM urls").fetchone()[0]
                visits = connection.execute("SELECT count(*) FROM visits").fetchone()[0]
            finally:
                connection.close()
            report.ok(
                f"{database.parent.name} History (Arc source)",
                f"{urls} URLs, {visits} visits available to read",
            )
        except sqlite3.Error as error:
            report.fail(f"{database.parent.name} History (Arc source)", f"{error}")

    places = zen_profile / "places.sqlite"
    if not places.is_file():
        report.fail(
            "Zen places.sqlite",
            "missing - launch Zen once and quit it so Firefox creates it",
        )
        return report
    report.ok("Zen places.sqlite", f"{places.stat().st_size:,} bytes")

    try:
        writer = sqlite3.connect(str(places), timeout=1.0)
    except sqlite3.Error as error:
        report.fail("open places.sqlite", f"{error}")
        return report

    try:
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.rollback()
            report.ok("places.sqlite writable")
        except sqlite3.OperationalError as error:
            report.fail("places.sqlite writable", f"{error} - is Zen still running?")

        from .history import _register_functions

        _register_functions(writer)
        report.ok("SQL function shims", "registered")

        info = {
            row[1]: {"type": row[2], "notnull": row[3]}
            for row in writer.execute("PRAGMA table_info(moz_places)")
        }
        if not info:
            report.fail("moz_places table", "absent")
            return report
        report.ok("moz_places table", f"{len(info)} columns")

        required = {"url", "title", "rev_host", "guid", "url_hash"}
        missing = sorted(required - set(info))
        if missing:
            report.fail("moz_places columns", f"missing: {missing}")
        else:
            report.ok("moz_places columns")

        optional = [
            name
            for name in ("recalc_frecency", "recalc_alt_frecency", "foreign_count")
            if name not in info
        ]
        if optional:
            report.warn("optional columns", f"absent, will be omitted: {optional}")

        triggers = [
            row[0]
            for row in writer.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name IN ('moz_places', 'moz_historyvisits')"
            )
        ]
        visit_trigger = any(name.startswith("moz_historyvisits_afterinsert") for name in triggers)
        if visit_trigger:
            report.ok("visit accounting triggers", f"{len(triggers)} trigger(s) present")
        else:
            report.warn(
                "visit accounting triggers",
                "absent - visit_count and last_visit_date are recomputed manually",
            )

        # origin_id is NOT NULL on some schema versions and is normally filled
        # by the afterinsert trigger. Without that trigger the importer writes
        # moz_origins itself, which needs the table to be present.
        origin = info.get("origin_id")
        has_place_trigger = any(
            name.startswith("moz_places_afterinsert") for name in triggers
        )
        if origin and origin["notnull"] and not has_place_trigger:
            has_origins = bool(
                writer.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='moz_origins'"
                ).fetchone()
            )
            if has_origins:
                report.warn(
                    "origin_id handling",
                    "NOT NULL with no afterinsert trigger - moz_origins is populated directly",
                )
            else:
                report.fail(
                    "origin_id handling",
                    "NOT NULL, no afterinsert trigger and no moz_origins table - "
                    "inserts would be rejected",
                )
        elif origin:
            report.ok("origin_id handling", "trigger present or column nullable")

        from .history import url_hash, verify_url_hash

        check = verify_url_hash(writer)
        if check.verified and not check.foreign:
            report.ok("url_hash formula", check.detail)
        elif check.verified:
            report.warn(
                "url_hash formula",
                f"{check.detail}; --nuke clears those rows before importing",
            )
        else:
            report.warn(
                "url_hash formula",
                f"{check.detail}; the import uses Firefox's own formula regardless. "
                "Run 'arczen hashes' for a breakdown",
            )

        sample = writer.execute("SELECT url, url_hash FROM moz_places LIMIT 1").fetchone()
        if sample:
            report.ok(
                "url_hash sample",
                f"stored={sample[1]} computed={url_hash(sample[0])} for {sample[0][:40]}",
            )

        places_count = writer.execute("SELECT count(*) FROM moz_places").fetchone()[0]
        visit_count = writer.execute("SELECT count(*) FROM moz_historyvisits").fetchone()[0]
        report.ok("existing history", f"{places_count} places, {visit_count} visits")
    except Exception as error:  # noqa: BLE001
        report.fail("inspect places.sqlite", f"{type(error).__name__}: {error}")
    finally:
        writer.close()

    return report


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def run_doctor(
    arc_profile: str | Path | None = None,
    zen_profile: str | Path | None = None,
) -> int:
    from .paths import resolve_arc_root, resolve_zen_profile

    reports = [check_modules()]

    try:
        arc_root = resolve_arc_root(arc_profile)
        print(f"\nArc data folder : {arc_root}")
    except FileNotFoundError as error:
        print(f"\nArc data folder : NOT FOUND - {error}")
        for report in reports:
            report.render()
        return 1

    try:
        zen = resolve_zen_profile(zen_profile)
        print(f"Zen profile     : {zen}")
    except FileNotFoundError as error:
        print(f"Zen profile     : NOT FOUND - {error}")
        for report in reports:
            report.render()
        return 1

    reports.append(check_history(arc_root, zen))

    for report in reports:
        report.render()

    failures = [check for report in reports for check in report.checks if check.status == FAIL]
    print(f"\n{RULE}")
    if failures:
        print(f"{len(failures)} blocking issue(s):\n")
        for check in failures:
            print(f"  - {check.name}: {check.detail}")
        return 1

    print("No blocking issues found.")
    return 0