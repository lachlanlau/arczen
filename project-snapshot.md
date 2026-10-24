# arczen — project snapshot

Generated from `/Users/lachlan/Downloads/arczen`. Included extensions: py.

## Project structure

```
arczen/
├── arczen/
│   ├── __init__.py
│   ├── arcdata.py
│   ├── build.py
│   ├── chromium_crypto.py
│   ├── cli.py
│   ├── containers.py
│   ├── doctor.py
│   ├── favicons.py
│   ├── history.py
│   ├── icons.py
│   ├── inspect.py
│   ├── migrate.py
│   ├── mozlz4.py
│   ├── passwords.py
│   ├── paths.py
│   ├── prefs.py
│   ├── privacy.py
│   └── themes.py
├── arczen.egg-info/
│   ├── dependency_links.txt
│   ├── entry_points.txt
│   ├── PKG-INFO
│   ├── requires.txt
│   ├── SOURCES.txt
│   └── top_level.txt
├── images/
│   └── arc.png
├── .gitignore
├── arc-passwords.csv
├── jslz4.py
├── pyproject.toml
└── readme.md
```

## File summary

| File | Lines | Approx tokens |
|---|---:|---:|
| `arczen/__init__.py` | 6 | 28 |
| `arczen/arcdata.py` | 408 | 3,621 |
| `arczen/build.py` | 589 | 5,086 |
| `arczen/chromium_crypto.py` | 176 | 1,552 |
| `arczen/cli.py` | 395 | 3,712 |
| `arczen/containers.py` | 434 | 3,675 |
| `arczen/doctor.py` | 336 | 2,865 |
| `arczen/favicons.py` | 125 | 1,002 |
| `arczen/history.py` | 1368 | 12,173 |
| `arczen/icons.py` | 751 | 7,009 |
| `arczen/inspect.py` | 173 | 1,620 |
| `arczen/migrate.py` | 618 | 6,075 |
| `arczen/mozlz4.py` | 33 | 249 |
| `arczen/passwords.py` | 131 | 1,067 |
| `arczen/paths.py` | 360 | 3,034 |
| `arczen/prefs.py` | 97 | 713 |
| `arczen/privacy.py` | 340 | 2,789 |
| `arczen/themes.py` | 260 | 2,224 |
| `jslz4.py` | 43 | 404 |
| **19 files** | **6,643** | **~58,898** |

## Source

### `arczen/__init__.py`

```python
# arczen/__init__.py
"""Ultimate Arc to Zen migration package."""

__all__ = ["__version__"]

__version__ = "1.0.0"
```

### `arczen/arcdata.py`

```python
# arczen/arcdata.py
"""Parse Arc's StorableSidebar.json into ordered, typed migration data."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

DEFAULT_PROFILE = "Default"


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class ArcTab:
    url: str
    title: str
    folder_path: tuple[str, ...]
    pinned: bool
    essential: bool
    order: int


@dataclass
class ArcFolder:
    path: tuple[str, ...]
    order: int
    collapsed: bool


@dataclass
class ArcSplitMember:
    """One pane of a split view."""

    url: str
    title: str
    # Arc records a factor for some panes only; None means it never set one.
    width_factor: float | None = None


@dataclass
class ArcSplitView:
    """Tabs Arc showed side by side in one sidebar slot.

    The member tabs are also present in ArcSpace.tabs as ordinary tabs, so the
    pages migrate whether or not the split itself can be rebuilt.
    """

    item_id: str
    orientation: str                 # horizontal | vertical
    pinned: bool
    folder_path: tuple[str, ...]
    order: int
    members: list[ArcSplitMember] = field(default_factory=list)


@dataclass
class ArcSpace:
    space_id: str
    name: str
    profile: str
    icon: str | None                      # resolved Zen value, filled in by the migrator
    icon_emoji: str | None
    icon_name: str | None                 # raw Arc/SF Symbol identifier
    icon_type_raw: dict[str, Any]         # untouched iconType, for inspect
    window_theme: dict[str, Any]
    folders: list[ArcFolder] = field(default_factory=list)
    tabs: list[ArcTab] = field(default_factory=list)
    splits: list[ArcSplitView] = field(default_factory=list)


@dataclass
class ArcSidebar:
    spaces: list[ArcSpace]
    essentials: dict[str, list[ArcTab]]

    def essentials_for(self, profile: str) -> list[ArcTab]:
        return self.essentials.get(profile, [])


# --------------------------------------------------------------------------
# Low level helpers for Arc's flat [id, object, id, object] arrays
# --------------------------------------------------------------------------

def _alternating(values: Any) -> Iterator[tuple[str, Any]]:
    if not isinstance(values, list):
        return
    index = 0
    while index < len(values) - 1:
        key = values[index]
        if isinstance(key, str):
            yield key, values[index + 1]
            index += 2
        else:
            index += 1


def _dig(source: Any, *keys: str) -> Any:
    current = source
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _load_json(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _expanded_item_ids(arc_root: Path) -> set[str]:
    """Folder IDs Arc currently shows expanded (from StorableWindows.json)."""
    path = arc_root / "StorableWindows.json"
    if not path.is_file():
        return set()

    try:
        data = _load_json(path)
    except (OSError, json.JSONDecodeError):
        logger.warning("StorableWindows.json unreadable; treating all folders as expanded")
        return set()

    expanded: set[str] = set()
    for window in data.get("windows", []):
        for item_id, state in _alternating(window.get("expandedItems", [])):
            if isinstance(state, dict) and "expanded" in state:
                expanded.add(item_id)
    return expanded


def _section_container_ids(container_ids: list[str], marker: str) -> list[str]:
    """Container UUIDs that follow a 'pinned' or 'unpinned' marker."""
    if marker not in container_ids:
        return []

    collected = []
    for container_id in container_ids[container_ids.index(marker) + 1:]:
        if container_id in ("pinned", "unpinned"):
            break
        collected.append(container_id)
    return collected


def _profile_of(space_blob: dict[str, Any]) -> str | None:
    return _dig(space_blob, "profile", "custom", "_0", "directoryBasename")


def _icon_of(custom_info: dict[str, Any]) -> tuple[str | None, str | None, dict[str, Any]]:
    """Split Arc's iconType into (emoji, named identifier, raw blob).

    Resolution is deliberately deferred: Arc's named icons are SF Symbols and do
    not line up with Zen's filenames, so they need validating against Zen's
    actual icon set before being turned into a chrome:// URL.
    """
    icon_type = custom_info.get("iconType") or {}
    if not isinstance(icon_type, dict):
        return None, None, {}

    emoji = icon_type.get("emoji_v2") or icon_type.get("emoji")
    if isinstance(emoji, dict):
        emoji = emoji.get("_0") or emoji.get("value")

    name = icon_type.get("icon") or icon_type.get("sfSymbol") or icon_type.get("builtin")
    if isinstance(name, dict):
        name = name.get("_0") or name.get("value") or name.get("name")

    return (
        str(emoji) if emoji else None,
        str(name) if name else None,
        icon_type,
    )


def _split_members(
    item: dict[str, Any],
    blob: dict[str, Any],
    items: dict[str, Any],
) -> list[ArcSplitMember]:
    """Panes of one split view, in Arc's order.

    itemWidthFactors is Arc's usual alternating [id, factor] array, and only
    holds an entry for some panes, so a missing factor is not an error.
    """
    factors: dict[str, float] = {}
    for member_id, value in _alternating(blob.get("itemWidthFactors") or []):
        try:
            factors[member_id] = float(value)
        except (TypeError, ValueError):
            continue

    members: list[ArcSplitMember] = []
    for child_id in item.get("childrenIds") or []:
        child = items.get(child_id)
        if not isinstance(child, dict):
            continue
        tab_blob = _dig(child, "data", "tab") or {}
        url = tab_blob.get("savedURL") or ""
        if not url or url.startswith("arc://"):
            continue
        members.append(
            ArcSplitMember(
                url=url,
                title=child.get("title") or tab_blob.get("savedTitle") or url,
                width_factor=factors.get(child_id),
            )
        )
    return members


def parse_sidebar(arc_root: Path) -> ArcSidebar:
    """Read Arc's sidebar into ordered spaces, folders, tabs and Essentials."""
    data = _load_json(arc_root / "StorableSidebar.json")
    expanded = _expanded_item_ids(arc_root)

    containers = data.get("sidebar", {}).get("containers", [])
    local = containers[1] if len(containers) > 1 and isinstance(containers[1], dict) else {}
    items = dict(_alternating(local.get("items", [])))
    local_spaces = list(_alternating(local.get("spaces", [])))

    sync_models = {
        space_id: (blob.get("value", {}) if isinstance(blob, dict) else {})
        for space_id, blob in _alternating(
            _dig(data, "firebaseSyncState", "syncData", "spaceModels") or []
        )
    }

    spaces: list[ArcSpace] = []
    for space_id, space_blob in local_spaces:
        if not isinstance(space_blob, dict):
            continue

        sync_blob = sync_models.get(space_id, {})
        name = space_blob.get("title") or sync_blob.get("title") or f"Space {space_id[:8]}"
        custom_info = space_blob.get("customInfo") or sync_blob.get("customInfo") or {}
        profile = _profile_of(space_blob) or _profile_of(sync_blob) or DEFAULT_PROFILE

        icon_emoji, icon_name, icon_raw = _icon_of(custom_info)
        space = ArcSpace(
            space_id=space_id,
            name=name,
            profile=profile,
            icon=None,
            icon_emoji=icon_emoji,
            icon_name=icon_name,
            icon_type_raw=icon_raw,
            window_theme=custom_info.get("windowTheme") or sync_blob.get("customInfo", {}).get("windowTheme") or {},
        )

        container_ids = space_blob.get("containerIDs", [])
        counter = 0

        def walk(item_ids: list[str], path: tuple[str, ...], pinned: bool) -> None:
            nonlocal counter
            for item_id in item_ids:
                item = items.get(item_id)
                if not isinstance(item, dict):
                    continue

                payload = item.get("data") or {}

                if "tab" in payload:
                    url = (payload["tab"] or {}).get("savedURL") or ""
                    if not url or url.startswith("arc://"):
                        continue
                    title = item.get("title") or (payload["tab"] or {}).get("savedTitle") or url
                    space.tabs.append(
                        ArcTab(
                            url=url,
                            title=title,
                            folder_path=path if pinned else (),
                            pinned=pinned,
                            essential=False,
                            order=counter,
                        )
                    )
                    counter += 1

                elif "list" in payload:
                    title = item.get("title") or "Untitled Folder"
                    children = item.get("childrenIds") or []
                    if pinned:
                        folder_path = path + (title,)
                        space.folders.append(
                            ArcFolder(
                                path=folder_path,
                                order=counter,
                                collapsed=item_id not in expanded,
                            )
                        )
                        counter += 1
                        walk(children, folder_path, pinned)
                    else:
                        # Arc nests open tabs under folders too; flatten them.
                        walk(children, path, pinned)

                elif "splitView" in payload or "split" in payload:
                    blob = payload.get("splitView") or payload.get("split") or {}
                    if not isinstance(blob, dict):
                        blob = {}
                    split = ArcSplitView(
                        item_id=item_id,
                        orientation=str(blob.get("layoutOrientation") or "horizontal"),
                        pinned=pinned,
                        folder_path=path if pinned else (),
                        order=counter,
                        members=_split_members(item, blob, items),
                    )
                    if split.members:
                        space.splits.append(split)
                    counter += 1
                    # Zen's split format is not yet reproduced, so the panes are
                    # migrated as ordinary tabs in Arc's order. The pages arrive
                    # either way; only the side-by-side layout is lost.
                    walk(item.get("childrenIds") or [], path, pinned)

                else:
                    # Container type not modelled here. Descend regardless so
                    # any tabs inside it survive instead of being dropped with
                    # the container.
                    children = item.get("childrenIds") or []
                    if children:
                        logger.info(
                            "Unmodelled Arc item %s (payload keys: %s) holds %d child "
                            "item(s); copying them as plain tabs",
                            item_id[:8],
                            ", ".join(sorted(payload)) or "none",
                            len(children),
                        )
                        walk(children, path, pinned)

        for marker, pinned in (("pinned", True), ("unpinned", False)):
            for container_id in _section_container_ids(container_ids, marker):
                container = items.get(container_id) or {}
                walk(container.get("childrenIds") or [], (), pinned)

        spaces.append(space)
        logger.info(
            "Arc space %r (profile %s): %d pinned, %d open, %d folders, %d split view(s)",
            space.name,
            space.profile,
            sum(1 for tab in space.tabs if tab.pinned),
            sum(1 for tab in space.tabs if not tab.pinned),
            len(space.folders),
            len(space.splits),
        )
        for split in space.splits:
            logger.info(
                "  Split view (%s, %s): %s",
                split.orientation,
                "pinned" if split.pinned else "open",
                " | ".join(member.title for member in split.members),
            )

    essentials = _parse_essentials(items)
    return ArcSidebar(spaces=spaces, essentials=essentials)


def _parse_essentials(items: dict[str, Any]) -> dict[str, list[ArcTab]]:
    """Collect every topApps container, keyed by the Arc profile that owns it."""
    essentials: dict[str, list[ArcTab]] = {}

    for container_id, container in items.items():
        container_type = _dig(container, "data", "itemContainer", "containerType")
        if not isinstance(container_type, dict) or "topApps" not in container_type:
            continue

        blob = container_type["topApps"].get("_0") or {}
        profile = _dig(blob, "custom", "_0", "directoryBasename")
        if not profile:
            profile = DEFAULT_PROFILE

        bucket = essentials.setdefault(profile, [])
        for order, tab_id in enumerate(container.get("childrenIds") or []):
            tab_blob = _dig(items.get(tab_id) or {}, "data", "tab") or {}
            url = tab_blob.get("savedURL") or ""
            if not url or url.startswith("arc://"):
                continue
            bucket.append(
                ArcTab(
                    url=url,
                    title=tab_blob.get("savedTitle") or url,
                    folder_path=(),
                    pinned=True,
                    essential=True,
                    order=order,
                )
            )

        logger.info(
            "Arc Essentials container %s -> profile %s (%d tabs)",
            container_id[:8],
            profile,
            len(bucket),
        )

    return essentials
```

