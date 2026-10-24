# arczen/migrate.py
"""Migration orchestration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from . import favicons, history, passwords, privacy
from .arcdata import ArcSidebar, ArcSpace, parse_sidebar
from .build import BuildOptions, build_session, write_session
from .chromium_crypto import ChromiumKey, CryptoError, load_key
from .containers import ContainerRequest, ensure_containers
from .icons import discover_zen_icons, resolve_space_icon
from .paths import (
    BackupSet,
    arc_chromium_profiles,
    arc_profile_names,
    resolve_arc_root,
    resolve_zen_profile,
)
from .prefs import apply_prefs
from .splitview import apply_split_views

logger = logging.getLogger(__name__)

# Everything --exclude accepts.
#   tabs            pinned tabs, their folders and Essentials
#   temporary-tabs  Arc's unpinned tabs
#   favicons        cached Arc favicons attached to migrated tabs
#   history         browsing history into places.sqlite
#   passwords       the Firefox-importable password CSV
COPYABLE = ("tabs", "temporary-tabs", "favicons", "history", "passwords")


@dataclass
class Options:
    mode: str = "merge"                     # merge | replace
    arc_path: str | Path | None = None      # Arc data folder override
    zen_path: str | Path | None = None      # Zen profile override
    arc_spaces: list[str] = field(default_factory=list)     # space names
    arc_profiles: list[str] = field(default_factory=list)   # profile folder or display names
    exclude: frozenset[str] = frozenset()
    containers: bool = True
    backups: bool = True
    dry_run: bool = False
    # Plumbing, not exposed on the command line.
    password_csv: Path = Path("arc-passwords.csv")
    keychain_service: str | None = None
    hide_container_badges: bool = True

    @property
    def replace(self) -> bool:
        """Replace mode clears Zen's own records before copying."""
        return self.mode == "replace"

    @property
    def scoped(self) -> bool:
        """True when the user named specific spaces or profiles."""
        return bool(self.arc_spaces or self.arc_profiles)

    def copies(self, what: str) -> bool:
        return what not in self.exclude


@dataclass
class Result:
    arc_root: Path
    zen_profile: Path
    mode: str = "merge"
    arc_spaces: list[str] = field(default_factory=list)
    arc_profiles: list[str] = field(default_factory=list)
    workspaces: int = 0
    tabs: int = 0
    folders: int = 0
    splits: list[str] = field(default_factory=list)
    splits_skipped: list[str] = field(default_factory=list)
    containers: dict[str, int] = field(default_factory=dict)
    containers_created: int = 0
    containers_purged: int = 0
    container_total: int = 0
    icons_resolved: int = 0
    icons_unresolved: list[str] = field(default_factory=list)
    history: dict[str, object] = field(default_factory=dict)
    bookmarks_cleared: int = 0
    favicons_cleared: int = 0
    site_data: dict[str, int] = field(default_factory=dict)
    passwords: int = 0
    password_csv: Path | None = None
    backup_dir: Path | None = None
    warnings: list[str] = field(default_factory=list)


class _KeyCache:
    """Derives Arc's master key once, so the user sees a single Keychain prompt."""

    def __init__(self, arc_root: Path, keychain_service: str | None):
        self.arc_root = arc_root
        self.keychain_service = keychain_service
        self._key: ChromiumKey | None = None
        self._failure: CryptoError | None = None

    def get(self) -> ChromiumKey:
        if self._failure:
            raise self._failure
        if self._key is None:
            try:
                self._key = load_key(self.arc_root, self.keychain_service)
            except CryptoError as error:
                self._failure = error
                raise
        return self._key


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------

@dataclass
class _Selection:
    """Which spaces were kept, and which Arc profiles feed profile-scoped data."""

    spaces: list[ArcSpace]
    profiles: frozenset[str]
    missing_spaces: list[str] = field(default_factory=list)
    missing_profiles: list[str] = field(default_factory=list)
    # Profiles where some but not all of their spaces were selected. Arc's
    # history and passwords have no space dimension, so these profiles arrive
    # whole and the user needs telling.
    partial_profiles: list[str] = field(default_factory=list)


