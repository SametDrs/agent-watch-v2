"""Colors, icons and formatting helpers for the dashboard widgets."""

from __future__ import annotations

import re

from agent_watch.model import Source, Status, effort_from_model

ACCENT = "#f0883e"
SOURCE_COLORS: dict[Source, str] = {
    Source.CLAUDE: "#d97757",
    Source.AGY: "#6ea8fe",
    Source.CODEX: "#3d7bf0",
}
STATUS_COLORS: dict[Status, str] = {
    Status.RUNNING: ACCENT,
    Status.DONE: "#3fb950",
    Status.FAILED: "#f85149",
    Status.PENDING: "#6e7681",
    Status.STALE: "#6e7681",
}
STATUS_ICONS: dict[Status, str] = {
    Status.RUNNING: "●",
    Status.DONE: "✓",
    Status.FAILED: "✗",
    Status.PENDING: "○",
    Status.STALE: "?",
}
DIM = "#8b949e"


def fmt_duration(seconds: float | None) -> str:
    """Format seconds as M:SS or H:MM:SS; None becomes an en dash."""
    if seconds is None:
        return "–"
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def fmt_tokens(n: int | None) -> str:
    """Format a token count compactly (950, 53k, 1.2M); None becomes an en dash."""
    if n is None:
        return "–"
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        if n >= 10_000:
            return f"{round(n / 1000)}k"
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return f"{n / 1_000_000:.1f}M".replace(".0M", "M")


_CLAUDE_RE = re.compile(r"^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{6,})?$")


def short_model(model: str) -> str:
    """Return a short display name for a model id."""
    if not model:
        return "?"
    lowered = model.lower()
    if effort := effort_from_model(lowered):
        lowered = lowered.removesuffix(f"-{effort}") or lowered
    if match := _CLAUDE_RE.match(lowered):
        name, major, minor = match.groups()
        return f"{name} {major}.{minor}" if minor else f"{name} {major}"
    stripped = re.sub(r"^gemini-\d+(\.\d+)?-", "", lowered)
    return stripped or lowered