### `arczen/build.py`

```python
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
```

### `arczen/chromium_crypto.py`

```python
# arczen/chromium_crypto.py
"""Shared Chromium (Arc) secret-store key handling and value decryption.

Both the cookie jar and the password store use the same OS-held master key,
so it is derived once here and reused.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# macOS Keychain generic-password items Arc is known to use, in priority order.
KEYCHAIN_SERVICES = ["Arc Safe Storage", "Arc", "Chromium Safe Storage"]

# Chromium's fixed PBKDF2 parameters for the macOS keyring path.
MAC_SALT = b"saltysalt"
MAC_ITERATIONS = 1003
MAC_KEY_LENGTH = 16
MAC_IV = b" " * 16


class CryptoError(RuntimeError):
    """Raised when Arc's encrypted stores cannot be opened."""


def _require_backend():
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as error:  # pragma: no cover - import guard
        raise CryptoError(
            "Reading Arc's encrypted stores needs the cryptography package: "
            "pip install cryptography"
        ) from error
    return Cipher, algorithms, modes


def _strip_pkcs7(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data

# --------------------------------------------------------------------------
# Key material
# --------------------------------------------------------------------------

def _macos_key(service_override: str | None) -> bytes:
    services = [service_override] if service_override else KEYCHAIN_SERVICES

    for service in services:
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-w", "-s", service],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as error:
            raise CryptoError("The macOS 'security' tool is unavailable") from error

        secret = result.stdout.strip()
        if result.returncode == 0 and secret:
            logger.info("Using Keychain item %r for Arc decryption", service)
            return hashlib.pbkdf2_hmac(
                "sha1",
                secret.encode("utf-8"),
                MAC_SALT,
                MAC_ITERATIONS,
                MAC_KEY_LENGTH,
            )

    raise CryptoError(
        "Could not read Arc's Keychain secret. Approve the Keychain prompt when it "
        "appears, or pass --keychain-service with the exact item name shown in "
        "Keychain Access."
    )


def _windows_key(arc_root: Path) -> bytes:
    import ctypes
    from ctypes import wintypes

    local_state = arc_root / "User Data" / "Local State"
    if not local_state.is_file():
        raise CryptoError(f"Missing Arc Local State file: {local_state}")

    with open(local_state, encoding="utf-8") as handle:
        state = json.load(handle)

    encoded = (state.get("os_crypt") or {}).get("encrypted_key")
    if not encoded:
        raise CryptoError("Arc Local State has no os_crypt.encrypted_key")

    blob = base64.b64decode(encoded)
    if not blob.startswith(b"DPAPI"):
        raise CryptoError("Unexpected master key format in Arc Local State")
    blob = blob[5:]

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(blob, len(blob))
    source = DataBlob(len(blob), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    output = DataBlob()

    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output)
    ):
        raise CryptoError(
            "CryptUnprotectData refused Arc's master key. The key is bound to your "
            "Windows account, so this must run as the same user that uses Arc."
        )

    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


@dataclass
class ChromiumKey:
    """The platform master key, plus the matching cipher mode."""

    platform: str
    material: bytes

    def decrypt(self, blob: bytes) -> str:
        """Decrypt one `password_value` blob to text."""

        Cipher, algorithms, modes = _require_backend()
        prefix = blob[:3]

        if prefix == b"v20":
            raise CryptoError(
                "This Arc build uses app-bound encryption (v20). Those values are tied "
                "to the browser binary and cannot be decrypted externally."
            )

        if self.platform == "darwin":
            payload = blob[3:] if prefix in (b"v10", b"v11") else blob
            if len(payload) % 16:
                raise CryptoError("Ciphertext is not a whole number of AES blocks")
            decryptor = Cipher(algorithms.AES(self.material), modes.CBC(MAC_IV)).decryptor()
            plaintext = _strip_pkcs7(decryptor.update(payload) + decryptor.finalize())

        elif self.platform == "win32":
            if prefix != b"v10":
                raise CryptoError("Unrecognised ciphertext prefix for a Windows store")
            nonce, ciphertext, tag = blob[3:15], blob[15:-16], blob[-16:]
            decryptor = Cipher(algorithms.AES(self.material), modes.GCM(nonce, tag)).decryptor()
            plaintext = decryptor.update(ciphertext) + decryptor.finalize()

        else:  # pragma: no cover - Arc ships on macOS and Windows only
            raise CryptoError(f"Unsupported platform for Arc decryption: {self.platform}")

        return plaintext.decode("utf-8", errors="replace")


def load_key(arc_root: Path, keychain_service: str | None = None) -> ChromiumKey:
    """Derive Arc's master key from the OS secret store."""
    if sys.platform == "darwin":
        return ChromiumKey("darwin", _macos_key(keychain_service))
    if sys.platform == "win32":
        return ChromiumKey("win32", _windows_key(arc_root))
    raise CryptoError("Arc's encrypted stores can only be read on macOS and Windows")
```

### `arczen/cli.py`

```python
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
```

### `arczen/containers.py`

