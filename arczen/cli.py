# arczen/cli.py
"""Command line interface."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .doctor import run_doctor
from .inspect import icon_candidates, inspect_spaces, list_icons
from .migrate import COPYABLE, Options, run

logger = logging.getLogger("arczen")

ZEN_PROCESS_NAMES = {
    "zen",
    "zen.exe",
    "zen-bin",
    "zen-browser",
    "zen-browser-bin",
    "zen-browser.exe",
    "zen browser",
    "zen browser.exe",
}

MIGRATION_COMMANDS = ("merge", "replace")
DIAGNOSTIC_COMMANDS = ("inspect", "doctor", "hashes")


def running_zen() -> list[str]:
    """Names of live Zen processes, or an empty list if psutil is unavailable."""
    try:
        import psutil
    except ImportError:
        return []

    found = []
    for process in psutil.process_iter(["pid", "name", "exe"]):
        try:
            name = (process.info.get("name") or "").lower()
            executable = Path(process.info.get("exe") or name).name.lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if name in ZEN_PROCESS_NAMES or executable in ZEN_PROCESS_NAMES:
            found.append(f"{process.info.get('name')} ({process.info.get('pid')})")
    return found


def _add_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--arc-path", metavar="DIR",
                        help="Arc data folder containing StorableSidebar.json, "
                             "if auto-discovery fails")
    parser.add_argument("--zen-path", metavar="DIR",
                        help="Zen profile directory, or a Zen root with profiles.ini")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")


def _add_migration_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--arc-spaces",
        nargs="+",
        action="extend",
        default=[],
        metavar='"NAME"',
        help="Copy only these Arc spaces, by name. Quote any name containing spaces: "
             '--arc-spaces "Work Stuff" Personal. Each space brings its own Arc '
             "profile's container with it",
    )
    parser.add_argument(
        "--arc-profiles",
        nargs="+",
        action="extend",
        default=[],
        metavar='"NAME"',
        help="Copy every space belonging to these Arc profiles. Accepts the folder "
             'name ("Default", "Profile 1") or the name Arc shows for the profile. '
             "Passwords and history are stored per profile, so this is the switch "
             "that selects them",
    )
    parser.add_argument(
        "--exclude",
        nargs="+",
        action="extend",
        default=[],
        choices=COPYABLE,
        metavar="WHAT",
        help="Skip one or more kinds of data: " + ", ".join(COPYABLE)
             + ". 'tabs' covers pinned tabs, their folders and Essentials; "
               "'temporary-tabs' covers Arc's unpinned tabs",
    )
    parser.add_argument("--no-containers", action="store_true",
                        help="Do not create containers from Arc profiles")
    parser.add_argument("--no-backup", action="store_true",
                        help="Do not copy Zen's files before changing them")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report planned changes and write nothing")
    parser.add_argument("--force", action="store_true",
                        help="Proceed even if Zen appears to be running")
    _add_paths(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arczen",
        description="Migrate Arc spaces, folders, pinned tabs, Essentials, icons, themes, "
                    "containers, history and passwords into Zen Browser.",
        epilog="Run 'arczen merge --help' or 'arczen replace --help' for the full option list.",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    merge = sub.add_parser(
        "merge",
        help="Add every Arc profile to the current Zen profile, keeping what Zen already has",
        description="Merge Arc into Zen. Existing Zen tabs, history and bookmarks are left "
                    "in place; one container is created per Arc profile and every migrated "
                    "space is bound to its own profile's container.",
    )
    _add_migration_options(merge)

    replace = sub.add_parser(
        "replace",
        help="Clear the Zen profile first, then copy Arc into it",
        description="Replace Zen's contents with Arc's. Zen's tabs, folders, groups, "
                    "closed-tab state, browsing history, bookmarks and cached favicons are "
                    "cleared, the stock Personal/Work/Banking/Shopping containers are "
                    "removed and the Arc containers are numbered from 1.",
    )
    _add_migration_options(replace)

    inspect = sub.add_parser("inspect", help="Diagnose icon and colour resolution; writes nothing")
    _add_paths(inspect)
    inspect.add_argument("--zen-install", help="Zen install directory, if icon discovery fails")
    inspect.add_argument("--list-icons", action="store_true",
                         help="Print every icon name Zen ships, by namespace")
    inspect.add_argument("--icon-candidates", metavar="ARC_ICON_NAME",
                         help="Show how one Arc icon identifier resolves, with near misses")

    doctor = sub.add_parser(
        "doctor",
        help="Check every precondition for the history importer; writes nothing",
    )
    _add_paths(doctor)

    hashes = sub.add_parser(
        "hashes",
        help="Audit moz_places.url_hash and repair rows written by other tools",
    )
    _add_paths(hashes)
    hashes.add_argument("--repair", action="store_true",
                        help="Rewrite every url_hash that Zen cannot match")

    return parser


def hash_audit(zen_path: str | None, repair: bool) -> int:
    """Classify every moz_places.url_hash, and optionally correct the bad ones."""
    import sqlite3

    from .history import CLASS_DETAIL, MATCH, audit_url_hashes, repair_url_hashes
    from .paths import BackupSet, resolve_zen_profile

    profile = resolve_zen_profile(zen_path)
    places = profile / "places.sqlite"
    print(f"\nZen profile   : {profile}")
    if not places.is_file():
        print("places.sqlite : missing - launch Zen once and quit it first")
        return 1
    print(f"places.sqlite : {places.stat().st_size:,} bytes")

    connection = sqlite3.connect(str(places), timeout=5.0)
    try:
        counts, examples = audit_url_hashes(connection)
        total = sum(counts.values())
        if not total:
            print("\nmoz_places is empty; nothing to audit.")
            return 0

        print(f"\n{total} row(s) in moz_places:")
        for kind, count in sorted(counts.items(), key=lambda item: -item[1]):
            label = "matches Firefox's hash()" if kind == MATCH else CLASS_DETAIL.get(kind, kind)
            print(f"  {count:>7}  {kind:<16} {label}")

        if examples:
            print("\nExamples of rows Zen cannot see:")
            for kind, url, stored, computed in examples:
                print(f"  [{kind}] {url[:66]}")
                print(f"      stored={stored}  should be={computed}")

        broken = total - counts.get(MATCH, 0)
        if not broken:
            print("\nEvery row uses Firefox's formula. Nothing to repair.")
            return 0

        if not repair:
            print(f"\n{broken} row(s) hold a hash Zen cannot match. These were written by "
                  "another tool and are invisible in the browser.")
            print("Run 'arczen hashes --repair' to rewrite them.")
            return 1

        backups = BackupSet.for_profile(profile, enabled=True)
        for name in ("places.sqlite", "places.sqlite-wal", "places.sqlite-shm"):
            backups.save(profile / name)
        fixed = repair_url_hashes(connection)
        print(f"\nRewrote url_hash on {fixed} row(s).")
        if backups.saved:
            print(f"Backup: {backups.root}")
        print("Those pages are now reachable from Zen's address bar and history.")
        return 0
    except sqlite3.OperationalError as error:
        print(f"\nCould not open places.sqlite: {error}")
        print("Quit Zen completely and try again.")
        return 1
    finally:
        connection.close()

SITE_DATA_LABELS = (
    ("cookies", "cookie(s)"),
    ("passwords", "password(s)"),
    ("local_storage", "localStorage entry(ies)"),
    ("storage_origins", "site storage origin(s)"),
    ("form_entries", "form history entry(ies)"),
    ("permissions", "site permission(s)"),
    ("site_settings", "per-site setting(s)"),
    ("tracker_events", "tracker log entry(ies)"),
    ("tab_notes", "tab note(s)"),
    ("files_removed", "data file(s) removed"),
)


def _print_summary(result, options: Options) -> None:
    print()
    print("Summary")
    print("-------")
    print(f"  Mode        : {options.mode}")
    print(f"  Spaces      : {', '.join(result.arc_spaces) or 'none'}")
    print(f"  Arc profiles: {', '.join(result.arc_profiles) or 'none'}")
    print(f"  Workspaces  : {result.workspaces}")
    print(f"  Tabs        : {result.tabs}")
    print(f"  Folders     : {result.folders}")
    if options.containers:
        print(f"  Containers  : {result.container_total} total "
              f"({result.containers_created} new, {result.containers_purged} removed)")
    else:
        print("  Containers  : disabled")
    print(f"  Icons       : {result.icons_resolved}/{result.workspaces} resolved")

    if result.history:
        if result.history.get("aborted"):
            print(f"  History     : not imported - {result.history.get('detail')}")
        else:
            if result.history.get("cleared_places"):
                print(
                    f"  Cleared     : {result.history['cleared_places']} old URL(s), "
                    f"{result.history.get('cleared_visits', 0)} visit(s), "
                    f"{result.history.get('cleared_bookmarks', 0)} bookmark(s), "
                    f"{result.favicons_cleared} favicon(s)"
                )
            print(
                f"  History     : {result.history.get('inserted', 0)} new URLs, "
                f"{result.history.get('updated', 0)} already present, "
                f"{result.history.get('visits', 0)} visits, "
                f"{result.history.get('skipped', 0)} skipped"
            )

    if result.site_data:
        wiped = [
            f"{result.site_data[key]} {label}"
            for key, label in SITE_DATA_LABELS
            if result.site_data.get(key)
        ]
        verb = "Would clear " if options.dry_run else "Site data   "
        if wiped:
            print(f"  {verb}: {wiped[0]}")
            for entry in wiped[1:]:
                print(f"                {entry}")

    if result.passwords:
        print(f"  Passwords   : {result.passwords} credentials -> {result.password_csv}")
    if result.backup_dir:
        print(f"  Backups     : {result.backup_dir}")

    for entry in result.icons_unresolved:
        print(f"\n  ! {entry}")
    if result.icons_unresolved:
        print("\n    Run 'arczen inspect' to see how each Arc icon was resolved.")

    for warning in result.warnings:
        print(f"\n  ! {warning}")

    if options.dry_run:
        print("\nDry run only. Nothing was written.")
    else:
        print("\nNext steps:")
        print("  1. Launch Zen. Your spaces, folders, pinned tabs and Essentials "
              "restore on startup.")
        if result.password_csv:
            print(f"  2. Open about:logins -> Import from a File -> {result.password_csv}")
            print(f"  3. Delete {result.password_csv}; it holds your passwords in plain text.")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw = list(sys.argv[1:] if argv is None else argv)

    # Bare invocation shows help rather than guessing at a destructive default.
    if not raw:
        parser.print_help()
        return 0

    args = parser.parse_args(raw)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
        force=True,
    )

    if args.command == "doctor":
        try:
            return run_doctor(args.arc_path, args.zen_path)
        except Exception as error:  # noqa: BLE001 - one clean message for the user
            logger.error("Diagnostics failed: %s", error)
            if args.verbose:
                raise
            return 1

    if args.command == "hashes":
        try:
            return hash_audit(args.zen_path, args.repair)
        except Exception as error:  # noqa: BLE001
            logger.error("Hash audit failed: %s", error)
            if args.verbose:
                raise
            return 1

    if args.command == "inspect":
        if args.list_icons:
            return list_icons(args.zen_install)
        if args.icon_candidates:
            return icon_candidates(args.icon_candidates, args.zen_install)
        try:
            return inspect_spaces(args.arc_path, args.zen_install, {})
        except Exception as error:  # noqa: BLE001
            logger.error("Inspection failed: %s", error)
            if args.verbose:
                raise
            return 1

    if args.command not in MIGRATION_COMMANDS:
        parser.print_help()
        return 0

    live = running_zen()
    if live and not args.dry_run and not args.force:
        logger.error("Zen is running: %s", ", ".join(live))
        logger.error("Quit Zen completely, then run again. It rewrites its session files on exit.")
        return 2

    exclude = frozenset(args.exclude)
    if {"tabs", "temporary-tabs"} <= exclude:
        logger.warning("Both tab kinds excluded: workspaces will be created empty.")

    options = Options(
        mode=args.command,
        arc_path=args.arc_path,
        zen_path=args.zen_path,
        arc_spaces=list(args.arc_spaces),
        arc_profiles=list(args.arc_profiles),
        exclude=exclude,
        containers=not args.no_containers,
        backups=not args.no_backup,
        dry_run=args.dry_run,
    )

    if options.mode == "replace" and not options.dry_run:
        logger.warning(
            "Replace mode: Zen's existing tabs, folders, groups, history, bookmarks and "
            "cached favicons are cleared before the copy."
        )

    try:
        result = run(options)
    except Exception as error:  # noqa: BLE001 - surface one clean message
        logger.error("%s failed: %s", options.mode.capitalize(), error)
        if args.verbose:
            raise
        return 1

    _print_summary(result, options)
    return 0


if __name__ == "__main__":
    sys.exit(main())