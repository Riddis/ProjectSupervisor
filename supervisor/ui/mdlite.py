"""Minimal, dependency-free Markdown subset for inbox evidence: headings, bold, inline code and
bullet lists only — the owner's own text and short research summaries, not a sandbox for untrusted authors.
Every piece of text is HTML-escaped before any tag is added, so the result is always safe to mark `| safe`."""
from __future__ import annotations

import html
import re

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")


def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _CODE.sub(r"<code>\1</code>", text)
    return text


def render(text: str) -> str:
    """Render `text` to a small safe HTML subset: blank-line paragraphs, `## heading`, `- item` bullet lists,
    `**bold**` and `` `code` ``. Anything else renders as plain escaped text."""
    parts: list[str] = []
    para: list[str] = []
    list_open = False

    def flush_para():
        if para:
            parts.append("<p>" + "<br>".join(para) + "</p>")
            para.clear()

    def close_list():
        nonlocal list_open
        if list_open:
            parts.append("</ul>")
            list_open = False

    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            flush_para()
            close_list()
        elif stripped.startswith("## "):
            flush_para()
            close_list()
            parts.append(f"<h4>{_inline(stripped[3:])}</h4>")
        elif stripped.startswith(("- ", "* ")):
            flush_para()
            if not list_open:
                parts.append("<ul>")
                list_open = True
            parts.append(f"<li>{_inline(stripped[2:])}</li>")
        else:
            close_list()
            para.append(_inline(stripped))
    flush_para()
    close_list()
    return "\n".join(parts)
