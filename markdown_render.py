"""Lightweight markdown renderer for the chat Text widget.

Not a full CommonMark implementation — handles what actually shows up in
LLM output regularly: fenced code blocks, inline code, bold, headers
(#/##/###), and bullet/numbered lists. Tables and nested/complex markdown
are shown as plain text rather than mis-rendered — still readable, just
unstyled.

Operates on a raw tk.Text widget (not the CTkTextbox wrapper), inserting
tag tuples the same way main.py's own _insert() does."""

import re

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_HEADER_RE = re.compile(r"^(#{1,3})\s+(.*)$")
_BULLET_RE = re.compile(r"^(\s*)[-*]\s+(.*)$")
_NUMBERED_RE = re.compile(r"^(\s*)(\d+)\.\s+(.*)$")
_FENCE_RE = re.compile(r"^```(\w*)\s*$")

HEADER_TAG_BY_LEVEL = {1: "md_header1", 2: "md_header2", 3: "md_header3"}


def render_into(text_widget, content: str) -> None:
    """Insert markdown-formatted content at the end of text_widget."""
    lines = content.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]

        fence_match = _FENCE_RE.match(line.strip())
        if fence_match:
            i += 1
            code_lines = []
            while i < len(lines) and not _FENCE_RE.match(lines[i].strip()):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing fence, if present
            text_widget.insert("end", "\n".join(code_lines) + "\n", ("md_code_block",))
            continue

        header_match = _HEADER_RE.match(line)
        if header_match:
            level = len(header_match.group(1))
            tag = HEADER_TAG_BY_LEVEL.get(level, "md_header3")
            _insert_inline(text_widget, header_match.group(2), (tag,))
            text_widget.insert("end", "\n")
            i += 1
            continue

        bullet_match = _BULLET_RE.match(line)
        if bullet_match:
            text_widget.insert("end", "  • ", ("md_bullet",))
            _insert_inline(text_widget, bullet_match.group(2), ())
            text_widget.insert("end", "\n")
            i += 1
            continue

        numbered_match = _NUMBERED_RE.match(line)
        if numbered_match:
            number = numbered_match.group(2)
            text_widget.insert("end", f"  {number}. ", ("md_bullet",))
            _insert_inline(text_widget, numbered_match.group(3), ())
            text_widget.insert("end", "\n")
            i += 1
            continue

        _insert_inline(text_widget, line, ())
        text_widget.insert("end", "\n")
        i += 1


def _insert_inline(text_widget, text: str, base_tags: tuple) -> None:
    """Handles **bold** and `inline code` within a single line."""
    tokens = []
    for m in _BOLD_RE.finditer(text):
        tokens.append((m.start(), m.end(), "md_bold", m.group(1)))
    for m in _INLINE_CODE_RE.finditer(text):
        tokens.append((m.start(), m.end(), "md_code_inline", m.group(1)))
    tokens.sort(key=lambda t: t[0])

    filtered = []
    last_end = 0
    for start, end, kind, inner in tokens:
        if start >= last_end:
            filtered.append((start, end, kind, inner))
            last_end = end

    pos = 0
    for start, end, kind, inner in filtered:
        if start > pos:
            text_widget.insert("end", text[pos:start], base_tags)
        text_widget.insert("end", inner, base_tags + (kind,))
        pos = end
    if pos < len(text):
        text_widget.insert("end", text[pos:], base_tags)
