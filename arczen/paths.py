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