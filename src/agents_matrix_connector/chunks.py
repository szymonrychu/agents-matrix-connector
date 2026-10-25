"""Split an agent's final text into Matrix message bodies (design D9)."""

import re

_BLANK_LINE = re.compile(r"\n[ \t]*\n")


def _strip_blank_lines(text: str) -> str:
    lines = text.split("\n")
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[start:end])


def _cut(text: str, limit: int) -> int:
    """Where the chunk starting `text` ends: the last blank line starting within the limit,
    else the last newline within it, else the limit itself."""
    paragraph = 0
    for match in _BLANK_LINE.finditer(text):
        if match.start() > limit:
            break
        paragraph = match.start()
    if paragraph > 0:
        return paragraph
    line = text.rfind("\n", 1, limit + 1)
    return line if line > 0 else limit


def split(text: str, limit: int) -> list[str]:
    """Chunks of at most `limit` characters, split on paragraph, then line, then hard
    boundaries, each stripped of leading and trailing blank lines; [] for blank text."""
    if limit < 1:
        raise ValueError(f"limit must be at least 1, got {limit}")
    chunks = []
    rest = _strip_blank_lines(text)
    while rest:
        if len(rest) <= limit:
            chunks.append(rest)
            break
        cut = _cut(rest, limit)
        chunk = _strip_blank_lines(rest[:cut])
        if chunk:
            chunks.append(chunk)
        rest = _strip_blank_lines(rest[cut:])
    return chunks