def _profile_lookup(arc_root: Path, sidebar: ArcSidebar) -> dict[str, set[str]]:
    """Lowered name -> Arc profile folder name(s) it refers to.

    Accepts the folder name on disk, the display name from Local State, and any
    profile string the sidebar itself reports.
    """
    lookup: dict[str, set[str]] = {}

    def add(token: str, directory: str) -> None:
        key = token.strip().lower()
        if key:
            lookup.setdefault(key, set()).add(directory)

    for path in arc_chromium_profiles(arc_root):
        add(path.name, path.name)
    for directory, label in arc_profile_names(arc_root).items():
        add(directory, directory)
        add(label, directory)
    for space in sidebar.spaces:
        add(space.profile, space.profile)

    return lookup


def _select(
    arc_root: Path,
    sidebar: ArcSidebar,
    space_names: list[str],
    profile_names: list[str],
) -> _Selection:
    """Narrow the sidebar to the named spaces and profiles.

    A named space brings its owning profile along, since that is where its
    history, passwords and favicons live.
    """
    all_profiles = frozenset(space.profile for space in sidebar.spaces)

    if not space_names and not profile_names:
        return _Selection(spaces=list(sidebar.spaces), profiles=all_profiles)

    wanted_spaces = {name.strip().lower() for name in space_names if name.strip()}
    wanted_profiles = {name.strip().lower() for name in profile_names if name.strip()}

    lookup = _profile_lookup(arc_root, sidebar)
    requested_profiles: set[str] = set()
    missing_profiles: list[str] = []
    for token in sorted(wanted_profiles):
        directories = lookup.get(token)
        if directories:
            requested_profiles |= directories
        else:
            missing_profiles.append(token)

    kept: list[ArcSpace] = []
    matched_spaces: set[str] = set()
    for space in sidebar.spaces:
        key = space.name.strip().lower()
        by_name = key in wanted_spaces
        by_profile = space.profile in requested_profiles
        if by_name:
            matched_spaces.add(key)
        if by_name or by_profile:
            kept.append(space)

    # Profiles feeding history and passwords: every explicitly named profile,
    # plus the owners of every named space.
    profiles = requested_profiles | {space.profile for space in kept}

    partial = sorted(
        profile
        for profile in profiles
        if any(space.profile == profile for space in sidebar.spaces)
        and not all(
            space in kept for space in sidebar.spaces if space.profile == profile
        )
    )

    return _Selection(
        spaces=kept,
        profiles=frozenset(profiles),
        missing_spaces=sorted(wanted_spaces - matched_spaces),
        missing_profiles=missing_profiles,
        partial_profiles=partial,
    )


def _apply_selection(sidebar: ArcSidebar, selection: _Selection) -> None:
    """Trim the sidebar in place, dropping Essentials no kept space can show."""
    sidebar.spaces = selection.spaces
    owners = {space.profile for space in selection.spaces}
    sidebar.essentials = {
        profile: tabs for profile, tabs in sidebar.essentials.items() if profile in owners
    }


# --------------------------------------------------------------------------
# Containers and icons
# --------------------------------------------------------------------------


def _container_plan(
    sidebar: ArcSidebar,
    profile_names: dict[str, str],
) -> tuple[list[ContainerRequest], dict[str, str]]:
    """One container per Arc profile, plus the space_id -> container key map.

    Arc shares logins and Essentials across every space in a profile, so the
    container follows the profile. The label is the name Arc shows for that
    profile, taken from profile.name in its Preferences file, falling back to
    the folder name on disk when that is missing.

    Aliases cover the folder name and every label format arczen has used
    before, so an existing container is adopted rather than duplicated - which
    also covers the case where Arc had not yet flushed a rename to disk.
    """
    key_for_space: dict[str, str] = {}
    first_space: dict[str, ArcSpace] = {}
    space_names: dict[str, list[str]] = {}

    for space in sidebar.spaces:
        key = f"profile:{space.profile}"
        key_for_space[space.space_id] = key
        space_names.setdefault(space.profile, []).append(space.name)
        first_space.setdefault(key, space)

    requests: list[ContainerRequest] = []
    for key, space in first_space.items():
        arc_name = profile_names.get(space.profile)
        label = arc_name or space.profile

        if arc_name:
            logger.info("Arc profile %s is named %r in Arc", space.profile, arc_name)
        else:
            logger.info(
                "Arc profile %s has no name in its Preferences file; using the folder "
                "name for the container",
                space.profile,
            )

        # Every name this same container could already be sitting under.
        candidates = {
            space.profile,
            f"Arc {space.profile}",
            *space_names.get(space.profile, ()),
        }
        if arc_name:
            candidates.add(f"Arc {arc_name}")

        requests.append(
            ContainerRequest(
                key=key,
                label=label,
                window_theme=space.window_theme,
                icon_emoji=space.icon_emoji,
                icon_name=space.icon_name,
                aliases=tuple(
                    sorted(name for name in candidates if name and name != label)
                ),
            )
        )

    return requests, key_for_space