```python
# arczen/containers.py
"""Contextual identity (container) setup driven by Arc space icons and colours.

Two modes, deliberately asymmetric:

  replace  containers.json is rebuilt from nothing. Every pre-existing identity
           goes, ours and the stock ones alike, and the Arc containers are
           numbered from 1. No matching, because nothing survives to match.
  merge    the user's containers stay. Ours are found by the arczenKey stamped
           on them, then by any label an older arczen version could have used,
           so a changed label format updates the existing record instead of
           adding a second one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .icons import container_icon_for, container_icon_from_seed
from .paths import BackupSet
from .themes import nearest_container_color

logger = logging.getLogger(__name__)

# Stamped onto every identity arczen writes, holding the Arc profile key it
# stands for. Written in both modes, matched on only in merge: it is the stable
# handle a later merge needs, since labels have changed format between versions.
ARCZEN_KEY = "arczenKey"


@dataclass
class ContainerRequest:
    key: str
    label: str
    window_theme: dict[str, Any]
    icon_emoji: str | None = None
    icon_name: str | None = None
    # Every name an earlier arczen version could have given this container.
    # Used by merge only.
    aliases: tuple[str, ...] = ()


@dataclass
class ContainerPlan:
    """What ensure_containers did, surfaced for the summary and inspect output."""

    mapping: dict[str, int]
    created: list[dict[str, Any]] = field(default_factory=list)
    reused: list[dict[str, Any]] = field(default_factory=list)
    purged: list[str] = field(default_factory=list)
    # Merge only: duplicates of a container this run wrote, left in place
    # because merge does not delete. Reported so they can be removed by hand.
    stale: list[str] = field(default_factory=list)


def _default_config() -> dict[str, Any]:
    return {
        "version": 5,
        "lastUserContextId": 4,
        "identities": [
            {"userContextId": 1, "public": True, "icon": "fingerprint", "color": "blue",
             "l10nId": "user-context-personal"},
            {"userContextId": 2, "public": True, "icon": "briefcase", "color": "orange",
             "l10nId": "user-context-work"},
            {"userContextId": 3, "public": True, "icon": "dollar", "color": "green",
             "l10nId": "user-context-banking"},
            {"userContextId": 4, "public": True, "icon": "cart", "color": "pink",
             "l10nId": "user-context-shopping"},
        ],
    }


def load_config(profile: Path) -> dict[str, Any]:
    path = profile / "containers.json"
    if not path.is_file():
        return _default_config()
    try:
        with open(path, encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, json.JSONDecodeError):
        logger.warning("containers.json unreadable; rebuilding from Firefox defaults")
        return _default_config()

    config.setdefault("version", 5)
    if not isinstance(config.get("identities"), list):
        config["identities"] = []

    # Drop anything that is not a usable identity record. One malformed entry is
    # enough to break Zen's container UI.
    config["identities"] = [
        identity for identity in config["identities"] if isinstance(identity, dict)
    ]

    try:
        last = int(config.get("lastUserContextId", 0))
    except (TypeError, ValueError):
        last = 0
    config["lastUserContextId"] = max(
        last,
        max((_context_id(identity) for identity in config["identities"]), default=0),
    )
    return config


def _context_id(identity: dict[str, Any]) -> int:
    try:
        return int(identity.get("userContextId") or 0)
    except (TypeError, ValueError):
        return 0


def _is_builtin(identity: dict[str, Any]) -> bool:
    """Firefox's stock identities are the ones carrying an l10nId."""
    return bool(identity.get("l10nId"))


def _is_ours(identity: dict[str, Any]) -> bool:
    return bool(identity.get(ARCZEN_KEY))


def _name_key(identity: dict[str, Any]) -> str:
    return str(identity.get("name") or "").strip().lower()


def _describe(identity: dict[str, Any]) -> str:
    name = identity.get("name") or identity.get("l10nId") or "unnamed"
    return f"{name} (id {_context_id(identity)})"


def _alias_set(request: ContainerRequest) -> set[str]:
    """Lowered names that identify this container, current label included."""
    return {
        name.strip().lower()
        for name in (request.label, *request.aliases)
        if name and name.strip()
    }


def plan_identity(request: ContainerRequest) -> dict[str, Any]:
    """Colour and icon for one container, derived from its Arc space."""
    color, reading = nearest_container_color(request.window_theme, seed=request.label)

    icon = container_icon_for(request.icon_emoji, request.icon_name)
    if icon == "circle" and not (request.icon_emoji or request.icon_name):
        icon = container_icon_from_seed(request.label)

    logger.info(
        "Container %r -> %s/%s (Arc colour via %s: %s)",
        request.label,
        icon,
        color,
        reading.strategy,
        reading.colors[0] if reading.colors else "none",
    )
    return {"icon": icon, "color": color}


def _renumber_collisions(identities: list[dict[str, Any]]) -> int:
    """Give every identity a distinct, positive userContextId.

    Merge path only: the user's file can already hold a collision, and two
    identities sharing an id makes Firefox resolve the wrong record while a tab
    pointing at the loser resolves nothing at all. Returns how many moved.
    """
    seen: set[int] = set()
    moved = 0
    high = max((_context_id(identity) for identity in identities), default=0)

    for identity in identities:
        current = _context_id(identity)
        if current > 0 and current not in seen:
            seen.add(current)
            continue
        high += 1
        while high in seen:
            high += 1
        logger.warning(
            "Container %r had an unusable id (%s); renumbered to %d",
            identity.get("name") or identity.get("l10nId") or "unnamed",
            identity.get("userContextId"),
            high,
        )
        identity["userContextId"] = high
        seen.add(high)
        moved += 1

    return moved


def _write(profile: Path, config: dict[str, Any], backups: BackupSet) -> None:
    path = profile / "containers.json"
    backups.save(path)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, separators=(",", ":"))


# --------------------------------------------------------------------------
# replace: rebuild from nothing
# --------------------------------------------------------------------------

def _rebuild_containers(
    profile: Path,
    requests: list[ContainerRequest],
    backups: BackupSet,
    dry_run: bool,
) -> ContainerPlan:
    """Discard every existing identity and write one container per Arc profile.

    Site data lives in the profile keyed by userContextId, not in this file, so
    numbering from 1 hands the old container 1's cookie jar to the new one.
    Clear site data from Zen's settings if that is not wanted.
    """
    # Read only to report what is being dropped; nothing from it is kept.
    previous = load_config(profile)["identities"]
    purged = [_describe(identity) for identity in previous]

    identities: list[dict[str, Any]] = []
    mapping: dict[str, int] = {}

    for index, request in enumerate(requests, start=1):
        identity = {
            "userContextId": index,
            "public": True,
            "name": request.label,
            ARCZEN_KEY: request.key,
            **plan_identity(request),
        }
        identities.append(identity)
        mapping[request.key] = index

    config = {
        "version": 5,
        "lastUserContextId": len(identities),
        "identities": identities,
    }

    plan = ContainerPlan(
        mapping=mapping,
        created=[dict(identity) for identity in identities],
        purged=purged,
    )

    if dry_run:
        logger.info(
            "Dry run: would discard %d existing container(s) and write %d from Arc",
            len(purged),
            len(identities),
        )
        return plan

    for label in purged:
        logger.info("Discarding container %s", label)
    _write(profile, config, backups)
    logger.info(
        "Rebuilt containers.json: %d container(s), numbered 1-%d",
        len(identities),
        len(identities),
    )
    for request in requests:
        logger.info("  Arc %s -> container %d", request.key, mapping[request.key])
    return plan


# --------------------------------------------------------------------------
# merge: keep the user's containers, update ours
# --------------------------------------------------------------------------

def _merge_containers(
    profile: Path,
    requests: list[ContainerRequest],
    backups: BackupSet,
    dry_run: bool,
) -> ContainerPlan:
    """Add or refresh one container per Arc profile, leaving everything else."""
    config = load_config(profile)
    identities: list[dict[str, Any]] = config["identities"]

    # Lowered name -> request key, across every label arczen could have written.
    alias_index: dict[str, str] = {}
    for request in requests:
        for alias in _alias_set(request):
            alias_index.setdefault(alias, request.key)

    by_key = {
        str(identity[ARCZEN_KEY]): identity for identity in identities if _is_ours(identity)
    }
    by_alias: dict[str, dict[str, Any]] = {}
    for identity in identities:
        if _is_builtin(identity):
            continue
        name = _name_key(identity)
        if name:
            by_alias.setdefault(name, identity)

    taken = {_context_id(identity) for identity in identities if _context_id(identity)}
    next_id = max(int(config["lastUserContextId"]), max(taken, default=0))

    def allocate() -> int:
        """Next free id, stepping over every surviving container."""
        nonlocal next_id
        next_id += 1
        while next_id in taken:
            next_id += 1
        taken.add(next_id)
        return next_id

    mapping: dict[str, int] = {}
    created: list[dict[str, Any]] = []
    reused: list[dict[str, Any]] = []
    adopted: set[int] = set()

    for request in requests:
        appearance = plan_identity(request)

        existing = by_key.get(request.key)
        if existing is None:
            for alias in sorted(_alias_set(request)):
                candidate = by_alias.get(alias)
                if candidate is not None and id(candidate) not in adopted:
                    existing = candidate
                    logger.info(
                        "Adopting existing container %r for Arc %s",
                        candidate.get("name"),
                        request.key,
                    )
                    break

        if existing is not None:
            # Refresh so re-runs pick up corrected labels, colours and icons,
            # and stamp the key so the next run matches without guessing.
            existing.update(appearance)
            existing["name"] = request.label
            existing["public"] = True
            existing[ARCZEN_KEY] = request.key
            if _context_id(existing) <= 0:
                existing["userContextId"] = allocate()
            adopted.add(id(existing))
            by_key[request.key] = existing
            by_alias[request.label.strip().lower()] = existing
            mapping[request.key] = _context_id(existing)
            reused.append(dict(existing))
            continue

        identity = {
            "userContextId": allocate(),
            "public": True,
            "name": request.label,
            ARCZEN_KEY: request.key,
            **appearance,
        }
        identities.append(identity)
        adopted.add(id(identity))
        by_key[request.key] = identity
        by_alias[request.label.strip().lower()] = identity
        mapping[request.key] = identity["userContextId"]
        created.append(dict(identity))

    # Merge does not delete, so name any duplicate it had to leave behind.
    request_keys = {request.key for request in requests}
    stale: list[str] = []
    for identity in identities:
        if _is_builtin(identity) or id(identity) in adopted:
            continue
        duplicate = (
            str(identity.get(ARCZEN_KEY) or "") in request_keys
            or _name_key(identity) in alias_index
        )
        if duplicate:
            stale.append(_describe(identity))

    _renumber_collisions(identities)
    # Re-read ids after renumbering so the session points at what was written.
    for request in requests:
        identity = by_key.get(request.key)
        if identity is not None:
            mapping[request.key] = _context_id(identity)

    config["lastUserContextId"] = max(
        (_context_id(identity) for identity in identities), default=0
    )

    plan = ContainerPlan(
        mapping=mapping,
        created=created,
        reused=reused,
        stale=stale,
    )

    if dry_run:
        logger.info(
            "Dry run: %d container(s) to create, %d to refresh, %d stale",
            len(created),
            len(reused),
            len(stale),
        )
        return plan

    _write(profile, config, backups)
    logger.info(
        "Wrote containers.json (%d created, %d refreshed, %d total)",
        len(created),
        len(reused),
        len(identities),
    )
    for request in requests:
        logger.info("  Arc %s -> container %d", request.key, mapping.get(request.key, 0))
    for label in stale:
        logger.warning("Duplicate container left in place: %s", label)
    return plan


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def ensure_containers(
    profile: Path,
    requests: list[ContainerRequest],
    backups: BackupSet,
    replace: bool = False,
    dry_run: bool = False,
) -> ContainerPlan:
    """Set up one container per Arc profile.

    replace rebuilds containers.json from nothing; otherwise the user's own
    containers are preserved and only arczen's are added or refreshed.
    """
    if replace:
        return _rebuild_containers(profile, requests, backups, dry_run)
    return _merge_containers(profile, requests, backups, dry_run)
```

### `arczen/doctor.py`

```python
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
```

### `arczen/favicons.py`

```python
# arczen/favicons.py
"""Pull cached Arc favicons out of Chromium's Favicons databases."""

from __future__ import annotations

import base64
import logging
import sqlite3
from pathlib import Path

from .paths import arc_favicon_databases

logger = logging.getLogger(__name__)


def _read_only_uri(path: Path) -> str:
    return "file:" + str(path).replace("?", "%3f").replace("#", "%23").replace(" ", "%20") + "?mode=ro&immutable=1"


def _mime_type(blob: bytes) -> str:
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if blob.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if blob.startswith(b"GIF87a") or blob.startswith(b"GIF89a"):
        return "image/gif"
    if blob.startswith(b"RIFF") and blob[8:12] == b"WEBP":
        return "image/webp"
    if blob.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon"
    if blob.lstrip().startswith(b"<svg"):
        return "image/svg+xml"
    return "image/png"

def _data_uri(blob: bytes) -> str:
    return f"data:{_mime_type(blob)};base64," + base64.b64encode(blob).decode("ascii")

def _best_icon(connection: sqlite3.Connection, url: str) -> str | None:
    rows = connection.execute(
        """
        SELECT bitmaps.width, bitmaps.height, bitmaps.image_data
        FROM icon_mapping AS mapping
        JOIN favicon_bitmaps AS bitmaps ON bitmaps.icon_id = mapping.icon_id
        WHERE mapping.page_url = ?
          AND bitmaps.image_data IS NOT NULL
          AND length(bitmaps.image_data) > 0
        """,
        (url,),
    ).fetchall()

    if not rows:
        return None

    def score(row: tuple[int, int, bytes]) -> tuple[int, int]:
        width, height, blob = row
        size = width or height or 0
        return (abs(size - 32), -len(blob))

    return _data_uri(min(rows, key=score)[2])

def collect_icons(
    arc_root: Path,
    urls: set[str],
    profiles: set[str] | None = None,
) -> dict[str, str]:
    """Map URL -> data URI for every favicon Arc has cached."""
    databases = arc_favicon_databases(arc_root, profiles)
    if not databases:
        logger.info("No Arc favicon databases found; skipping favicons")
        return {}

    icons: dict[str, str] = {}
    wanted = sorted(urls)

    for database in databases:
        try:
            connection = sqlite3.connect(_read_only_uri(database), uri=True)
        except sqlite3.Error as error:
            logger.warning("Could not open %s: %s", database, error)
            continue

        try:
            for url in wanted:
                if url in icons:
                    continue
                icon = _best_icon(connection, url)
                if icon:
                    icons[url] = icon
        except sqlite3.Error as error:
            logger.warning("Favicon read failed for %s: %s", database, error)
        finally:
            connection.close()

    logger.info("Recovered %d favicon(s) from %d Arc database(s)", len(icons), len(databases))
    return icons
    """Map URL -> data URI for every favicon Arc has cached."""
    databases = arc_favicon_databases(arc_root)
    if not databases:
        logger.info("No Arc favicon databases found; skipping favicons")
        return {}

    icons: dict[str, str] = {}
    wanted = sorted(urls)

    for database in databases:
        try:
            connection = sqlite3.connect(_read_only_uri(database), uri=True)
        except sqlite3.Error as error:
            logger.warning("Could not open %s: %s", database, error)
            continue

        try:
            for url in wanted:
                if url in icons:
                    continue
                icon = _best_icon(connection, url)
                if icon:
                    icons[url] = icon
        except sqlite3.Error as error:
            logger.warning("Favicon read failed for %s: %s", database, error)
        finally:
            connection.close()

    logger.info("Recovered %d favicon(s) from %d Arc database(s)", len(icons), len(databases))
    return icons
```

