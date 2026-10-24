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