def _resolve_icons(sidebar: ArcSidebar, result: Result) -> None:
    """Fill ArcSpace.icon with a value Zen can actually render."""
    icon_set = discover_zen_icons(None)

    for space in sidebar.spaces:
        resolution = resolve_space_icon(space.icon_emoji, space.icon_name, icon_set)
        space.icon = resolution.value

        if resolution.value:
            result.icons_resolved += 1
        logger.info(
            "Icon for %r: %s (%s via %s)",
            space.name,
            resolution.value or "none",
            resolution.kind,
            resolution.method,
        )

        if space.icon_name and resolution.kind not in ("selectable", "general", "override"):
            detail = f"{space.name}: Arc icon {space.icon_name!r} has no Zen equivalent"
            if resolution.candidates:
                detail += f" (closest: {', '.join(resolution.candidates)})"
            result.icons_unresolved.append(detail)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def run(options: Options) -> Result:
    arc_root = resolve_arc_root(options.arc_path)
    zen_profile = resolve_zen_profile(options.zen_path)
    logger.info("Mode:            %s", options.mode)
    logger.info("Arc data folder: %s", arc_root)
    logger.info("Zen profile:     %s", zen_profile)

    result = Result(arc_root=arc_root, zen_profile=zen_profile, mode=options.mode)
    backups = BackupSet.for_profile(zen_profile, enabled=options.backups and not options.dry_run)
    keys = _KeyCache(arc_root, options.keychain_service)

    # 1. Read Arc, then narrow to the requested spaces and profiles.
    logger.info("[1/7] Reading Arc sidebar")
    sidebar = parse_sidebar(arc_root)
    if not sidebar.spaces:
        raise RuntimeError("Arc's sidebar contains no spaces to migrate")

    available_spaces = sorted({space.name for space in sidebar.spaces})
    available_profiles = sorted(
        {space.profile for space in sidebar.spaces}
        | {path.name for path in arc_chromium_profiles(arc_root)}
    )

    selection = _select(arc_root, sidebar, options.arc_spaces, options.arc_profiles)

    if selection.missing_spaces:
        raise RuntimeError(
            "No Arc space called "
            + ", ".join(repr(name) for name in selection.missing_spaces)
            + ". Available spaces: "
            + ", ".join(repr(name) for name in available_spaces)
        )
    if selection.missing_profiles:
        labels = arc_profile_names(arc_root)
        described = ", ".join(
            repr(f"{name} ({labels[name]})") if name in labels else repr(name)
            for name in available_profiles
        )
        raise RuntimeError(
            "No Arc profile called "
            + ", ".join(repr(name) for name in selection.missing_profiles)
            + f". Available profiles: {described}"
        )

    _apply_selection(sidebar, selection)
    if not sidebar.spaces:
        raise RuntimeError("The space and profile selection left no spaces to migrate")

    result.arc_spaces = [space.name for space in sidebar.spaces]
    result.arc_profiles = sorted(selection.profiles)
    # None means "every Arc profile"; a set narrows the per-profile stores.
    profile_filter = selection.profiles if options.scoped else None

    logger.info(
        "Migrating %d space(s): %s",
        len(sidebar.spaces),
        ", ".join(repr(name) for name in result.arc_spaces),
    )
    logger.info(
        "Profile-scoped data (history, passwords, favicon cache) from: %s",
        ", ".join(result.arc_profiles),
    )

    # Arc's Chromium stores have no space column, so a part-selected profile
    # still contributes its whole history and all of its passwords.
    if selection.partial_profiles:
        detail = (
            "Arc stores history and passwords per profile, not per space, so the "
            "whole of "
            + ", ".join(repr(name) for name in selection.partial_profiles)
            + " was copied even though only some of its spaces were selected. "
              "Exclude them with --exclude history passwords if that is not wanted."
        )
        logger.warning("%s", detail)
        if options.copies("history") or options.copies("passwords"):
            result.warnings.append(detail)

    for profile, tabs in sidebar.essentials.items():
        logger.info("Essentials for Arc profile %r: %d tab(s)", profile, len(tabs))
    if not sidebar.essentials and options.copies("tabs"):
        result.warnings.append("Arc reported no Essentials (topApps) containers")

    # 2. Icons.
    logger.info("[2/7] Resolving space icons")
    _resolve_icons(sidebar, result)

    # 3. Containers, one per Arc profile that owns a selected space.
    logger.info("[3/7] Preparing containers")
    container_ids: dict[str, int] = {}
    key_for_space: dict[str, str] = {}
    if options.containers:
        requests, key_for_space = _container_plan(sidebar, arc_profile_names(arc_root))
        plan = ensure_containers(
            zen_profile,
            requests,
            backups,
            replace=options.replace,
            dry_run=options.dry_run,
        )
        container_ids = plan.mapping
        result.containers_created = len(plan.created)
        result.containers_purged = len(plan.purged)
        result.container_total = len(plan.created) + len(plan.reused)
        logger.info(
            "Every space of an Arc profile shares that profile's container, so logins "
            "and Essentials carry across spaces exactly as they did in Arc."
        )
        if options.replace and plan.purged:
            result.warnings.append(
                f"Replace mode discarded {len(plan.purged)} existing container(s). "
                "Cookies and site storage are keyed by container number, not by name, "
                "so the renumbered containers inherit the old data - clear site data "
                "from Zen's settings if that matters."
            )
        if plan.stale:
            result.warnings.append(
                "These containers duplicate ones this run wrote, left from an earlier "
                "arczen version: "
                + ", ".join(plan.stale)
                + ". Remove them in Zen's container settings, or run 'arczen replace' "
                  "to rebuild the file from scratch."
            )
    else:
        logger.info("Containers disabled; every space uses Zen's default container")

    # 4. Favicons. Scoped twice over: only the selected profiles' caches are
    #    opened, and only the selected spaces' URLs are looked up.
    icons: dict[str, str] = {}
    wants_tabs = options.copies("tabs") or options.copies("temporary-tabs")
    if options.copies("favicons") and wants_tabs:
        logger.info("[4/7] Collecting favicons")
        urls = {tab.url for space in sidebar.spaces for tab in space.tabs}
        urls.update(tab.url for tabs in sidebar.essentials.values() for tab in tabs)
        icons = favicons.collect_icons(arc_root, urls, profile_filter)
    else:
        logger.info("[4/7] Favicons skipped")

    # 5. Build and write session state.
    logger.info("[5/7] Building Zen workspaces, folders and tabs")
    build_options = BuildOptions(
        include_pinned_tabs=options.copies("tabs"),
        include_temporary_tabs=options.copies("temporary-tabs"),
    )
    session = build_session(sidebar, container_ids, key_for_space, icons, build_options)
    # Arc's split views, rebuilt as Zen split groups over the tabs already placed.
    split_report = apply_split_views(session, sidebar, options)
    write_session(zen_profile, session, backups, nuke=options.replace, dry_run=options.dry_run)

    result.workspaces = len(session["spaces"])
    result.tabs = sum(1 for tab in session["tabs"] if not tab.get("zenIsEmpty"))
    # Split groups ride in the folders array too; they are not folders.
    result.folders = sum(
        1 for folder in session["folders"] if not folder.get("splitViewGroup")
    )
    result.splits = split_report.written
    result.splits_skipped = split_report.skipped

    if result.splits_skipped:
        result.warnings.append(
            f"{len(result.splits_skipped)} Arc split view(s) could not be rebuilt; "
            "their tabs are present individually, so re-split them in Zen:\n      "
            + "\n      ".join(result.splits_skipped)
        )

    # 6. Preferences.
    logger.info("[6/7] Writing preferences")
    prefs: dict[str, object] = {
        "zen.workspaces.continue-where-left-off": True,
        "browser.startup.page": 3,
        # Hide the coloured container stripe on tabs sitting in their own
        # workspace's container. Written in both modes and whether or not
        # containers were created: Zen's default is to show the stripe, so
        # leaving the pref out asks for exactly what we do not want.
        "zen.workspaces.hide-default-container-indicator": True,
    }
    if session["spaces"]:
        prefs["zen.workspaces.active"] = session["spaces"][0]["uuid"]
    if container_ids:
        prefs["privacy.userContext.enabled"] = True
        prefs["privacy.userContext.ui.enabled"] = True
        prefs["zen.workspaces.force-container-workspace"] = True
        # One container per Arc profile, and Essentials are written once per
        # container, so Zen shows each profile's Essentials in every space of
        # that profile without a second copy.
        prefs["zen.workspaces.container-specific-essentials-enabled"] = True
    else:
        # No containers means no container to scope Essentials to; leaving this
        # on points Zen at identities that do not exist.
        prefs["zen.workspaces.container-specific-essentials-enabled"] = False

    unconfirmed = apply_prefs(zen_profile, prefs, backups, options.dry_run)
    if unconfirmed:
        result.warnings.append(
            "These preferences were written but were not in prefs.js when it was "
            "read back: "
            + ", ".join(unconfirmed)
            + ". Zen rewrites prefs.js from memory when it exits, so quit Zen "
              "completely and run again."
        )

    # 7. History and passwords, both read per Arc profile.
    logger.info("[7/7] History and passwords")

    if options.replace:
        # Icons live in their own database, so clearing them is independent of
        # whether history is being imported.
        result.favicons_cleared = history.clear_favicons(zen_profile, backups, options.dry_run)

        # Cookies, site storage, saved passwords, form history, permissions and
        # per-site settings. places.sqlite and favicons.sqlite stay with
        # history.py, which owns them.
        report = privacy.clear_site_data(zen_profile, backups, options.dry_run)
        result.site_data = report.counts
        result.warnings.extend(report.warnings)

        if not options.copies("passwords"):
            result.warnings.append(
                "Zen's saved passwords were cleared, but 'passwords' is excluded so no "
                "Arc credentials were exported to replace them. Re-run without "
                "--exclude passwords, or restore logins.json from the backup folder."
            )

    if options.copies("history"):
        result.history = history.migrate_history(
            arc_root,
            zen_profile,
            backups,
            options.dry_run,
            history.HistoryOptions(
                nuke=options.replace,
                clear_bookmarks=options.replace,
                profiles=profile_filter,
            ),
        )
        result.bookmarks_cleared = int(result.history.get("cleared_bookmarks", 0))

        if result.history.get("aborted"):
            result.warnings.append(f"History not imported: {result.history.get('detail')}")
        elif not options.dry_run and not result.history.get("inserted"):
            if result.history.get("updated"):
                result.warnings.append(
                    f"No new history rows: all {result.history['updated']} Arc URL(s) were "
                    "already in Zen, most likely from an earlier run."
                )
            else:
                result.warnings.append(
                    "No history rows were written. Run 'arczen doctor' to see which "
                    "precondition failed."
                )
    elif options.replace:
        # History import is excluded, so do the wipe on its own.
        cleared = history.nuke_places(
            zen_profile,
            backups,
            bookmarks=True,
            dry_run=options.dry_run,
        )
        result.bookmarks_cleared = cleared["cleared_bookmarks"]
        result.history = {
            **history._new_history_stats(),
            **cleared,
            "detail": "cleared only; history excluded",
        }
    else:
        logger.info("History excluded")

    if options.copies("passwords"):
        try:
            result.passwords = passwords.export_passwords(
                arc_root,
                Path(options.password_csv).expanduser(),
                keychain_service=options.keychain_service,
                dry_run=options.dry_run,
                key=keys.get(),
                profiles=profile_filter,
            )
            if result.passwords and not options.dry_run:
                result.password_csv = Path(options.password_csv).expanduser()
        except (passwords.PasswordError, CryptoError) as error:
            logger.warning("Password export skipped: %s", error)
            result.warnings.append(f"Passwords not exported: {error}")
    else:
        logger.info("Password export excluded")

    if backups.saved:
        result.backup_dir = backups.root

    return result