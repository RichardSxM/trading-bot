"""Reading the design tokens back from ``tokens.css`` and checking their contrast (spec 0004 AC-15, AC-16).

``/styleguide`` shows every token with these values, and the token test compares them with the
frontmatter of root DESIGN.md.
"""

import re
from pathlib import Path
from typing import Final

TOKENS_CSS: Final = Path(__file__).resolve().parent.parent / "static" / "css" / "tokens.css"

_COMMENT: Final = re.compile(r"/\*.*?\*/", re.DOTALL)
_DECLARATION: Final = re.compile(r"--([a-z0-9-]+)\s*:\s*([^;]+);")
_HEX: Final = re.compile(r"#[0-9a-fA-F]{6}")


def parse_tokens_css(css: str) -> dict[str, str]:
    """Every ``--name: value;`` custom property, keyed by name without the dashes, in file order."""
    return {name: " ".join(value.split()) for name, value in _DECLARATION.findall(_COMMENT.sub("", css))}


def read_tokens(path: Path = TOKENS_CSS) -> dict[str, str]:
    """The tokens of the dashboard's own ``tokens.css``."""
    return parse_tokens_css(path.read_text(encoding="utf-8"))


def color_tokens(tokens: dict[str, str]) -> dict[str, str]:
    """Only the tokens whose value is a six digit hex color."""
    return {name: value for name, value in tokens.items() if _HEX.fullmatch(value)}


def relative_luminance(hex_color: str) -> float:
    """WCAG 2 relative luminance of a ``#rrggbb`` color."""
    if not _HEX.fullmatch(hex_color):
        raise ValueError(f"not a #rrggbb color: {hex_color!r}")
    channels = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    red, green, blue = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(first: str, second: str) -> float:
    """WCAG 2 contrast ratio of two ``#rrggbb`` colors, from 1 to 21."""
    lighter, darker = sorted((relative_luminance(first), relative_luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)