### `arczen/history.py`

```python
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
```

### `arczen/icons.py`

```python
# arczen/icons.py
"""Resolve Arc space icons onto Zen's icon set and Firefox container icons.

Arc stores short icon identifiers (planet, bulb) or a raw emoji. Zen ships SVGs
across two namespaces:

  zen-icons/selectable/<name>.svg   the set Zen's workspace picker lists
  zen-icons/<name>.svg              the general UI set, which includes lightbulb

Blind interpolation into one namespace produces a dead chrome:// URL and a blank
icon, so names are resolved against a real inventory: read from the install when
it can be found, otherwise from the bundled list below.
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import re
import sys
import zipfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

ICON_BASE = "chrome://browser/skin/zen-icons"
SELECTABLE_BASE = f"{ICON_BASE}/selectable"

# Filenames live as plain ASCII in zip local headers, so a byte scan works even
# when zipfile rejects omni.ja's optimised central directory.
SELECTABLE_PATTERN = re.compile(rb"zen-icons/selectable/([A-Za-z0-9][A-Za-z0-9._-]*)\.svg")
GENERAL_PATTERN = re.compile(rb"zen-icons/([A-Za-z0-9][A-Za-z0-9._-]*)\.svg")

# Inventory taken from src/browser/themes/shared/zen-icons/jar.inc.mn.
# Used whenever the installed icon set cannot be read.
BUNDLED_SELECTABLE: tuple[str, ...] = (
    "airplane", "american-football", "baseball", "basket", "bed", "bell", "book",
    "bookmark", "briefcase", "brush", "bug", "build", "cafe", "call", "card",
    "chat", "checkbox", "circle", "cloud", "code", "coins", "construct",
    "cutlery", "egg", "extension-puzzle", "eye", "fast-food", "fish", "flag",
    "flame", "flask", "folder", "game-controller", "globe", "globe-1",
    "grid-2x2", "grid-3x3", "heart", "ice-cream", "image", "inbox", "key",
    "layers", "leaf", "lightning", "location", "lock-closed", "logo-github",
    "logo-rss", "logo-usd", "mail", "map", "megaphone", "moon", "music",
    "navigate", "nuclear", "page", "palette", "paw", "people", "pizza",
    "planet", "present", "rocket", "school", "shapes", "shirt", "skull",
    "square", "squares", "star", "star-1", "stats-chart", "sun", "tada",
    "terminal", "ticket", "time", "trash", "triangle", "video", "volume-high",
    "wallet", "warning", "water", "weight",
)

BUNDLED_GENERAL: tuple[str, ...] = (
    "algorithm", "bolt", "bookmark", "bookmark-hollow", "boost", "brackets-curly",
    "camera", "canvas", "chevron", "container-tab", "cookies-fill", "customize",
    "desktop-notification", "developer", "dice", "downloads", "duplicate-tab",
    "edit-copy", "edit-theme", "extension", "eyedropper", "face-sun", "folder",
    "forget", "fullscreen", "geo", "hammer", "heart-circle-fill", "help",
    "history", "home", "info", "library", "lightbulb", "link", "mail", "manage",
    "media-play", "menu", "microphone", "midi", "moon-stars", "move-cursor",
    "new-tab-image", "open", "page-portrait", "paintbrush", "palette",
    "passwords", "permissions", "persistent-storage", "pin", "plus", "popup",
    "print", "private-window", "privateBrowsing", "reader-mode", "reload",
    "save", "screen", "screenshot", "search-glass", "search-page", "security",
    "send-to-device", "settings", "share", "sidebar", "sliders", "sparkles",
    "spell-check", "split", "square-wand-sparkle", "tab", "text-size",
    "tool-profiler", "tracking-protection", "translations", "trash",
    "u-turn-to-left", "unpin", "video-fill", "wand-sparkle", "window", "xr",
    "zoom-out",
)

# Arc identifier modifiers that carry no meaning for icon identity.
NAME_SUFFIXES = (
    "fill", "filled", "circle", "square", "slash", "badge", "left", "right",
    "up", "down", "and", "arrow", "triangle", "rectangle", "stack", "bubble",
    "outline", "solid", "small", "large", "alt",
)

# Arc / SF Symbol identifier -> candidate Zen names, tried in order. Every
# candidate is validated against the live inventory before it is used.
NAME_HINTS: dict[str, tuple[str, ...]] = {
    "bulb": ("globe-1", "lightbulb", "lightning"),
    "lightbulb": ("globe-1", "lightbulb", "lightning"),
    "idea": ("globe-1", "lightbulb", "sparkles"),
    "planet": ("planet", "globe", "nuclear"),
    "globe": ("globe", "planet"),
    "world": ("globe", "planet"),
    "web": ("globe", "planet"),
    "graduationcap": ("school", "book"),
    "graduation": ("school", "book"),
    "school": ("school", "book"),
    "student": ("school", "book"),
    "study": ("school", "book"),
    "book": ("book", "page", "bookmark"),
    "books": ("book", "layers"),
    "library": ("book", "layers"),
    "briefcase": ("briefcase", "build"),
    "suitcase": ("briefcase", "airplane"),
    "work": ("briefcase", "build"),
    "office": ("briefcase", "build"),
    "house": ("home", "bed"),
    "home": ("home", "bed"),
    "person": ("people", "eye"),
    "people": ("people",),
    "user": ("people",),
    "profile": ("people",),
    "cart": ("basket", "card"),
    "basket": ("basket",),
    "bag": ("basket", "wallet"),
    "shopping": ("basket", "card"),
    "creditcard": ("card", "wallet", "coins"),
    "card": ("card", "wallet"),
    "wallet": ("wallet", "coins"),
    "dollarsign": ("logo-usd", "coins", "wallet"),
    "dollar": ("logo-usd", "coins"),
    "money": ("coins", "wallet", "logo-usd"),
    "bank": ("coins", "wallet"),
    "gamecontroller": ("game-controller",),
    "game": ("game-controller", "dice"),
    "controller": ("game-controller",),
    "music": ("music", "volume-high"),
    "note": ("music", "page"),
    "headphones": ("music", "volume-high"),
    "speaker": ("volume-high", "music"),
    "camera": ("image", "video"),
    "photo": ("image",),
    "image": ("image",),
    "film": ("video", "ticket"),
    "movie": ("video", "ticket"),
    "video": ("video",),
    "tv": ("video", "square"),
    "heart": ("heart",),
    "star": ("star", "star-1"),
    "flag": ("flag",),
    "bolt": ("lightning", "flame"),
    "lightning": ("lightning",),
    "flash": ("lightning",),
    "zap": ("lightning",),
    "flame": ("flame",),
    "fire": ("flame",),
    "leaf": ("leaf", "water"),
    "plant": ("leaf",),
    "nature": ("leaf", "water"),
    "pawprint": ("paw",),
    "paw": ("paw",),
    "pet": ("paw", "fish"),
    "animal": ("paw", "fish"),
    "airplane": ("airplane", "rocket"),
    "plane": ("airplane",),
    "flight": ("airplane",),
    "travel": ("airplane", "map"),
    "rocket": ("rocket",),
    "car": ("navigate", "map"),
    "envelope": ("mail", "inbox"),
    "mail": ("mail", "inbox"),
    "email": ("mail", "inbox"),
    "message": ("chat", "mail"),
    "chat": ("chat", "megaphone"),
    "calendar": ("time", "page"),
    "clock": ("time",),
    "time": ("time",),
    "timer": ("time",),
    "folder": ("folder",),
    "doc": ("page", "folder"),
    "document": ("page",),
    "file": ("page", "folder"),
    "paper": ("page",),
    "chart": ("stats-chart",),
    "graph": ("stats-chart",),
    "stats": ("stats-chart",),
    "analytics": ("stats-chart",),
    "gear": ("construct", "build"),
    "settings": ("construct", "build"),
    "cog": ("construct", "build"),
    "hammer": ("build", "construct"),
    "wrench": ("construct", "build"),
    "tools": ("construct", "build"),
    "paintbrush": ("brush", "palette"),
    "brush": ("brush", "palette"),
    "paint": ("palette", "brush"),
    "design": ("palette", "brush"),
    "palette": ("palette",),
    "pencil": ("brush", "page"),
    "terminal": ("terminal", "code"),
    "console": ("terminal", "code"),
    "code": ("code", "terminal"),
    "chevron": ("code",),
    "cube": ("squares", "layers"),
    "box": ("squares", "basket"),
    "package": ("squares", "basket"),
    "gift": ("present",),
    "present": ("present",),
    "party": ("tada", "present"),
    "cup": ("cafe",),
    "coffee": ("cafe",),
    "cafe": ("cafe",),
    "mug": ("cafe",),
    "fork": ("cutlery", "fast-food"),
    "food": ("fast-food", "cutlery"),
    "restaurant": ("cutlery", "fast-food"),
    "pizza": ("pizza", "fast-food"),
    "dumbbell": ("weight",),
    "fitness": ("weight", "american-football"),
    "gym": ("weight",),
    "workout": ("weight",),
    "sport": ("american-football", "baseball"),
    "sports": ("american-football", "baseball"),
    "football": ("american-football",),
    "baseball": ("baseball",),
    "cross": ("flask", "warning"),
    "health": ("flask", "heart"),
    "medical": ("flask",),
    "pills": ("flask",),
    "flask": ("flask",),
    "science": ("flask", "nuclear"),
    "lab": ("flask",),
    "building": ("briefcase", "squares"),
    "lock": ("lock-closed", "key"),
    "shield": ("lock-closed", "warning"),
    "secure": ("lock-closed", "key"),
    "private": ("lock-closed", "eye"),
    "key": ("key", "lock-closed"),
    "password": ("key", "lock-closed"),
    "bell": ("bell",),
    "notification": ("bell", "megaphone"),
    "alert": ("warning", "bell"),
    "warning": ("warning",),
    "bookmark": ("bookmark",),
    "tag": ("ticket", "bookmark"),
    "ticket": ("ticket",),
    "sparkle": ("sparkles", "tada"),
    "sparkles": ("sparkles", "tada"),
    "magic": ("sparkles", "wand-sparkle"),
    "ai": ("sparkles", "algorithm"),
    "brain": ("algorithm", "lightbulb"),
    "cpu": ("algorithm", "code"),
    "chip": ("algorithm",),
    "server": ("layers", "cloud"),
    "database": ("layers", "cloud"),
    "cloud": ("cloud",),
    "moon": ("moon",),
    "night": ("moon",),
    "dark": ("moon",),
    "sun": ("sun",),
    "day": ("sun",),
    "light": ("sun", "globe-1", "lightbulb"),
    "water": ("water", "fish"),
    "fish": ("fish",),
    "bug": ("bug",),
    "debug": ("bug", "code"),
    "github": ("logo-github", "code"),
    "git": ("logo-github", "code"),
    "rss": ("logo-rss",),
    "feed": ("logo-rss",),
    "news": ("logo-rss", "page"),
    "map": ("map", "navigate", "location"),
    "location": ("location", "navigate"),
    "pin": ("location", "pin"),
    "navigate": ("navigate", "map"),
    "compass": ("navigate",),
    "trash": ("trash",),
    "delete": ("trash",),
    "archivebox": ("inbox", "layers"),
    "tray": ("inbox",),
    "inbox": ("inbox",),
    "link": ("link",),
    "magnifyingglass": ("search-glass", "eye"),
    "search": ("search-glass",),
    "eye": ("eye",),
    "shirt": ("shirt",),
    "clothes": ("shirt",),
    "bed": ("bed", "home"),
    "sleep": ("bed", "moon"),
    "skull": ("skull",),
    "egg": ("egg",),
    "grid": ("grid-2x2", "grid-3x3", "squares"),
    "layers": ("layers",),
    "shapes": ("shapes", "triangle"),
    "circle": ("circle",),
    "square": ("square", "squares"),
    "triangle": ("triangle",),
    "checkbox": ("checkbox",),
    "check": ("checkbox",),
    "call": ("call", "chat"),
    "phone": ("call",),
    "megaphone": ("megaphone",),
    "extension": ("extension-puzzle",),
    "puzzle": ("extension-puzzle",),
    "nuclear": ("nuclear",),
    "atom": ("nuclear", "algorithm"),
}

# Firefox container icons are a closed set.
CONTAINER_ICON_SET = (
    "fingerprint", "briefcase", "dollar", "cart", "vacation", "gift",
    "food", "fruit", "pet", "tree", "chill", "circle", "fence",
)

CONTAINER_ICON_HINTS: dict[str, tuple[str, ...]] = {
    "briefcase": (
        "briefcase", "work", "building", "office", "doc", "document", "folder",
        "chart", "stats", "terminal", "code", "cpu", "server", "graduationcap",
        "graduation", "school", "student", "study", "book", "books", "library",
        "pencil", "bulb", "lightbulb", "idea", "brain", "flask", "science",
        "lab", "algorithm",
    ),
    "dollar": ("dollar", "dollarsign", "bank", "creditcard", "card", "money",
               "finance", "wallet", "coins"),
    "cart": ("cart", "bag", "basket", "shopping", "shirt", "clothes"),
    "vacation": ("airplane", "plane", "flight", "beach", "vacation", "travel",
                 "sun", "map", "location", "navigate", "compass", "planet",
                 "globe", "world", "rocket"),
    "gift": ("gift", "present", "heart", "star", "sparkle", "sparkles", "party",
             "tada", "ticket"),
    "food": ("fork", "cup", "coffee", "cafe", "food", "restaurant", "pizza",
             "cutlery", "egg"),
    "fruit": ("fruit", "apple"),
    "pet": ("pawprint", "paw", "pet", "dog", "cat", "animal", "fish"),
    "tree": ("tree", "leaf", "plant", "nature", "water"),
    "chill": ("moon", "snow", "chill", "music", "game", "gamecontroller", "film",
              "movie", "video", "tv", "bed", "sleep", "night", "headphones"),
    "fence": ("house", "home", "fence", "lock", "shield", "key", "secure",
              "private", "password"),
    "fingerprint": ("person", "people", "fingerprint", "profile", "account",
                    "user", "eye"),
}

EMOJI_CONTAINER_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("\U0001F4BC", "\U0001F3E2", "\U0001F5C2", "\U0001F4CA", "\U0001F4BB"), "briefcase"),
    (("\U0001F393", "\U0001F4DA", "\U0001F4D6", "\u270F", "\U0001F4A1", "\U0001F9E0"), "briefcase"),
    (("\U0001F4B0", "\U0001F4B5", "\U0001F3E6", "\U0001F4B3"), "dollar"),
    (("\U0001F6D2", "\U0001F6CD", "\U0001F455"), "cart"),
    (("\u2708", "\U0001F3D6", "\U0001F5FA", "\U0001F30D", "\U0001FA90", "\U0001F680"), "vacation"),
    (("\U0001F381", "\U0001F389", "\u2764", "\u2B50", "\u2728"), "gift"),
    (("\U0001F374", "\u2615", "\U0001F355", "\U0001F354"), "food"),
    (("\U0001F34E", "\U0001F34A", "\U0001F347"), "fruit"),
    (("\U0001F43E", "\U0001F436", "\U0001F431", "\U0001F41F"), "pet"),
    (("\U0001F333", "\U0001F331", "\U0001F343"), "tree"),
    (("\u2744", "\U0001F319", "\U0001F3B5", "\U0001F3AE", "\U0001F3AC", "\U0001F4FA"), "chill"),
    (("\U0001F3E0", "\U0001F512", "\U0001F6E1", "\U0001F511"), "fence"),
]


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class IconSet:
    """What icons Zen actually has, and where that was learned from."""

    selectable: tuple[str, ...] = ()
    general: tuple[str, ...] = ()
    source: str = "none"
    paths_tried: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.selectable or self.general)

    def names(self, namespace: str) -> tuple[str, ...]:
        return self.selectable if namespace == "selectable" else self.general

    def url(self, name: str, namespace: str) -> str:
        base = SELECTABLE_BASE if namespace == "selectable" else ICON_BASE
        return f"{base}/{name}.svg"


@dataclass
class IconResolution:
    """The outcome of resolving one Arc space icon."""

    value: str | None = None          # what goes into the Zen workspace
    kind: str = "none"                # emoji | selectable | general | override | none
    namespace: str | None = None
    source_emoji: str | None = None
    source_name: str | None = None
    matched_name: str | None = None
    method: str = "none"
    candidates: list[str] = field(default_factory=list)


def zen_install_candidates() -> list[Path]:
    """Plausible Zen resource roots, most likely first."""
    roots: list[Path] = []

    if sys.platform == "darwin":
        bundles: list[Path] = []
        for folder in (Path("/Applications"), Path.home() / "Applications"):
            if not folder.is_dir():
                continue
            for entry in sorted(folder.glob("*.app")):
                if "zen" in entry.name.lower():
                    bundles.append(entry)
        # Named bundles first, then anything else that looks like Zen.
        for name in ("Zen.app", "Zen Browser.app", "Zen Twilight.app"):
            for folder in (Path("/Applications"), Path.home() / "Applications"):
                candidate = folder / name
                if candidate.is_dir() and candidate not in bundles:
                    bundles.insert(0, candidate)
        roots.extend(bundle / "Contents" / "Resources" for bundle in bundles)

    elif sys.platform == "win32":
        local = Path.home() / "AppData" / "Local"
        roots.extend(
            [
                Path("C:/Program Files/Zen Browser"),
                Path("C:/Program Files/Zen"),
                Path("C:/Program Files (x86)/Zen Browser"),
                local / "Zen Browser",
                local / "zen",
            ]
        )

    else:
        roots.extend(
            [
                Path("/usr/lib/zen"),
                Path("/usr/lib/zen-browser"),
                Path("/usr/lib64/zen"),
                Path("/opt/zen"),
                Path("/opt/zen-browser"),
                Path("/usr/share/zen"),
                Path("/var/lib/flatpak/app/app.zen_browser.zen/current/active/files/lib/zen"),
                Path.home() / ".var" / "app" / "app.zen_browser.zen" / "current" / "active" / "files" / "lib" / "zen",
                Path.home() / ".local" / "share" / "zen",
            ]
        )

    return roots


def _normalise_root(path: Path) -> list[Path]:
    """Accept an .app bundle, a Resources dir, or any dir holding omni.ja."""
    options = [path]
    if path.suffix == ".app":
        options.append(path / "Contents" / "Resources")
    options.append(path / "browser")
    return options


def _names_from_bytes(blob: bytes) -> tuple[set[str], set[str]]:
    selectable = {match.decode("ascii") for match in SELECTABLE_PATTERN.findall(blob)}
    general = {
        match.decode("ascii")
        for match in GENERAL_PATTERN.findall(blob)
        if match.decode("ascii") not in selectable
    }
    # Strip the subdirectory sets that share the zen-icons prefix.
    general.discard("library")
    return selectable, general


def _names_from_archive(archive: Path) -> tuple[set[str], set[str]]:
    """Read icon names from omni.ja.

    zipfile is tried first, then a raw byte scan. Mozilla relocates omni.ja's
    central directory, which zipfile often rejects outright, but the filenames
    sit uncompressed in the local file headers either way.
    """
    try:
        with zipfile.ZipFile(archive) as bundle:
            entries = "\n".join(bundle.namelist()).encode("utf-8")
        selectable, general = _names_from_bytes(entries)
        if selectable or general:
            return selectable, general
    except (OSError, zipfile.BadZipFile) as error:
        logger.debug("zipfile could not read %s (%s); falling back to a byte scan", archive, error)

    try:
        return _names_from_bytes(archive.read_bytes())
    except OSError as error:
        logger.debug("Could not read %s: %s", archive, error)
        return set(), set()


def _names_from_tree(root: Path) -> tuple[set[str], set[str]]:
    base = root / "chrome" / "browser" / "skin" / "classic" / "browser" / "zen-icons"
    if not base.is_dir():
        base = root / "chrome" / "browser" / "skin" / "zen-icons"
    if not base.is_dir():
        return set(), set()

    selectable = {path.stem for path in (base / "selectable").glob("*.svg")}
    general = {path.stem for path in base.glob("*.svg")}
    return selectable, general


@lru_cache(maxsize=8)
def discover_zen_icons(install_hint: str | None = None) -> IconSet:
    """Inventory Zen's icons, falling back to the bundled list from jar.inc.mn."""
    roots: list[Path] = []
    if install_hint:
        roots.extend(_normalise_root(Path(install_hint).expanduser()))
    for candidate in zen_install_candidates():
        roots.extend(_normalise_root(candidate))

    tried: list[str] = []
    selectable: set[str] = set()
    general: set[str] = set()
    found_at: Path | None = None

    for root in roots:
        if not root.exists():
            continue

        for archive in (root / "omni.ja", root / "browser" / "omni.ja"):
            if not archive.is_file():
                continue
            tried.append(str(archive))
            archive_selectable, archive_general = _names_from_archive(archive)
            if archive_selectable or archive_general:
                selectable |= archive_selectable
                general |= archive_general
                found_at = archive

        tried.append(str(root))
        tree_selectable, tree_general = _names_from_tree(root)
        selectable |= tree_selectable
        general |= tree_general
        if (tree_selectable or tree_general) and found_at is None:
            found_at = root

        if selectable:
            break

    if selectable or general:
        source = f"install ({found_at})" if found_at else "install"
        if not selectable:
            selectable = set(BUNDLED_SELECTABLE)
            source += " + bundled selectable list"
        if not general:
            general = set(BUNDLED_GENERAL)
            source += " + bundled general list"
        logger.info(
            "Zen icon set: %d selectable, %d general, from %s",
            len(selectable),
            len(general),
            source,
        )
    else:
        selectable = set(BUNDLED_SELECTABLE)
        general = set(BUNDLED_GENERAL)
        source = "bundled list (install not found)"
        logger.info(
            "Could not read Zen's installed icon set; using the bundled list "
            "(%d selectable, %d general). Pass --zen-install to read the real one.",
            len(selectable),
            len(general),
        )

    return IconSet(
        selectable=tuple(sorted(selectable)),
        general=tuple(sorted(general)),
        source=source,
        paths_tried=tuple(tried),
    )


# --------------------------------------------------------------------------
# Name matching
# --------------------------------------------------------------------------

def _normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _tokens(identifier: str) -> list[str]:
    """Split an Arc identifier into meaningful parts, dropping modifiers."""
    parts = [part for part in re.split(r"[.\-_\s]", identifier.lower()) if part]
    kept = [part for part in parts if part not in NAME_SUFFIXES]
    return kept or parts


def match_icon_name(
    identifier: str,
    icon_set: IconSet,
) -> tuple[str | None, str | None, str, list[str]]:
    """Map an Arc icon identifier onto a Zen icon.

    The selectable namespace is preferred because it is what Zen's picker lists.
    Returns (name, namespace, method, near_misses).
    """
    if not icon_set:
        return None, None, "no-icon-set", []

    lookups = {
        "selectable": {_normalise(name): name for name in icon_set.selectable},
        "general": {_normalise(name): name for name in icon_set.general},
    }
    order = ("selectable", "general")

    # 1. Exact match after normalising punctuation on both sides.
    key = _normalise(identifier)
    for namespace in order:
        hit = lookups[namespace].get(key)
        if hit:
            return hit, namespace, "exact", []

    tokens = _tokens(identifier)

    # 2. Match with modifiers stripped.
    stripped = _normalise("".join(tokens))
    for namespace in order:
        hit = lookups[namespace].get(stripped)
        if hit:
            return hit, namespace, "modifier-stripped", []

    # 3. Curated hints, token by token.
    for token in tokens:
        for hint in NAME_HINTS.get(token, ()):
            hint_key = _normalise(hint)
            for namespace in order:
                hit = lookups[namespace].get(hint_key)
                if hit:
                    return hit, namespace, f"hint:{token}->{hint}", []

    # 4. Substring containment, long tokens only.
    for token in tokens:
        token_key = _normalise(token)
        if len(token_key) < 4:
            continue
        for namespace in order:
            for normalised, original in sorted(lookups[namespace].items()):
                if token_key in normalised or normalised in token_key:
                    return original, namespace, f"substring:{token}", []

    # 5. Fuzzy, with a high cutoff so wrong icons are not invented.
    for namespace in order:
        pool = list(lookups[namespace])
        close = difflib.get_close_matches(key, pool, n=3, cutoff=0.75)
        if close:
            return (
                lookups[namespace][close[0]],
                namespace,
                "fuzzy",
                [lookups[namespace][name] for name in close[1:]],
            )

    near: list[str] = []
    for namespace in order:
        pool = list(lookups[namespace])
        near.extend(
            lookups[namespace][name]
            for name in difflib.get_close_matches(key, pool, n=4, cutoff=0.4)
        )
    return None, None, "unmatched", near[:6]


def resolve_space_icon(
    emoji: str | None,
    identifier: str | None,
    icon_set: IconSet,
    override: str | None = None,
) -> IconResolution:
    """Decide what icon value to write for one Arc space."""
    if override:
        if override.startswith(("chrome://", "resource://", "data:")):
            value, namespace, name = override, None, override
        else:
            raw = override.strip()
            namespace = "selectable"
            if raw.startswith("selectable/"):
                name = raw.split("/", 1)[1]
            elif raw.startswith("general/"):
                name, namespace = raw.split("/", 1)[1], "general"
            else:
                name = raw
                # Honour whichever namespace actually holds the name.
                if name not in icon_set.selectable and name in icon_set.general:
                    namespace = "general"
            value = icon_set.url(name, namespace)

        return IconResolution(
            value=value,
            kind="override",
            namespace=namespace,
            source_emoji=emoji,
            source_name=identifier,
            matched_name=name,
            method="user-override",
        )

    if identifier:
        name, namespace, method, near = match_icon_name(identifier, icon_set)
        if name and namespace:
            return IconResolution(
                value=icon_set.url(name, namespace),
                kind=namespace,
                namespace=namespace,
                source_emoji=emoji,
                source_name=identifier,
                matched_name=name,
                method=method,
                candidates=near,
            )

        # Nothing matched. An emoji renders; a dead chrome:// URL does not.
        if emoji:
            return IconResolution(
                value=emoji,
                kind="emoji",
                source_emoji=emoji,
                source_name=identifier,
                method=f"{method}-fell-back-to-emoji",
                candidates=near,
            )

        return IconResolution(
            kind="none",
            source_emoji=emoji,
            source_name=identifier,
            method=method,
            candidates=near,
        )

    if emoji:
        return IconResolution(value=emoji, kind="emoji", source_emoji=emoji, method="emoji")

    return IconResolution(kind="none", method="no-arc-icon")


# --------------------------------------------------------------------------
# Firefox container icons
# --------------------------------------------------------------------------

def container_icon_for(emoji: str | None, identifier: str | None) -> str:
    """Closest Firefox container icon for an Arc space icon."""
    if identifier:
        tokens = _tokens(identifier)
        for icon, hints in CONTAINER_ICON_HINTS.items():
            if any(token in hints for token in tokens):
                return icon
        normalised = _normalise(identifier)
        for icon in CONTAINER_ICON_SET:
            if icon in normalised:
                return icon

    if emoji:
        for emojis, icon in EMOJI_CONTAINER_HINTS:
            if any(character in emoji for character in emojis):
                return icon

    return "circle"


def container_icon_from_seed(seed: str) -> str:
    """Stable container icon when an Arc space has no icon at all."""
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return CONTAINER_ICON_SET[digest[0] % len(CONTAINER_ICON_SET)]
```

