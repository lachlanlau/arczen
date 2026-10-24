# arczen/build.py
"""Build Zen session structures from parsed Arc data and write them out."""

from __future__ import annotations

import copy
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import mozlz4
from .arcdata import ArcSidebar, ArcSpace, ArcTab
from .paths import BackupSet, zen_session_files
from .themes import zen_theme

logger = logging.getLogger(__name__)


@dataclass
class BuildOptions:
    # "tabs" at the command line: Arc's pinned tabs, their folders and Essentials.
    include_pinned_tabs: bool = True
    # "temporary-tabs" at the command line: Arc's unpinned tabs.
    include_temporary_tabs: bool = True
    # Copy Arc's collapsed/expanded folder state instead of opening everything.
    apply_folder_state: bool = True


def _uuid() -> str:
    return f"{{{uuid.uuid4()}}}"


class SessionBuilder:
    """Mints Zen tab, folder and group objects with globally unique IDs."""

    def __init__(self) -> None:
        self.stamp = int(datetime.now().timestamp() * 1000)
        self.counter = 0

    def _next(self) -> int:
        self.counter += 1
        return self.counter

    def sync_id(self, suffix: str = "") -> str:
        return f"{self.stamp}-{self._next()}{suffix}"

    def folder_id(self) -> str:
        return f"{self.stamp}-f{self._next()}"

    def tab(
        self,
        url: str,
        title: str,
        workspace_uuid: str,
        container_id: int,
        *,
        pinned: bool,
        essential: bool,
        group_id: str | None = None,
        image: str | None = None,
    ) -> dict[str, Any]:
        sync_id = self.sync_id()
        doc_id = self.counter + 100_000

        entry = {
            "url": url,
            "title": title,
            "cacheKey": 0,
            "ID": doc_id,
            "docshellUUID": _uuid(),
            "resultPrincipalURI": None,
            "hasUserInteraction": False,
            "triggeringPrincipal_base64": '{"3":{}}',
            "docIdentifier": doc_id,
            "children": [],
            "transient": False,
            "navigationKey": _uuid(),
            "navigationId": _uuid(),
        }

        tab = {
            "entries": [entry],
            "lastAccessed": self.stamp + self.counter,
            "pinned": pinned,
            "hidden": False,
            "zenWorkspace": workspace_uuid,
            "zenSyncId": sync_id,
            "zenEssential": essential,
            # Flag, not an id. "true" marks the tab as using its workspace's own
            # container, which is what zen.workspaces.hide-default-container-indicator
            # suppresses the coloured stripe for. null makes Zen treat the tab as
            # explicitly bound to some other container and draw the stripe anyway,
            # so every tab gets it, essential or not.
            "zenDefaultUserContextId": "true",
            "zenPinnedIcon": None,
            "zenIsEmpty": False,
            "zenHasStaticIcon": bool(image),
            "zenGlanceId": None,
            "zenIsGlance": False,
            "zenLiveFolderItemId": None,
            "searchMode": None,
            "userContextId": container_id,
            "attributes": {},
            # Firefox reads `index` as the active entry within `entries`, 1-based.
            "index": 1,
            "image": image or "",
        }

        if pinned:
            if group_id:
                tab["groupId"] = group_id
            tab["_zenPinnedInitialState"] = {"entry": copy.deepcopy(entry), "image": image}
            tab["_zenIsActiveTab"] = False

        return tab


    def folder_anchor(
        self,
        workspace_uuid: str,
        group_id: str,
        container_id: int,
    ) -> dict[str, Any]:
        """Hidden about:blank tab that stops Zen pruning a folder with no direct tabs."""
        sync_id = self.sync_id("-empty")
        doc_id = self.counter + 900_000

        entry = {
            "url": "about:blank",
            "title": "about:blank",
            "cacheKey": 0,
            "ID": doc_id,
            "docshellUUID": _uuid(),
            "resultPrincipalURI": None,
            "hasUserInteraction": False,
            "triggeringPrincipal_base64": '{"3":{}}',
            "docIdentifier": doc_id,
            "children": [],
            "transient": True,
            "navigationKey": _uuid(),
            "navigationId": _uuid(),
        }

        return {
            "entries": [entry],
            "lastAccessed": self.stamp + self.counter,
            "pinned": True,
            "hidden": False,
            "zenWorkspace": workspace_uuid,
            "zenSyncId": sync_id,
            "zenEssential": False,
            # Same flag as a real tab: the anchor is a pinned tab inside this
            # workspace, so it belongs to the workspace's own container.
            "zenDefaultUserContextId": "true",
            "zenPinnedIcon": None,
            "zenIsEmpty": True,
            "zenHasStaticIcon": False,
            "zenGlanceId": None,
            "zenIsGlance": False,
            "zenLiveFolderItemId": None,
            "searchMode": None,
            "userContextId": container_id,
            "attributes": {},
            "index": 1,
            "image": None,
            "groupId": group_id,
            "_zenPinnedInitialState": {"entry": copy.deepcopy(entry), "image": None},
            "_zenIsActiveTab": False,
        }


