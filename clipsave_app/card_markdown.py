"""Bounded Markdown preview documents for asset grid cards.

Every fragment of user content is HTML-escaped before any markup is
generated: raw HTML never passes through, no ``<img>`` is ever emitted and
the document never references external resources.
"""

from __future__ import annotations

import html
import re

from PySide6.QtGui import QFont, QTextBlockFormat, QTextCharFormat, QTextCursor, QTextDocument

_DARK_COLORS = {
    "heading": "#f2f5f8",
    "muted": "#9aa4b0",
    "link": "#6ea8fe",
    "code_bg": "rgba(255,255,255,26)",
    "code_block_bg": "rgba(255,255,255,17)",
}
_LIGHT_COLORS = {
    "heading": "#242c35",
    "muted": "#6b7684",
    "link": "#2f7df6",
    "code_bg": "rgba(57,76,96,24)",
    "code_block_bg": "rgba(57,76,96,17)",
}

_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_RULE_RE = re.compile(r"^\s*(?:-\s*){3,}$|^\s*(?:\*\s*){3,}$|^\s*(?:_\s*){3,}$")
_LIST_RE = re.compile(r"^(\s*)(?:[-*+]|\d{1,3}[.)])\s+(.*)$")
_TASK_RE = re.compile(r"^\[([ xX])\]\s+(.*)$")
_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)[^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)[^)]*\)")
_AUTOLINK_RE = re.compile(r"https?://[^\s<>\"'，。；：！？（）【】《》]+")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\s][^*]*?)\*(?!\*)")
_STRIKE_RE = re.compile(r"~~([^~]+)~~")


def build_card_markdown_document(content: str, dark: bool, font: QFont) -> QTextDocument:
    colors = _DARK_COLORS if dark else _LIGHT_COLORS
    document = QTextDocument()
    document.setDocumentMargin(0)
    document.setDefaultFont(font)
    document.setDefaultStyleSheet(_stylesheet(colors))
    document.setHtml(_render_html(content, colors))
    _apply_typography(document, font)
    return document


def _stylesheet(colors: dict[str, str]) -> str:
    return f"""
    h1, h2, h3, h4, h5, h6 {{ color: {colors['heading']}; font-weight: 600;
        margin-top: 4px; margin-bottom: 2px; }}
    p {{ margin-top: 0; margin-bottom: 3px; }}
    pre {{ background-color: {colors['code_block_bg']}; margin-top: 2px; margin-bottom: 3px;
          font-family: Consolas, 'Courier New', monospace; }}
    code {{ background-color: {colors['code_bg']};
           font-family: Consolas, 'Courier New', monospace; }}
    blockquote {{ margin-left: 10px; margin-right: 0; color: {colors['muted']}; }}
    ul, ol {{ margin-left: 14px; margin-top: 0; margin-bottom: 2px; }}
    li {{ margin-top: 0; margin-bottom: 1px; }}
    a {{ color: {colors['link']}; text-decoration: none; }}
    """


def _inline(source: str, colors: dict[str, str]) -> str:
    fragments: list[str] = []

    def keep(fragment: str) -> str:
        fragments.append(fragment)
        return f"\x00{len(fragments) - 1}\x00"

    text = html.escape(source)
    text = _CODE_SPAN_RE.sub(lambda m: keep(f"<code>{m.group(1)}</code>"), text)
    text = _IMAGE_RE.sub(
        lambda m: keep(
            f'<span style="color:{colors["muted"]}">\N{FRAME WITH PICTURE} {m.group(1)}</span>'
        ),
        text,
    )
    text = _LINK_RE.sub(
        lambda m: keep(f'<a href="{m.group(2)}">{m.group(1)}</a>'), text
    )

    def autolink(match: re.Match) -> str:
        url = match.group(0)
        trail = ""
        while url and url[-1] in ".,;:!?)]>":
            trail = url[-1] + trail
            url = url[:-1]
        if not url:
            return match.group(0)
        return keep(f'<a href="{url}">{url}</a>') + trail

    text = _AUTOLINK_RE.sub(autolink, text)
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _ITALIC_RE.sub(r"<i>\1</i>", text)
    text = _STRIKE_RE.sub(r"<s>\1</s>", text)
    for index, fragment in enumerate(fragments):
        text = text.replace(f"\x00{index}\x00", fragment)
    return text


