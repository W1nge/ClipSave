from __future__ import annotations

import re
from functools import lru_cache

from PySide6.QtCore import QPointF
from PySide6.QtGui import QFont, QFontMetricsF, QTextLayout, QTextOption


_MACHINE_TEXT_SPAN_RE = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9+.-]*://|www\.)"
    r"[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+"
    r"|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    r"|(?:[A-Za-z]:[\\/]|\\\\)[^\s\r\n]+"
    r"|(?:\.{0,2}/|~/|/)?(?:[A-Za-z0-9._-]+[\\/])+[A-Za-z0-9._-]+"
    r"|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|[0-9a-fA-F]{20,}"
    r"|[A-Za-z0-9_+/=-]{20,}"
)


def plain_text_wrap_mode(content: str) -> QTextOption.WrapMode:
    value = content.strip()
    if not value:
        return QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere
    machine_text = (
        re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]*://\S+", value)
        or re.fullmatch(r"www\.\S+", value, re.IGNORECASE)
        or re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value)
        or re.fullmatch(r"(?:[A-Za-z]:[\\/]|\\\\|/|\./|\.\./|~/).+", value)
        or re.fullmatch(r"\S*[\\/]\S*", value)
        or re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            value,
        )
        or re.fullmatch(r"[0-9a-fA-F]{20,}", value)
        or re.fullmatch(r"[A-Za-z0-9_+/=-]{20,}", value)
        or re.fullmatch(
            r"(?:[A-Za-z0-9-]+\.)+[A-Za-z0-9-]{2,}(?:/\S*)?",
            value,
        )
    )
    if machine_text:
        return QTextOption.WrapMode.WrapAnywhere
    return QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere


def plain_text_layout_source(content: str) -> tuple[str, tuple[int, ...]]:
    return _layout_source_prefix(content[:330])


@lru_cache(maxsize=256)
def _layout_source_prefix(source: str) -> tuple[str, tuple[int, ...]]:
    machine_positions = [False] * len(source)
    if plain_text_wrap_mode(source) != QTextOption.WrapMode.WrapAnywhere:
        for match in _MACHINE_TEXT_SPAN_RE.finditer(source):
            machine_positions[match.start() : match.end()] = [True] * (
                match.end() - match.start()
            )
    transformed: list[str] = []
    original_boundaries = [0]
    for index, character in enumerate(source):
        transformed.append(character)
        # QTextLayout reports offsets in UTF-16 code units, not Python code
        # points.  Keep a boundary for the first half of a surrogate pair so
        # its line lengths can always map back to the original string.
        if ord(character) > 0xFFFF:
            original_boundaries.append(index)
        original_boundaries.append(index + 1)
        if machine_positions[index]:
            transformed.append("\u200b")
            original_boundaries.append(index + 1)
    return "".join(transformed), tuple(original_boundaries)


def _utf16_length(text: str) -> int:
    return sum(2 if ord(character) > 0xFFFF else 1 for character in text)


def plain_text_layout(
    content: str,
    width: int,
    height: int,
    font: QFont,
) -> tuple[list[QTextLayout], tuple[tuple[int, int], ...]]:
    source, original_boundaries = plain_text_layout_source(content)
    layouts: list[QTextLayout] = []
    lines: list[tuple[int, int]] = []
    transformed_offset = 0
    y = 0.0
    wrap_mode = plain_text_wrap_mode(content)
    segments = source.splitlines(keepends=True) or [source]
    for segment in segments:
        if y >= max(1, height):
            break
        newline_length = len(segment) - len(segment.rstrip("\r\n"))
        paragraph = segment[:-newline_length] if newline_length else segment
        layout = QTextLayout(paragraph, font)
        option = QTextOption()
        option.setWrapMode(wrap_mode)
        layout.setTextOption(option)
        layout.beginLayout()
        paragraph_line_indexes: list[int] = []
        completed = False
        while y < max(1, height):
            line = layout.createLine()
            if not line.isValid():
                completed = True
                break
            line.setLineWidth(max(1, width))
            line.setPosition(QPointF(0.0, y))
            transformed_start = transformed_offset + line.textStart()
            transformed_end = transformed_start + line.textLength()
            original_start = original_boundaries[transformed_start]
            original_end = original_boundaries[transformed_end]
            paragraph_line_indexes.append(len(lines))
            lines.append((original_start, original_end - original_start))
            y += line.height()
        layout.endLayout()
        layouts.append(layout)
        if not paragraph:
            y += QFontMetricsF(font).height()
            completed = True
        if completed and newline_length:
            newline_start = transformed_offset + _utf16_length(paragraph)
            newline_end = newline_start + _utf16_length(segment[-newline_length:])
            original_newline_length = (
                original_boundaries[newline_end]
                - original_boundaries[newline_start]
            )
            if paragraph_line_indexes:
                line_index = paragraph_line_indexes[-1]
                start, length = lines[line_index]
                lines[line_index] = (start, length + original_newline_length)
            else:
                lines.append(
                    (
                        original_boundaries[newline_start],
                        original_newline_length,
                    )
                )
        transformed_offset += _utf16_length(segment)
    return layouts, tuple(lines)


def plain_text_layout_signature(
    content: str,
    width: int,
    height: int,
    font: QFont,
) -> tuple[tuple[int, int], ...]:
    _layouts, lines = plain_text_layout(content, width, height, font)
    return lines
