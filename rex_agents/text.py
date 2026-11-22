"""Lines of styled text measured in terminal cells. A line is a list of spans,
so widths can be measured and cut without parsing escape codes."""

from __future__ import annotations

import unicodedata
from typing import NamedTuple, Optional

# SGR codes. Dim is the terminal's own, so it reads on any background.
MUTED = "2"


class Span(NamedTuple):
    text: str
    color: Optional[str] = None
    bold: bool = False
    key: Optional[str] = None  # the key a click on the span presses


def key_at(line: Line, column: int) -> Optional[str]:
    """The key of the span covering `column`, counted from 0."""
    for span in line:
        column -= text_width(span.text)
        if column < 0:
            return span.key
    return None


Line = list[Span]


def char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def text_width(text: str) -> int:
    return sum(char_width(c) for c in text)


def line_width(line: Line) -> int:
    return sum(text_width(s.text) for s in line)


def plain(line: Line) -> str:
    return "".join(s.text for s in line)


def fit(line: Line, width: int) -> Line:
    """Exactly `width` cells: padded, or cut with an ellipsis."""
    current = line_width(line)
    if current <= width:
        return [*line, Span(" " * (width - current))] if current < width else list(line)
    out: Line = []
    room = max(width - 1, 0)
    for span in line:
        kept = ""
        for ch in span.text:
            w = char_width(ch)
            if w > room:
                room = -1
                break
            kept += ch
            room -= w
        if kept:
            out.append(span._replace(text=kept))
        if room < 0:
            break
    out.append(Span("…", MUTED))
    return [*out, Span(" " * (width - line_width(out)))]


def pad_text(text: str, width: int, right: bool = False) -> str:
    gap = " " * max(width - text_width(text), 0)
    return gap + text if right else text + gap


def to_ansi(line: Line, color: bool) -> str:
    if not color:
        return plain(line)
    out: list[str] = []
    for span in line:
        codes = [code for code in ("1" if span.bold else None, span.color) if code]
        # Blanks show no color, so a run of them needs no escape codes.
        if codes and span.text.strip():
            out.append(f"\x1b[{';'.join(codes)}m{span.text}\x1b[0m")
        else:
            out.append(span.text)
    return "".join(out)


def plan_text(plan: str) -> str:
    return plan.capitalize() if plan.islower() else plan


