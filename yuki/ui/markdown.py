"""Yuki's replies as clean formatted text in the chat (the window's Chat page and the Alt+Shift overlay).

Typed replies come back as Markdown: a short summary line, then bullet points or
numbered steps, **bold**, `code`, links and small headings where useful. Qt's own
Markdown parser (md4c, GitHub dialect, through :class:`QTextDocument`) reads the
text; :func:`to_html` then writes it out again as Qt rich text styled for the
glass theme, because a label's default Markdown look is a word processor's
(huge headings, 40 px list indents, no code background):

* paragraphs with a small gap under each, no gap above;
* headings a step above the body size, semibold, never huge;
* lists as a narrow bullet or number column and a text column, nested lists
  indented one step, so a wrapped line lines up under its text;
* inline code and code blocks in a monospace font on a faint wash;
* links in the accent colour; block quotes dimmed with a thin bar.

A streaming reply is rendered again on every change; a reply of a few hundred
words takes well under a millisecond per pass. Spoken replies are plain text
and come through unchanged.
"""

from __future__ import annotations

import html
from functools import lru_cache
from typing import Any

from PySide6.QtGui import QFont, QTextBlock, QTextCharFormat, QTextDocument, QTextFormat, QTextListFormat

#: Monospace fonts, in order of preference (Windows 11 ships Cascadia).
MONO = "'Cascadia Mono', 'Cascadia Code', Consolas, monospace"


def _css(c: Any) -> str:
    return f"rgba({c.red()},{c.green()},{c.blue()},{c.alphaF():.3f})"


def _span(text: str, fmt: QTextCharFormat, theme: Any) -> str:
    out = html.escape(text).replace("\u2028", "<br>").replace("\n", "<br>")
    styles = []
    if fmt.fontWeight() >= QFont.Weight.DemiBold:
        styles.append("font-weight:700")
    if fmt.fontItalic():
        styles.append("font-style:italic")
    if fmt.fontStrikeOut():
        styles.append("text-decoration:line-through")
    if fmt.fontFixedPitch():
        wash = theme.text if theme is not None else None
        bg = f"background-color:{_css(_alpha(wash, 0.10 if theme.dark else 0.07))};" if wash is not None else ""
        out = f'<span style="font-family:{MONO};{bg}">&#8201;{out}&#8201;</span>'
    if styles:
        out = f'<span style="{";".join(styles)}">{out}</span>'
    if fmt.isAnchor() and fmt.anchorHref():
        color = f"color:{_css(theme.accent)};" if theme is not None else ""
        out = f'<a href="{html.escape(fmt.anchorHref(), quote=True)}" style="{color}text-decoration:none">{out}</a>'
    return out


def _alpha(c: Any, a: float) -> Any:
    from PySide6.QtGui import QColor

    out = QColor(c)
    out.setAlphaF(a)
    return out


def _inline(block: QTextBlock, theme: Any) -> str:
    parts = []
    it = block.begin()
    while not it.atEnd():
        frag = it.fragment()
        if frag.isValid():
            parts.append(_span(frag.text(), frag.charFormat(), theme))
        it += 1
    return "".join(parts)


def _is_code(block: QTextBlock) -> bool:
    bf = block.blockFormat()
    return bool(bf.nonBreakableLines() or bf.hasProperty(QTextFormat.Property.BlockCodeFence)
                or bf.hasProperty(QTextFormat.Property.BlockCodeLanguage))


def _marker(block: QTextBlock) -> str:
    lst = block.textList()
    style = lst.format().style()
    if style in (QTextListFormat.Style.ListDecimal, QTextListFormat.Style.ListLowerAlpha,
                 QTextListFormat.Style.ListUpperAlpha, QTextListFormat.Style.ListLowerRoman,
                 QTextListFormat.Style.ListUpperRoman):
        return f"{lst.itemNumber(block) + 1}."
    return "•" if lst.format().indent() <= 1 else "◦"


def to_html(text: str, theme: Any = None, *, px: int = 14) -> str:
    """``text`` (Markdown) as Qt rich text styled for ``theme`` (a :class:`~yuki.ui.window.theme.Theme`)."""
    return _render(text or "", theme.name if theme is not None else "", px, theme)