### `arczen/inspect.py`

```python
# arczen/inspect.py
"""Diagnostics for icon and colour resolution, without touching any profile."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .arcdata import parse_sidebar
from .containers import ContainerRequest, plan_identity
from .icons import discover_zen_icons, match_icon_name, resolve_space_icon
from .paths import resolve_arc_root
from .themes import read_colors

logger = logging.getLogger(__name__)

RULE = "-" * 72


def _print_columns(names: tuple[str, ...], width_limit: int = 100) -> None:
    if not names:
        print("  (none)")
        return
    width = max(len(name) for name in names) + 2
    columns = max(1, width_limit // width)
    for index, name in enumerate(names):
        end = "\n" if (index + 1) % columns == 0 else ""
        print("  " + name.ljust(width), end=end)
    if len(names) % columns:
        print()


def list_icons(zen_install: str | None = None) -> int:
    icon_set = discover_zen_icons(zen_install)

    print(f"Icon set source : {icon_set.source}")
    if icon_set.paths_tried:
        print("Paths examined  :")
        for path in icon_set.paths_tried:
            print(f"  {path}")
    print()

    print(f"selectable/ ({len(icon_set.selectable)}) "
          "— the set Zen's workspace icon picker lists:")
    _print_columns(icon_set.selectable)

    print(f"\ngeneral ({len(icon_set.general)}) "
          "— renders correctly but is not shown in the picker:")
    _print_columns(icon_set.general)

    print(
        '\nOverride with --space-icon "Space Name=planet", or force a namespace '
        'with "Space Name=general/lightbulb".'
    )
    return 0


def icon_candidates(identifier: str, zen_install: str | None = None) -> int:
    icon_set = discover_zen_icons(zen_install)
    name, namespace, method, near = match_icon_name(identifier, icon_set)

    print(f"Arc identifier : {identifier}")
    print(f"Icon set       : {icon_set.source}")
    print(f"Resolved to    : {name or '(no match)'}")
    print(f"Namespace      : {namespace or '(none)'}")
    print(f"Method         : {method}")
    if name and namespace:
        print(f"URL            : {icon_set.url(name, namespace)}")
    if near:
        print(f"Near misses    : {', '.join(near)}")
    if not name:
        print(
            "\nNo confident match. Pick a name from 'arczen inspect --list-icons' and pass\n"
            '  --space-icon "<Space Name>=<icon-name>"'
        )
    return 0


def inspect_spaces(
    arc_profile: str | Path | None = None,
    zen_install: str | None = None,
    overrides: dict[str, str] | None = None,
) -> int:
    arc_root = resolve_arc_root(arc_profile)
    sidebar = parse_sidebar(arc_root)
    icon_set = discover_zen_icons(zen_install)
    overrides = {key.lower(): value for key, value in (overrides or {}).items()}

    print(f"\nArc data folder : {arc_root}")
    print(f"Icon set source : {icon_set.source}")
    print(f"Icon set size   : {len(icon_set.selectable)} selectable, "
          f"{len(icon_set.general)} general")
    print(f"Spaces          : {len(sidebar.spaces)}")
    essentials = ", ".join(
        f"{profile}={len(tabs)}" for profile, tabs in sidebar.essentials.items()
    )
    print(f"Essentials      : {essentials or 'none'}")

    unresolved: list[tuple[str, str, list[str]]] = []

    for space in sidebar.spaces:
        print(f"\n{RULE}")
        print(f"Space    : {space.name}")
        print(f"Profile  : {space.profile}")
        print(f"Space id : {space.space_id}")

        print("\n  Raw Arc iconType:")
        blob = json.dumps(space.icon_type_raw, indent=4, ensure_ascii=False) or "{}"
        for line in blob.splitlines():
            print(f"    {line}")

        print(f"\n  Parsed emoji      : {space.icon_emoji or '(none)'}")
        print(f"  Parsed identifier : {space.icon_name or '(none)'}")

        resolution = resolve_space_icon(
            space.icon_emoji,
            space.icon_name,
            icon_set,
            overrides.get(space.name.lower()),
        )
        print(f"  Resolved icon     : {resolution.value or '(none)'}")
        print(f"  Namespace         : {resolution.namespace or '(none)'}")
        print(f"  Resolution kind   : {resolution.kind}")
        print(f"  Resolution method : {resolution.method}")
        if resolution.candidates:
            print(f"  Near misses       : {', '.join(resolution.candidates)}")
        if space.icon_name and resolution.kind not in ("selectable", "general", "override"):
            unresolved.append((space.name, space.icon_name, resolution.candidates))

        reading = read_colors(space.window_theme)
        print(f"\n  Colour strategy   : {reading.strategy}")
        print(f"  Colours (RGB)     : {reading.colors or '(none)'}")
        if not reading.colors:
            keys = sorted(space.window_theme) if isinstance(space.window_theme, dict) else []
            print(f"  windowTheme keys  : {keys or '(theme is empty in Arc)'}")

        appearance = plan_identity(
            ContainerRequest(
                key=space.space_id,
                label=space.name,
                window_theme=space.window_theme,
                icon_emoji=space.icon_emoji,
                icon_name=space.icon_name,
            )
        )
        print(f"  Container icon    : {appearance['icon']}")
        print(f"  Container colour  : {appearance['color']}")

        pinned = sum(1 for tab in space.tabs if tab.pinned)
        print(f"\n  Pinned tabs       : {pinned}")
        print(f"  Open tabs         : {len(space.tabs) - pinned}")
        print(f"  Folders           : {len(space.folders)}")
        print(f"  Split views       : {len(space.splits)}")
        for split in space.splits:
            panes = " + ".join(member.title for member in split.members)
            state = "pinned" if split.pinned else "open"
            print(f"    - {split.orientation}, {state}: {panes}")

    print(f"\n{RULE}")
    if unresolved:
        print("\nIcons that did not resolve to a Zen icon:")
        for name, identifier, near in unresolved:
            suffix = f" — closest: {', '.join(near)}" if near else ""
            print(f"  - {name} (Arc icon: {identifier}){suffix}")
        print(
            "\nFind a name with 'arczen inspect --list-icons', then override it:\n"
            '  arczen migrate --space-icon "Space Name=icon-name"'
        )
    else:
        print("\nAll Arc space icons resolved.")

    return 0
```

