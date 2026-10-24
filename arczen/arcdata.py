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