def _render(text: str, _key: str, px: int, theme: Any) -> str:
    return _render_cached(text, _key, px, _ThemeRef(theme))


class _ThemeRef:
    """Hashable by the theme's name, so :func:`_render_cached` keeps one result per text and theme."""

    def __init__(self, theme: Any) -> None:
        self.theme = theme

    def __hash__(self) -> int:
        return hash(getattr(self.theme, "name", ""))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _ThemeRef) and getattr(self.theme, "name", "") == getattr(other.theme, "name", "")


@lru_cache(maxsize=256)
def _render_cached(text: str, _key: str, px: int, ref: _ThemeRef) -> str:
    theme = ref.theme
    doc = QTextDocument()
    doc.setMarkdown(text, QTextDocument.MarkdownFeature.MarkdownDialectGitHub)
    out: list[str] = []
    block = doc.begin()
    open_list: list[Any] = []      # the QTextList objects whose table is open (outermost first)
    code: list[str] = []
    dim = _css(theme.text_2) if theme is not None else "gray"
    grid = _css(theme.baseline) if theme is not None else "gray"

    def close_lists(keep: int = 0) -> None:
        while len(open_list) > keep:
            open_list.pop()
            out.append("</table>")

    def flush_code() -> None:
        if not code:
            return
        wash = _css(_alpha(theme.text, 0.08 if theme.dark else 0.05)) if theme is not None else "transparent"
        body = "<br>".join(html.escape(line).replace(" ", "&nbsp;") for line in code)
        out.append(f'<table width="100%" cellspacing="0" cellpadding="8" style="margin:2px 0 8px 0;'
                   f'background-color:{wash}"><tr><td style="font-family:{MONO};font-size:{px - 1}px">'
                   f"{body}</td></tr></table>")
        code.clear()

    while block.isValid():
        bf = block.blockFormat()
        if _is_code(block):
            close_lists()
            code.append(block.text())
            block = block.next()
            continue
        flush_code()
        inline = _inline(block, theme)
        lst = block.textList()
        if lst is not None:
            # one small table per item: a narrow marker column and the text, so a wrapped line lines up under
            # its text; a nested item is indented one step
            depth = max(1, lst.format().indent())
            gap = 3 if block.next().isValid() and block.next().textList() is not None else 8
            out.append(f'<table cellspacing="0" cellpadding="0" style="margin:0 0 {gap}px {2 + (depth - 1) * 18}px">'
                       f'<tr><td width="{px + 6}" valign="top" style="color:{dim}">{_marker(block)}</td>'
                       f'<td valign="top">{inline}</td></tr></table>')
            block = block.next()
            continue
        close_lists()
        if not block.text().strip():
            block = block.next()
            continue
        level = bf.headingLevel()
        quote = bf.intProperty(QTextFormat.Property.BlockQuoteLevel) if bf.hasProperty(
            QTextFormat.Property.BlockQuoteLevel) else 0
        if level:
            size = px + (3 if level <= 1 else 2 if level == 2 else 1)
            out.append(f'<p style="margin:8px 0 4px 0;font-size:{size}px;font-weight:600">{inline}</p>')
        elif quote:
            out.append(f'<table cellspacing="0" cellpadding="0" style="margin:0 0 6px 0"><tr>'
                       f'<td width="3" style="background-color:{grid}"></td><td width="10"></td>'
                       f'<td style="color:{dim}">{inline}</td></tr></table>')
        elif bf.property(QTextFormat.Property.BlockTrailingHorizontalRulerWidth) is not None:
            out.append(f'<hr style="color:{grid}">')
        else:
            out.append(f'<p style="margin:0 0 6px 0">{inline}</p>')
        block = block.next()
    flush_code()
    close_lists()
    result = "".join(out)
    # the last block's bottom gap would pad the bubble: drop it
    for tail in ('<p style="margin:0 0 6px 0">',):
        i = result.rfind(tail)
        if i >= 0 and result.endswith("</p>") and result.find("<p", i + 1) < 0:
            result = result[:i] + '<p style="margin:0">' + result[i + len(tail):]
    return result


__all__ = ["to_html", "MONO"]
