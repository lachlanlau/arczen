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