def _anchor_paths(space: ArcSpace) -> set[tuple[str, ...]]:
    """Folders needing a hidden anchor tab: those with no direct pinned tab.

    Zen prunes a tab group that holds no tabs of its own, which would reparent
    its children to the workspace root and flatten the hierarchy.
    """
    direct = {
        tab.folder_path
        for tab in space.tabs
        if tab.pinned and tab.folder_path
    }
    return {folder.path for folder in space.folders if folder.path not in direct}


def _essentials_by_space(
    sidebar: ArcSidebar,
    container_ids: dict[str, int],
    container_key: dict[str, str],
) -> dict[str, list[ArcTab]]:
    """Essentials each space should write out, keyed by space id.

    Zen scopes Essentials to the container, not the workspace: every space
    sharing a container shows the same strip, and removing an entry in one
    space removes it from all of them. So exactly one copy per container is
    written, by the first space using that container, and the remaining spaces
    emit none - they still show the strip.

    Grouping is on the resolved userContextId because that is what Zen scopes
    by. With --no-containers every space sits in container 0, so the whole
    migration gets one shared set; where several Arc profiles land in one
    container their Essentials are merged, deduplicated by URL.
    """
    host_for_container: dict[int, str] = {}
    profiles_for_container: dict[int, list[str]] = {}

    for space in sidebar.spaces:
        container = container_ids.get(container_key.get(space.space_id, ""), 0)
        host_for_container.setdefault(container, space.space_id)
        bucket = profiles_for_container.setdefault(container, [])
        if space.profile not in bucket:
            bucket.append(space.profile)

    by_space: dict[str, list[ArcTab]] = {}
    for container, host in host_for_container.items():
        profiles = profiles_for_container[container]
        tabs: list[ArcTab] = []
        seen: set[str] = set()
        for profile in profiles:
            for tab in sidebar.essentials_for(profile):
                if tab.url in seen:
                    continue
                seen.add(tab.url)
                tabs.append(tab)
        by_space[host] = tabs

        if len(profiles) > 1:
            logger.info(
                "Container %d is shared by Arc profiles %s; their Essentials are "
                "merged into one strip (%d tab(s) after removing duplicates)",
                container,
                ", ".join(profiles),
                len(tabs),
            )

    return by_space


def _space_sequence(
    space: ArcSpace,
    options: BuildOptions,
    essentials: list[ArcTab],
    anchor_paths: set[tuple[str, ...]],
) -> list[tuple[str, object]]:
    """Arc's sidebar sequence for one space, as ("tab", ArcTab) / ("anchor", path).

    Essentials first, then pinned content with each folder's anchor sitting
    exactly where Arc showed that folder, then unpinned tabs. `essentials` is
    empty for every space whose container strip was already written elsewhere.
    """
    sequence: list[tuple[str, object]] = []

    if options.include_pinned_tabs:
        sequence.extend(("tab", tab) for tab in essentials)

        # Folders and pinned tabs share one counter from Arc's depth-first walk,
        # so a single sort over both reproduces the sidebar order.
        events: list[tuple[int, str, object]] = [
            (folder.order, "anchor", folder.path)
            for folder in space.folders
            if folder.path in anchor_paths
        ]
        events.extend((tab.order, "tab", tab) for tab in space.tabs if tab.pinned)
        events.sort(key=lambda event: event[0])
        sequence.extend((kind, payload) for _, kind, payload in events)

    if options.include_temporary_tabs:
        open_tabs = sorted(
            (tab for tab in space.tabs if not tab.pinned),
            key=lambda tab: tab.order,
        )
        sequence.extend(("tab", tab) for tab in open_tabs)

    return sequence


