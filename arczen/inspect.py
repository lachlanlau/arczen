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
            layout = "side by side" if split.orientation == "horizontal" else f"{split.orientation} -> side by side"
            print(f"    - {layout}, {state}: {panes}")

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