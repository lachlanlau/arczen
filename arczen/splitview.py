# arczen/splitview.py
"""Rebuild Arc's split views in Zen's session format.

Schema verified against three Zen session dumps: an unpinned split, a pinned
split, and a pinned split nested inside a folder.

A split is a tab group with "splitView": true. Its members keep their place in
session["tabs"] and point at the group through "groupId"; the geometry lives in
session["splitViewData"], keyed on the same group id and referring to tabs by
their "zenSyncId".

Because a pinned tab already uses "groupId" to say which folder it is in, a
split inside a folder cannot express that on its tabs. Zen instead adds a
session["folders"] entry for the split group carrying "parentId". A split
directly under the pinned row, or any unpinned split, has no folder entry.

No "maxSplitViewId" key appears in any dump, so none is written.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Firefox tab-group colours, which Zen inherits. Invisible on a split group
# (its name is empty and it renders as panes), but the field is always present.
GROUP_COLORS = ("blue", "purple", "cyan", "orange", "yellow", "pink", "green", "gray", "red")

# Zen has no vertical split, so every Arc orientation lands side by side.
GRID_TYPE = "vsep"
SPLIT_DIRECTION = "row"


@dataclass
class SplitReport:
    """What the write actually managed to do, for the migration summary."""

    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _tab_url(tab: dict[str, Any]) -> str:
    entries = tab.get("entries") or []
    if not entries or not isinstance(entries[0], dict):
        return ""
    return str(entries[0].get("url") or "")


def _id_base(folders: list[dict[str, Any]], groups: list[dict[str, Any]]) -> str:
    """Reuse the numeric stem the session already uses for folder/group ids."""
    for entry in list(folders) + list(groups):
        head = str(entry.get("id") or "").split("-", 1)[0]
        if head.isdigit():
            return head
    return str(int(time.time() * 1000))


def _next_id(base: str, counter: int, taken: set[str]) -> tuple[int, str]:
    """Fresh split-group id, distinct from every folder and group already there."""
    while True:
        counter += 1
        candidate = f"{base}-s{counter}"
        if candidate not in taken:
            taken.add(candidate)
            return counter, candidate


def _sizes(members: list[Any]) -> list[int]:
    """Pane widths as percentages that add up to 100.

    Arc stores a factor for some panes only, so an even split is the fallback.
    """
    count = len(members)
    first = getattr(members[0], "width_factor", None)
    if count == 2 and isinstance(first, (int, float)) and 0.05 < float(first) < 0.95:
        left = int(round(float(first) * 100))
        return [left, 100 - left]
    share = 100 // count
    sizes = [share] * count
    sizes[-1] = 100 - share * (count - 1)
    return sizes


def _find_run(
    tabs: list[dict[str, Any]],
    urls: list[str],
    pinned: bool,
    claimed: set[int],
    workspace: str | None,
) -> list[dict[str, Any]] | None:
    """Locate the already-written member tabs of one split.

    Arc emits a split's children consecutively, so the panes arrive in the tab
    list as an unbroken run with matching URLs, pin state, workspace and folder.
    A run that cannot be identified is left alone rather than guessed at.
    """
    width = len(urls)
    for start in range(len(tabs) - width + 1):
        window = tabs[start:start + width]
        if any(id(tab) in claimed for tab in window):
            continue
        if any(tab.get("zenIsEmpty") for tab in window):
            continue
        if any(bool(tab.get("pinned")) != pinned for tab in window):
            continue
        space_id = window[0].get("zenWorkspace")
        if workspace is not None and space_id != workspace:
            continue
        if any(tab.get("zenWorkspace") != space_id for tab in window):
            continue
        if len({str(tab.get("groupId") or "") for tab in window}) != 1:
            continue
        if [_tab_url(tab) for tab in window] != urls:
            continue
        return window
    return None


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def apply_split_views(session: dict[str, Any], sidebar: Any, options: Any) -> SplitReport:
    """Write every migratable Arc split into an already-built Zen session.

    Mutates `session` in place and must run before it is serialised.
    """
    report = SplitReport()

    tabs: list[dict[str, Any]] = session.setdefault("tabs", [])
    folders: list[dict[str, Any]] = session.setdefault("folders", [])
    groups: list[dict[str, Any]] = session.setdefault("groups", [])
    split_data: list[dict[str, Any]] = session.setdefault("splitViewData", [])

    # Zen marks split-ness on every folder and group, not just the split ones.
    for folder in folders:
        folder.setdefault("splitViewGroup", False)
    for group in groups:
        group.setdefault("splitView", False)

    taken_ids = {str(entry.get("id")) for entry in list(folders) + list(groups)}
    folders_by_id = {str(folder.get("id")): folder for folder in folders}
    base = _id_base(folders, groups)

    claimed: set[int] = set()
    counter = 0
    color_index = 0

    for space in sidebar.spaces:
        workspace: str | None = None
        for split in sorted(space.splits, key=lambda item: item.order):
            label = f"{space.name}: " + " + ".join(member.title for member in split.members)

            if split.pinned and not options.copies("tabs"):
                continue
            if not split.pinned and not options.copies("temporary-tabs"):
                continue
            if len(split.members) < 2:
                report.skipped.append(f"{label} (only one pane survived the copy)")
                continue

            window = _find_run(
                tabs,
                [member.url for member in split.members],
                bool(split.pinned),
                claimed,
                workspace,
            )
            if window is None:
                report.skipped.append(f"{label} (member tabs not found in the written session)")
                logger.warning("Split %s: member tabs not located; left as plain tabs", split.item_id[:8])
                continue

            workspace = window[0].get("zenWorkspace")
            parent_id = str(window[0].get("groupId") or "") or None
            parent = folders_by_id.get(parent_id) if parent_id else None

            counter, group_id = _next_id(base, counter, taken_ids)

            for tab in window:
                tab["groupId"] = group_id
                claimed.add(id(tab))

            member_ids = [str(tab.get("zenSyncId")) for tab in window]
            sizes = _sizes(split.members)

            split_data.append({
                "groupId": group_id,
                "gridType": GRID_TYPE,
                "layoutTree": {
                    "type": "splitter",
                    "direction": SPLIT_DIRECTION,
                    "children": [
                        {"type": "leaf", "tabId": tab_id, "sizeInParent": size}
                        for tab_id, size in zip(member_ids, sizes)
                    ],
                },
                "tabs": member_ids,
            })

            groups.append({
                "pinned": bool(split.pinned),
                "splitView": True,
                "id": group_id,
                "name": "",
                "color": GROUP_COLORS[color_index % len(GROUP_COLORS)],
                "collapsed": False,
                "saveOnWindowClose": True,
            })
            color_index += 1

            # Only a nested split needs a folder entry; that entry is the sole
            # place the parent folder can be recorded.
            if split.pinned and parent is not None:
                empty_ids = parent.get("emptyTabIds") or []
                previous = (
                    {"type": "tab", "id": empty_ids[0]}
                    if empty_ids
                    else {"type": "start", "id": None}
                )
                folders.append({
                    "pinned": True,
                    "splitViewGroup": True,
                    "id": group_id,
                    "name": "",
                    "collapsed": False,
                    "saveOnWindowClose": True,
                    "parentId": parent_id,
                    "prevSiblingInfo": previous,
                    "emptyTabIds": [],
                    "workspaceId": workspace,
                })

            note = ""
            if split.orientation != "horizontal":
                note = f" (Arc's {split.orientation} layout became side by side)"
            if len(split.members) > 2:
                note += " (3+ panes: layout untested)"
            report.written.append(label + note)

            logger.info(
                "Split %s -> Zen group %s (%s, %d pane(s), parent folder %s)",
                split.item_id[:8],
                group_id,
                "pinned" if split.pinned else "open",
                len(member_ids),
                parent_id or "none",
            )

    return report