def build_session(
    sidebar: ArcSidebar,
    container_ids: dict[str, int],
    container_key: dict[str, str],
    icons: dict[str, str],
    options: BuildOptions,
) -> dict[str, Any]:
    """Produce a complete zen-sessions.jsonlz4 payload."""
    builder = SessionBuilder()
    spaces: list[dict[str, Any]] = []
    folders: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    tabs: list[dict[str, Any]] = []

    # Essentials belong to the container, so they are written once per container
    # rather than once per space.
    essentials_by_space = _essentials_by_space(sidebar, container_ids, container_key)

    for space in sidebar.spaces:
        workspace_uuid = _uuid()
        container_id = container_ids.get(container_key.get(space.space_id, ""), 0)

        workspace = {
            "uuid": workspace_uuid,
            "name": space.name,
            "theme": zen_theme(space.window_theme),
            "containerTabId": container_id,
            "hasCollapsedPinnedTabs": False,
        }
        if space.icon:
            workspace["icon"] = space.icon
        spaces.append(workspace)

        # Folder ids in Arc's own order. Arc's depth-first walk already emits a
        # parent before all its descendants, so ordering by `order` alone is
        # both parent-safe and faithful to the sidebar. Folders belong to the
        # pinned structure, so they go when "tabs" is excluded.
        path_to_id: dict[tuple[str, ...], str] = {}
        ordered_folders = (
            sorted(space.folders, key=lambda folder: folder.order)
            if options.include_pinned_tabs
            else []
        )
        space_folders: list[dict[str, Any]] = []

        for folder in ordered_folders:
            if folder.path in path_to_id:
                continue

            folder_id = builder.folder_id()
            path_to_id[folder.path] = folder_id
            collapsed = folder.collapsed if options.apply_folder_state else False

            space_folders.append(
                {
                    "pinned": True,
                    "splitViewGroup": False,
                    "id": folder_id,
                    "name": folder.path[-1],
                    "collapsed": collapsed,
                    "saveOnWindowClose": True,
                    "parentId": path_to_id.get(folder.path[:-1]),
                    "prevSiblingInfo": {"type": "start", "id": None},
                    "emptyTabIds": [],
                    "userIcon": "",
                    "workspaceId": workspace_uuid,
                }
            )
            groups.append(
                {
                    "pinned": True,
                    "splitView": False,
                    "id": folder_id,
                    "name": folder.path[-1],
                    "color": "zen-workspace-color",
                    "collapsed": collapsed,
                    "saveOnWindowClose": True,
                }
            )

        _chain_siblings(space_folders, path_to_id, ordered_folders)
        folders.extend(space_folders)
        folder_by_id = {folder["id"]: folder for folder in space_folders}

        hosts_essentials = space.space_id in essentials_by_space
        sequence = _space_sequence(
            space,
            options,
            essentials_by_space.get(space.space_id, []),
            _anchor_paths(space),
        )

        space_tab_count = 0
        essential_count = 0
        for kind, payload in sequence:
            if kind == "anchor":
                folder_id = path_to_id.get(payload)  # type: ignore[arg-type]
                if not folder_id:
                    continue
                anchor = builder.folder_anchor(workspace_uuid, folder_id, container_id)
                folder_by_id[folder_id]["emptyTabIds"] = [anchor["zenSyncId"]]
                tabs.append(anchor)
                continue

            arc_tab: ArcTab = payload  # type: ignore[assignment]
            group_id = path_to_id.get(arc_tab.folder_path) if arc_tab.pinned else None
            tabs.append(
                builder.tab(
                    arc_tab.url,
                    arc_tab.title,
                    workspace_uuid,
                    container_id,
                    pinned=arc_tab.pinned,
                    essential=arc_tab.essential,
                    group_id=group_id,
                    image=icons.get(arc_tab.url),
                )
            )
            space_tab_count += 1
            if arc_tab.essential:
                essential_count += 1

        logger.info(
            "Built workspace %r: container %s, %d tabs (%d essentials), %d folders",
            space.name,
            container_id,
            space_tab_count,
            essential_count,
            len(space_folders),
        )
        if options.include_pinned_tabs and not hosts_essentials:
            logger.info(
                "  Essentials for %r come from container %s, already written once",
                space.name,
                container_id,
            )

    return {
        "spaces": spaces,
        "tabs": tabs,
        "folders": folders,
        "groups": groups,
        "splitViewData": [],
        "lastCollected": builder.stamp,
    }


