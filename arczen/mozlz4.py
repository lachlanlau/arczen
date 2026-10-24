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