### `arczen/migrate.py`

```python
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
    write_session(zen_profile, session, backups, nuke=options.replace, dry_run=options.dry_run)

    result.workspaces = len(session["spaces"])
    result.tabs = sum(1 for tab in session["tabs"] if not tab.get("zenIsEmpty"))
    result.folders = len(session["folders"])

    # Arc's split views: the panes migrate as ordinary tabs in the right place,
    # but the side-by-side layout is not reproduced, so each one is named.
    if options.copies("tabs") or options.copies("temporary-tabs"):
        for space in sidebar.spaces:
            for split in space.splits:
                if split.pinned and not options.copies("tabs"):
                    continue
                if not split.pinned and not options.copies("temporary-tabs"):
                    continue
                result.splits.append(
                    f"{space.name}: "
                    + " + ".join(member.title for member in split.members)
                )
    if result.splits:
        result.warnings.append(
            f"{len(result.splits)} Arc split view(s) arrived as separate tabs; Zen's "
            "split layout is not written yet. Re-split them in Zen:\n      "
            + "\n      ".join(result.splits)
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
```

### `arczen/mozlz4.py`

```python
# arczen/mozlz4.py
"""mozLz4 container format used by Zen and Firefox session files."""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

MAGIC = b"mozLz40\x00"


def read_json(path: str | Path) -> dict[str, Any]:
    """Decompress a mozLz4 file and parse the JSON payload."""
    import lz4.block

    raw = Path(path).read_bytes()
    if raw[:8] != MAGIC:
        raise ValueError(f"Not a mozLz4 file: {path}")

    size = struct.unpack("<I", raw[8:12])[0]
    payload = lz4.block.decompress(raw[12:], uncompressed_size=size)
    return json.loads(payload)


def write_json(path: str | Path, data: dict[str, Any]) -> None:
    """Serialise and write a dict as a mozLz4 file."""
    import lz4.block

    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    compressed = lz4.block.compress(payload, store_size=False)
    Path(path).write_bytes(MAGIC + struct.pack("<I", len(payload)) + compressed)
```

