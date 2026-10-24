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