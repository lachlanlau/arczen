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