### `arczen/passwords.py`

```python
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
```

### `arczen/paths.py`

```python
# arczen/paths.py
"""Arc and Zen location discovery, plus the backup bookkeeping."""

from __future__ import annotations

import configparser
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


# --------------------------------------------------------------------------
# Arc
# --------------------------------------------------------------------------

def arc_candidate_roots() -> list[Path]:
    if sys.platform == "darwin":
        return [Path.home() / "Library" / "Application Support" / "Arc"]

    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        packages = local / "Packages"
        roots = [packages / "TheBrowserCompany.Arc_ttt1ap7aakyb4" / "LocalCache" / "Local" / "Arc"]
        for match in sorted(packages.glob("TheBrowserCompany.Arc_*")):
            candidate = match / "LocalCache" / "Local" / "Arc"
            if candidate not in roots:
                roots.append(candidate)
        return roots

    return []


def is_arc_root(path: Path) -> bool:
    sidebar = path / "StorableSidebar.json"
    if not sidebar.is_file():
        return False
    try:
        with open(sidebar, encoding="utf-8") as handle:
            json.load(handle)
        return True
    except (OSError, json.JSONDecodeError):
        return False


def discover_arc_root() -> Path | None:
    for candidate in arc_candidate_roots():
        resolved = candidate.expanduser()
        if resolved.is_dir() and is_arc_root(resolved):
            return resolved
    return None


def resolve_arc_root(explicit: str | Path | None = None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if not is_arc_root(path):
            raise FileNotFoundError(f"No valid StorableSidebar.json under: {path}")
        return path

    found = discover_arc_root()
    if not found:
        raise FileNotFoundError(
            "Could not locate an Arc data folder. Pass --arc-profile with the directory "
            "that contains StorableSidebar.json."
        )
    return found


def arc_user_data(arc_root: Path) -> Path:
    return arc_root / "User Data"


def arc_chromium_profiles(arc_root: Path) -> list[Path]:
    """Chromium profile directories inside Arc's User Data folder."""
    user_data = arc_user_data(arc_root)
    if not user_data.is_dir():
        return []

    profiles = []
    default = user_data / "Default"
    if default.is_dir():
        profiles.append(default)
    profiles.extend(sorted(user_data.glob("Profile *")))
    return [path for path in profiles if path.is_dir()]


def arc_profile_names(arc_root: Path) -> dict[str, str]:
    """Arc profile folder name -> the name shown for it inside Arc.

    Read from profile.info_cache in User Data/Local State, which is what Arc
    updates when a profile is renamed. The per-profile Preferences file holds a
    second copy that is not rewritten on rename, so it can sit on Chromium's
    stock "Person 1" forever; it is only a fallback here.

    The one exception: if Local State marks a name as still being the stock
    default and Preferences holds something different, Preferences wins.
    """
    names: dict[str, str] = {}
    stock: set[str] = set()

    state = arc_user_data(arc_root) / "Local State"
    if state.is_file():
        try:
            with open(state, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError, ValueError):
            data = {}
        if isinstance(data, dict):
            cache = (data.get("profile") or {}).get("info_cache") or {}
            if isinstance(cache, dict):
                for directory, info in cache.items():
                    if not isinstance(info, dict):
                        continue
                    name = info.get("name")
                    if isinstance(name, str) and name.strip():
                        names[str(directory)] = name.strip()
                        if info.get("is_using_default_name"):
                            stock.add(str(directory))

    for path in arc_chromium_profiles(arc_root):
        if path.name in names and path.name not in stock:
            continue

        prefs = path / "Preferences"
        if not prefs.is_file():
            continue
        try:
            with open(prefs, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue

        profile = data.get("profile")
        name = profile.get("name") if isinstance(profile, dict) else None
        if isinstance(name, str) and name.strip():
            names[path.name] = name.strip()

    return names


def is_sqlite(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def arc_favicon_databases(arc_root: Path, profiles: set[str] | None = None) -> list[Path]:
    """Favicons databases, optionally limited to named Arc profile folders."""
    return [
        profile / "Favicons"
        for profile in arc_chromium_profiles(arc_root)
        if (profiles is None or profile.name in profiles)
        and is_sqlite(profile / "Favicons")
    ]


def arc_history_databases(arc_root: Path, profiles: set[str] | None = None) -> list[Path]:
    """History databases, optionally limited to named Arc profile folders."""
    return [
        profile / "History"
        for profile in arc_chromium_profiles(arc_root)
        if (profiles is None or profile.name in profiles)
        and is_sqlite(profile / "History")
    ]


def arc_login_databases(arc_root: Path, profiles: set[str] | None = None) -> list[Path]:
    """Login Data databases, optionally limited to named Arc profile folders."""
    return [
        profile / "Login Data"
        for profile in arc_chromium_profiles(arc_root)
        if (profiles is None or profile.name in profiles)
        and is_sqlite(profile / "Login Data")
    ]

# --------------------------------------------------------------------------
# Zen
# --------------------------------------------------------------------------

def zen_candidate_roots() -> list[Path]:
    roots: list[Path] = []
    if sys.platform == "darwin":
        roots.append(Path.home() / "Library" / "Application Support" / "zen")
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        roots.append(Path(appdata) / "zen" if appdata else Path.home() / "AppData" / "Roaming" / "zen")
    else:
        roots.append(Path.home() / ".zen")
        flatpak = Path.home() / ".var" / "app" / "app.zen_browser.zen"
        roots.extend([flatpak / "zen", flatpak / ".zen", flatpak / "config" / "zen"])
    return roots


def is_zen_profile(path: Path) -> bool:
    return path.is_dir() and (path / "prefs.js").is_file()


def _profiles_from_ini(zen_root: Path) -> list[Path]:
    profiles: list[Path] = []
    ini = zen_root / "profiles.ini"
    if not ini.is_file():
        return profiles

    parser = configparser.ConfigParser()
    parser.read(ini, encoding="utf-8")
    for section in parser.sections():
        if not section.startswith("Profile"):
            continue
        raw = parser.get(section, "Path", fallback=None)
        if not raw:
            continue
        relative = parser.get(section, "IsRelative", fallback="1") == "1"
        candidate = zen_root / raw if relative else Path(raw)
        if candidate.is_dir():
            profiles.append(candidate)
    return profiles


def _default_from_ini(zen_root: Path) -> Path | None:
    installs = zen_root / "installs.ini"
    if installs.is_file():
        parser = configparser.ConfigParser()
        parser.read(installs, encoding="utf-8")
        for section in parser.sections():
            raw = parser.get(section, "Default", fallback=None)
            if raw:
                candidate = Path(raw) if Path(raw).is_absolute() else zen_root / raw
                if candidate.is_dir():
                    return candidate

    ini = zen_root / "profiles.ini"
    if ini.is_file():
        parser = configparser.ConfigParser()
        parser.read(ini, encoding="utf-8")
        for section in parser.sections():
            if not section.startswith("Profile"):
                continue
            if parser.get(section, "Default", fallback="0") != "1":
                continue
            raw = parser.get(section, "Path", fallback=None)
            if not raw:
                continue
            relative = parser.get(section, "IsRelative", fallback="1") == "1"
            candidate = zen_root / raw if relative else Path(raw)
            if candidate.is_dir():
                return candidate
    return None


def zen_profiles_in_root(zen_root: Path) -> list[Path]:
    found: list[Path] = []
    if is_zen_profile(zen_root):
        found.append(zen_root)
    found.extend(path for path in _profiles_from_ini(zen_root) if is_zen_profile(path))

    container = zen_root / "Profiles"
    if container.is_dir():
        found.extend(path for path in sorted(container.iterdir()) if is_zen_profile(path))

    unique: list[Path] = []
    seen = set()
    for path in found:
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def discover_zen_profiles() -> list[Path]:
    profiles: list[Path] = []
    for root in zen_candidate_roots():
        if root.exists():
            profiles.extend(zen_profiles_in_root(root))
    return profiles


def resolve_zen_profile(explicit: str | Path | None = None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_dir():
            raise FileNotFoundError(f"Zen path does not exist: {path}")
        if is_zen_profile(path):
            return path

        default = _default_from_ini(path)
        if default and is_zen_profile(default):
            return default

        profiles = zen_profiles_in_root(path)
        if profiles:
            return profiles[0]
        raise FileNotFoundError(f"No Zen profile found under: {path}")

    for root in zen_candidate_roots():
        if not root.exists():
            continue
        default = _default_from_ini(root)
        if default and is_zen_profile(default):
            return default
        profiles = zen_profiles_in_root(root)
        if profiles:
            return profiles[0]

    raise FileNotFoundError(
        "Could not locate a Zen profile. Launch Zen once, quit it, then pass --zen-profile."
    )


def zen_session_files(profile: Path) -> dict[str, Path]:
    return {
        "zen_sessions": profile / "zen-sessions.jsonlz4",
        "sessionstore": profile / "sessionstore.jsonlz4",
        "recovery": profile / "sessionstore-backups" / "recovery.jsonlz4",
    }


# --------------------------------------------------------------------------
# Backups
# --------------------------------------------------------------------------

@dataclass
class BackupSet:
    """Copies every touched file into one timestamped folder before changes."""

    root: Path
    enabled: bool = True
    saved: list[Path] = field(default_factory=list)

    @classmethod
    def for_profile(cls, profile: Path, enabled: bool = True) -> "BackupSet":
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return cls(root=profile / f"arczen-backup-{stamp}", enabled=enabled)

    def save(self, path: Path) -> None:
        if not self.enabled or not path.exists():
            return
        if path in self.saved:
            return

        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / path.name
        counter = 1
        while target.exists():
            target = self.root / f"{path.stem}.{counter}{path.suffix}"
            counter += 1

        shutil.copy2(path, target)
        self.saved.append(path)
```

### `arczen/prefs.py`