def _chain_siblings(
    space_folders: list[dict[str, Any]],
    path_to_id: dict[tuple[str, ...], str],
    ordered_folders: list[Any],
) -> None:
    """Set prevSiblingInfo so Zen restores nested folders in Arc's order."""
    order_by_id = {
        path_to_id[folder.path]: folder.order
        for folder in ordered_folders
        if folder.path in path_to_id
    }
    by_parent: dict[str | None, list[dict[str, Any]]] = {}
    for folder in space_folders:
        by_parent.setdefault(folder["parentId"], []).append(folder)

    for siblings in by_parent.values():
        siblings.sort(key=lambda folder: order_by_id.get(folder["id"], 1_000_000))
        previous: str | None = None
        for folder in siblings:
            folder["prevSiblingInfo"] = (
                {"type": "group", "id": previous} if previous else {"type": "start", "id": None}
            )
            previous = folder["id"]


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def _blank_sessionstore(stamp: int) -> dict[str, Any]:
    return {
        "version": ["sessionrestore", 1],
        "windows": [
            {
                "tabs": [],
                "selected": 1,
                "_closedTabs": [],
                "busy": False,
                "width": 1280,
                "height": 860,
                "screenX": 80,
                "screenY": 80,
                "sizemode": "normal",
            }
        ],
        "selectedWindow": 1,
        "_closedWindows": [],
        "session": {
            "state": "running",
            "lastUpdate": stamp,
            "startTime": stamp,
            "recentCrashes": 0,
        },
        "global": {},
    }


def _reset_window(window: dict[str, Any]) -> None:
    window["tabs"] = []
    window["folders"] = []
    window["groups"] = []
    window["splitViews"] = []
    window["splitViewData"] = []
    window["closedGroups"] = []
    window["_closedTabs"] = []
    window["_lastClosedTabGroupCount"] = -1
    window["lastClosedTabGroupId"] = None
    # Session cookies ride inside the session files, so emptying cookies.sqlite
    # on its own would let them restore on next launch.
    window["cookies"] = []
    window["selected"] = 1


def write_session(
    profile: Path,
    session: dict[str, Any],
    backups: BackupSet,
    nuke: bool = False,
    dry_run: bool = False,
) -> None:
    """Write zen-sessions.jsonlz4 and mirror it into the Firefox sessionstore files.

    `nuke` is replace mode: Zen's own tabs, folders, groups and closed-tab state
    are dropped instead of merged with Arc's.
    """
    files = zen_session_files(profile)
    stamp = session["lastCollected"]

    if dry_run:
        logger.info(
            "Dry run: would write %d workspaces / %d tabs / %d folders to %s",
            len(session["spaces"]),
            len(session["tabs"]),
            len(session["folders"]),
            files["zen_sessions"].name,
        )
        return

    # 1. Zen's authoritative workspace store.
    target = files["zen_sessions"]
    existing: dict[str, Any] = {}
    if target.is_file():
        backups.save(target)
        try:
            existing = mozlz4.read_json(target)
        except (OSError, ValueError) as error:
            logger.warning("Existing %s unreadable (%s); writing fresh", target.name, error)

    payload = dict(existing)
    payload.update(session)
    if nuke:
        payload["savedGroups"] = []
        payload["_closedWindows"] = []
        payload["maxSplitViewId"] = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    mozlz4.write_json(target, payload)
    logger.info("Wrote %s", target.name)

    # 2. Firefox session files. Zen takes the intersection by zenSyncId, so the
    #    same tab objects must exist in both stores or restored tabs get pruned.
    space_uuids = {space["uuid"] for space in session["spaces"]}
    first_unpinned = next(
        (index for index, tab in enumerate(session["tabs"], start=1) if not tab["pinned"]),
        1,
    )

    for key in ("sessionstore", "recovery"):
        path = files[key]
        if path.is_file():
            backups.save(path)
            try:
                data = mozlz4.read_json(path)
            except (OSError, ValueError) as error:
                logger.warning("%s unreadable (%s); rebuilding", path.name, error)
                data = _blank_sessionstore(stamp)
        else:
            data = _blank_sessionstore(stamp)
            path.parent.mkdir(parents=True, exist_ok=True)

        windows = data.get("windows") or []
        if not windows:
            windows = _blank_sessionstore(stamp)["windows"]
            data["windows"] = windows

        if nuke:
            for window in windows:
                _reset_window(window)
            data["_closedWindows"] = []
            # Older builds park session cookies at the top level.
            data["cookies"] = []

        window = windows[0]
        window["spaces"] = copy.deepcopy(session["spaces"])
        window["folders"] = copy.deepcopy(session["folders"])
        window["groups"] = copy.deepcopy(session["groups"])
        window["splitViewData"] = []
        window["tabs"] = copy.deepcopy(session["tabs"])
        window["selected"] = max(1, min(first_unpinned, len(window["tabs"]) or 1))
        if space_uuids and window.get("activeZenSpace") not in space_uuids:
            window["activeZenSpace"] = session["spaces"][0]["uuid"]

        mozlz4.write_json(path, data)
        logger.info("Wrote %s", path.name)