def _render_item(text: str, colors: dict[str, str]) -> str:
    task = _TASK_RE.match(text)
    if task is None:
        return _inline(text, colors)
    box = "☑" if task.group(1).lower() == "x" else "□"
    return f"{box}&nbsp;" + _inline(task.group(2), colors)


def _render_list_items(items: list[tuple[bool, int, str]], colors: dict[str, str]) -> str:
    if not items:
        return ""
    tag = "ol" if items[0][0] else "ul"
    base = items[0][1]
    parts: list[str] = []
    index = 0
    while index < len(items):
        _, indent, text = items[index]
        if indent <= base:
            index += 1
            child: list[tuple[bool, int, str]] = []
            while index < len(items) and items[index][1] > base:
                child.append(items[index])
                index += 1
            entry = _render_item(text, colors)
            if child:
                entry += _render_list_items(child, colors)
            parts.append(f"<li>{entry}</li>")
        else:
            parts.append(f"<li>{_render_item(text, colors)}</li>")
            index += 1
    return f"<{tag}>" + "".join(parts) + f"</{tag}>"


def _render_html(content: str, colors: dict[str, str]) -> str:
    blocks: list[str] = []
    lines = content.splitlines()
    for index, line in enumerate(lines):
        if line.strip():
            if _HEADING_RE.match(line):
                lines.pop(index)
            break
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)[:3]
            index += 1
            code: list[str] = []
            while index < total and not lines[index].lstrip().startswith(marker):
                code.append(lines[index])
                index += 1
            index += 1
            blocks.append("<pre>" + html.escape("\n".join(code)) + "</pre>")
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            blocks.append(
                f"<h{level}>{_inline(heading.group(2), colors)}</h{level}>"
            )
            index += 1
            continue
        if _RULE_RE.match(line):
            index += 1
            continue
        if line.lstrip().startswith(">"):
            quote: list[str] = []
            while index < total and lines[index].lstrip().startswith(">"):
                quote.append(lines[index].lstrip()[1:].lstrip())
                index += 1
            rendered = "<br>".join(
                _inline(part, colors) for part in quote if part
            )
            blocks.append(f"<blockquote>{rendered}</blockquote>")
            continue
        listed = _LIST_RE.match(line)
        if listed:
            items: list[tuple[bool, int, str]] = []
            while index < total:
                match = _LIST_RE.match(lines[index])
                if match is None:
                    break
                marker = match.group(0).lstrip()[:1]
                items.append(
                    (
                        marker.isdigit(),
                        len(match.group(1).expandtabs(4)),
                        match.group(2),
                    )
                )
                index += 1
            blocks.append(_render_list_items(items, colors))
            continue
        if not line.strip():
            index += 1
            continue
        paragraph: list[str] = []
        while index < total:
            current = lines[index]
            if (
                not current.strip()
                or _FENCE_RE.match(current)
                or _HEADING_RE.match(current)
                or _RULE_RE.match(current)
                or _LIST_RE.match(current)
                or current.lstrip().startswith(">")
            ):
                break
            paragraph.append(current.strip())
            index += 1
        blocks.append(
            "<p>" + "<br>".join(_inline(part, colors) for part in paragraph) + "</p>"
        )
    return "".join(blocks)


def _apply_typography(document: QTextDocument, font: QFont) -> None:
    base_size = font.pointSizeF() if font.pointSizeF() > 0 else 12.0
    block = document.begin()
    while block.isValid():
        block_format = block.blockFormat()
        level = block_format.headingLevel()
        cursor = QTextCursor(block)
        if level:
            cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            heading_format = QTextCharFormat()
            heading_format.setFontPointSize(base_size)
            heading_format.setFontWeight(QFont.Weight.DemiBold)
            cursor.mergeCharFormat(heading_format)
        else:
            block_format.setLineHeight(
                140.0, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value
            )
            cursor.setBlockFormat(block_format)
        block = block.next()