```python
# arczen/prefs.py
"""Targeted prefs.js rewriting (Zen must be closed)."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from .paths import BackupSet

logger = logging.getLogger(__name__)

# One user_pref line: name in group 1, raw value text in group 2.
PREF_LINE = re.compile(r'^\s*user_pref\(\s*"([^"]+)"\s*,\s*(.+?)\s*\)\s*;\s*$')


def _format(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _read_prefs(path: Path) -> dict[str, str]:
    """Every user_pref currently in the file, as name -> raw value text."""
    if not path.is_file():
        return {}
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return {}

    found: dict[str, str] = {}
    for line in content.splitlines():
        match = PREF_LINE.match(line)
        if match:
            found[match.group(1)] = match.group(2)
    return found


def apply_prefs(
    profile: Path,
    values: dict[str, Any],
    backups: BackupSet,
    dry_run: bool = False,
) -> list[str]:
    """Replace or append user_pref lines, then confirm they landed.

    Returns the names of any preference missing or holding the wrong value when
    the file is read back. Zen rewrites prefs.js from its in-memory copy when it
    exits, so a pref written while Zen is open is discarded without complaint -
    that read-back is the only way to catch it.
    """
    path = profile / "prefs.js"
    original = path.read_text(encoding="utf-8") if path.is_file() else ""

    kept = []
    for line in original.splitlines():
        match = PREF_LINE.match(line)
        if match and match.group(1) in values:
            continue
        kept.append(line)

    while kept and not kept[-1].strip():
        kept.pop()

    for key, value in values.items():
        kept.append(f'user_pref("{key}", {_format(value)});')

    content = "\n".join(kept) + "\n"

    if dry_run:
        logger.info("Dry run: would set %d preference(s)", len(values))
        for key, value in values.items():
            logger.info("   %s = %s", key, _format(value))
        return []

    backups.save(path)
    path.write_text(content, encoding="utf-8")
    logger.info("Updated %d preference(s) in prefs.js", len(values))

    present = _read_prefs(path)
    unconfirmed = [
        key for key, value in values.items() if present.get(key) != _format(value)
    ]
    for key in unconfirmed:
        logger.warning(
            "Preference %s did not survive the write (found %s, wanted %s)",
            key,
            present.get(key, "nothing"),
            _format(values[key]),
        )
    return unconfirmed
```

### `arczen/privacy.py`

```python
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
```

### `arczen/themes.py`

```python
# arczen/themes.py
"""Arc space colour extraction and Zen/Firefox colour conversion.

Arc's windowTheme layout varies between builds, so colours are looked up by
known paths first and then by a recursive sweep, with the winning strategy
recorded for the inspect command.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

ZEN_COLOR_TYPE = "explicit-lightness"
DEFAULT_OPACITY = 0.5

# Firefox contextual identity palette. These are the only colour names a
# container accepts, so Arc colours get snapped to the nearest one.
CONTAINER_COLORS: dict[str, tuple[int, int, int]] = {
    "blue": (0x37, 0x9F, 0xFF),
    "turquoise": (0x00, 0xC7, 0x9A),
    "green": (0x51, 0xCD, 0x00),
    "yellow": (0xFF, 0xCB, 0x00),
    "orange": (0xFF, 0x9F, 0x00),
    "red": (0xFF, 0x61, 0x3D),
    "pink": (0xFF, 0x4B, 0xDA),
    "purple": (0xAF, 0x51, 0xF5),
}

# Ordered lookup paths, tried before the recursive sweep.
GRADIENT_PATH = ["background", "single", "_0", "style", "color", "_0", "blendedGradient", "_0"]
SINGLE_PATH = ["background", "single", "_0", "style", "color", "_0", "blendedSingleColor", "_0"]
COMPLEMENT_PATH = ["background", "single", "_0", "style", "color", "_0", "blendedComplement", "_0"]


@dataclass
class ColorReading:
    """What was found, and which strategy found it."""

    colors: list[list[int]] = field(default_factory=list)
    strategy: str = "none"

    def __bool__(self) -> bool:
        return bool(self.colors)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _clamp(value: Any, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(high, number))


def _nested(source: Any, path: list[str]) -> Any:
    current = source
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _rgb(color: Any) -> list[int] | None:
    """Arc stores extended-sRGB floats that can fall outside 0-1."""
    if not isinstance(color, dict):
        return None
    out = []
    for component in ("red", "green", "blue"):
        if component not in color:
            return None
        out.append(round(_clamp(color[component], 0.0, 1.0) * 255))
    return out


def _looks_like_color(node: Any) -> bool:
    return (
        isinstance(node, dict)
        and "red" in node
        and "green" in node
        and "blue" in node
    )


def _sweep_colors(node: Any, found: list[list[int]], depth: int = 0) -> None:
    """Depth-first hunt for any red/green/blue triplet anywhere in the theme."""
    if depth > 12 or len(found) >= 6:
        return

    if _looks_like_color(node):
        rgb = _rgb(node)
        if rgb and rgb not in found:
            found.append(rgb)
        return

    if isinstance(node, dict):
        for key in sorted(node):
            _sweep_colors(node[key], found, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _sweep_colors(item, found, depth + 1)


# --------------------------------------------------------------------------
# Colour extraction
# --------------------------------------------------------------------------

def read_colors(window_theme: dict[str, Any]) -> ColorReading:
    """Up to three RGB triplets describing an Arc space background."""
    if not isinstance(window_theme, dict) or not window_theme:
        return ColorReading(strategy="empty-theme")

    gradient = _nested(window_theme, GRADIENT_PATH)
    if isinstance(gradient, dict):
        colors = [rgb for rgb in (_rgb(c) for c in gradient.get("baseColors", []) or []) if rgb]
        if colors:
            return ColorReading(colors[:3], "blendedGradient.baseColors")

    for path, label in ((SINGLE_PATH, "blendedSingleColor"), (COMPLEMENT_PATH, "blendedComplement")):
        node = _nested(window_theme, path)
        if isinstance(node, dict):
            rgb = _rgb(node.get("color")) or _rgb(node)
            if rgb:
                return ColorReading([rgb], label)

    palette = window_theme.get("primaryColorPalette") or {}
    for key in ("midTone", "shaded", "tintedLight", "tinted", "base"):
        rgb = _rgb(palette.get(key))
        if rgb:
            return ColorReading([rgb], f"primaryColorPalette.{key}")

    # Nothing matched a known shape. Sweep the whole structure rather than
    # silently defaulting to blue, which is what the original tool did.
    swept: list[list[int]] = []
    _sweep_colors(window_theme, swept)
    if swept:
        return ColorReading(swept[:3], "recursive-sweep")

    return ColorReading(strategy="no-color-found")


def theme_colors(window_theme: dict[str, Any]) -> list[list[int]]:
    return read_colors(window_theme).colors


def _modifiers(window_theme: dict[str, Any]) -> dict[str, Any]:
    for path in (GRADIENT_PATH, SINGLE_PATH):
        node = _nested(window_theme, path)
        if isinstance(node, dict) and isinstance(node.get("modifiers"), dict):
            return node["modifiers"]
    return {}


def zen_theme(window_theme: dict[str, Any]) -> dict[str, Any]:
    """Build a Zen workspace theme, falling back to Zen's neutral default."""
    reading = read_colors(window_theme)
    if not reading:
        return {"type": "gradient", "gradientColors": [], "opacity": DEFAULT_OPACITY, "texture": 0}

    modifiers = _modifiers(window_theme)
    opacity = (
        _clamp(modifiers.get("intensityFactor"), 0.2, 1.0)
        if "intensityFactor" in modifiers
        else DEFAULT_OPACITY
    )
    texture = (
        _clamp(modifiers.get("noiseFactor"), 0.0, 1.0)
        if modifiers.get("overlay") == "grain"
        else 0
    )

    return {
        "type": "gradient",
        "gradientColors": [
            {
                "c": rgb,
                "isCustom": False,
                "algorithm": "",
                "isPrimary": index == 0,
                "lightness": 50,
                "type": ZEN_COLOR_TYPE,
            }
            for index, rgb in enumerate(reading.colors)
        ],
        "opacity": opacity,
        "texture": texture,
    }


# --------------------------------------------------------------------------
# Perceptual nearest-colour matching (Oklab)
# --------------------------------------------------------------------------

def _srgb_to_linear(channel: int) -> float:
    value = channel / 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _to_oklab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    red, green, blue = (_srgb_to_linear(c) for c in rgb)

    long_ = 0.4122214708 * red + 0.5363325363 * green + 0.0514459929 * blue
    medium = 0.2119034982 * red + 0.6806995451 * green + 0.1073969566 * blue
    short = 0.0883024619 * red + 0.2817188376 * green + 0.6299787005 * blue

    long_ = long_ ** (1 / 3) if long_ > 0 else -((-long_) ** (1 / 3))
    medium = medium ** (1 / 3) if medium > 0 else -((-medium) ** (1 / 3))
    short = short ** (1 / 3) if short > 0 else -((-short) ** (1 / 3))

    return (
        0.2104542553 * long_ + 0.7936177850 * medium - 0.0040720468 * short,
        1.9779984951 * long_ - 2.4285922050 * medium + 0.4505937099 * short,
        0.0259040371 * long_ + 0.7827717662 * medium - 0.8086757660 * short,
    )


_CONTAINER_OKLAB = {name: _to_oklab(rgb) for name, rgb in CONTAINER_COLORS.items()}


def _color_from_seed(seed: str) -> str:
    """Stable container colour for spaces Arc never themed."""
    palette = sorted(CONTAINER_COLORS)
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return palette[digest[0] % len(palette)]


def nearest_container_color(
    window_theme: dict[str, Any],
    seed: str | None = None,
) -> tuple[str, ColorReading]:
    """Closest Firefox container colour, plus how the Arc colour was found."""
    reading = read_colors(window_theme)

    if not reading:
        # Arc leaves windowTheme empty on some spaces. Defaulting every one of
        # those to blue makes them indistinguishable, so derive from the name.
        if seed:
            return _color_from_seed(seed), ColorReading(strategy=f"{reading.strategy}+name-hash")
        return "blue", reading

    target = _to_oklab(tuple(reading.colors[0]))  # type: ignore[arg-type]

    def distance(name: str) -> float:
        lab = _CONTAINER_OKLAB[name]
        # Chroma weighted above lightness: hue identity matters more than brightness.
        return (
            0.6 * (lab[0] - target[0]) ** 2
            + (lab[1] - target[1]) ** 2
            + (lab[2] - target[2]) ** 2
        )

    return min(_CONTAINER_OKLAB, key=distance), reading
```

### `jslz4.py`

```python
# small tool for decoding zen-sessions.jsonlz4, made with Gemini. Used when debugging space labels showing up.

import argparse
import sys
import os
import lz4.block

def decompress_jsonlz4(input_path, output_path=None):
    if not os.path.exists(input_path):
        print(f"Error: File '{input_path}' does not exist.")
        sys.exit(1)

    # Default output filename if not specified
    if not output_path:
        base_name = os.path.splitext(input_path)[0]
        output_path = f"{base_name}.json" if base_name != input_path else f"{input_path}.decompressed.json"

    with open(input_path, "rb") as f:
        magic = f.read(8)
        if magic != b"mozLz40\0":
            print(f"Error: '{input_path}' does not have a valid Firefox jsonlz4 header.")
            sys.exit(1)
        
        compressed_bytes = f.read()
        decompressed_bytes = lz4.block.decompress(compressed_bytes)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(decompressed_bytes.decode("utf-8"))

    print(f"Successfully decompressed:\n  Input:  {input_path}\n  Output: {output_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Decompress Firefox .jsonlz4 files.")
    parser.add_argument("input", nargs="?", help="Path to the .jsonlz4 file")
    parser.add_argument("-o", "--output", help="Optional path for output .json file")

    args = parser.parse_args()

    # Prompt interactively if path wasn't provided via CLI argument
    file_input = args.input or input("Enter path to .jsonlz4 file: ").strip('"\' ')
    
    decompress_jsonlz4(file_input, args.output)
```
