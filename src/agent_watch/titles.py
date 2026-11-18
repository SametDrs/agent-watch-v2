"""Short agent titles derived from raw prompts."""

from __future__ import annotations

import re

TITLE_MAX = 60

_SLASH_CMD_RE = re.compile(r"^/[A-Za-z][\w:.-]*(?=\s)\s*")
_LABEL_RE = re.compile(
    r"^(?:goal|task|objective|prompt|brief|request|context|instructions?)\s*:\s*",
    re.IGNORECASE,
)
_BRIEF_RE = re.compile(
    r"^read\s+and\s+follow\s+(?:the\s+|this\s+)?(?:brief|instructions?|task|prompt|spec|plan)"
    r"(?:\s+(?:in|at|from|file))?\s*:?\s*(?P<path>[^\s\"'`]+|\"[^\"]+\"|'[^']+'|`[^`]+`)?[.:]?\s*",
    re.IGNORECASE,
)
_BULLET_RE = re.compile(r"^(?:#+\s+|[-*>]\s+|\d+[.)]\s+)")
_CLAUSE_RE = re.compile(r"[,;:](?=\s)|\s[-–—]\s")
_MIN_CLAUSE = 20
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s+[A-Z0-9`\"'(\[]|$)")


def _strip_prefixes(text: str) -> tuple[str, str]:
    """Strip slash-commands, labels and brief pointers; return (rest, last brief path)."""
    path = ""
    for _ in range(6):
        before = text
        text = text.lstrip()
        text = _BULLET_RE.sub("", text, count=1)
        text = _SLASH_CMD_RE.sub("", text, count=1)
        text = _LABEL_RE.sub("", text, count=1)
        if match := _BRIEF_RE.match(text):
            path = (match.group("path") or "").strip("\"'`") or path
            text = text[match.end():]
        if text == before:
            break
    return text.strip(), path


def _basename(path: str) -> str:
    parts = path.replace("\\", "/").rstrip("/").split("/")
    return parts[-1] if path else ""


def _clause_cut(text: str, limit: int) -> str:
    """Return the longest clause (up to a , ; : or dash) that fits the limit, or ''."""
    best = ""
    for match in _CLAUSE_RE.finditer(text):
        head = text[: match.start()]
        if len(head) > limit:
            break
        if len(head) >= _MIN_CLAUSE and head.count("(") == head.count(")"):
            best = head
    return best


def short_title(prompt: str | None, limit: int = TITLE_MAX) -> str:
    """Return a short title from a prompt: first clause, no commands/labels, word-capped."""
    if not prompt:
        return ""
    rest, path = _strip_prefixes(prompt.strip())
    # First non-empty line that still has content after stripping labels.
    line = ""
    for raw in rest.splitlines():
        cand, _ = _strip_prefixes(raw)
        if cand:
            line = cand
            break
    text = " ".join(line.split())
    if not text:
        text = _basename(path)
    if not text:
        return ""
    if match := _SENTENCE_END_RE.search(text):
        text = text[: match.end()].rstrip(".")
    text = text.strip()
    if len(text) <= limit:
        return text
    if clause := _clause_cut(text, limit):
        return clause
    cut = text[: limit - 1]
    if text[limit - 1] != " " and " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-–—(") + "…"
