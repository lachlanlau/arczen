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