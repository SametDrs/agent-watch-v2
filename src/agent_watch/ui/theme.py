"""Colors, icons and formatting helpers for the dashboard widgets."""

from __future__ import annotations

import re

from agent_watch.model import Source, Status, effort_from_model

BG = "#0f1117"
SURFACE = "#161b26"
SURFACE_HI = "#1f2533"
BORDER = "#2a3142"
TEXT = "#e6e9ef"
MUTED = "#9aa3b5"
ACCENT = "#8aa4ff"
SOURCE_COLORS: dict[Source, str] = {
    Source.CLAUDE: "#e08a6a",
    Source.AGY: "#7fb2ff",
    Source.CODEX: "#a99bff",
}
STATUS_COLORS: dict[Status, str] = {
    Status.RUNNING: "#f2b866",
    Status.DONE: "#7fd1a0",
    Status.FAILED: "#ff7a8a",
    Status.PENDING: "#5c6478",
    Status.STALE: "#5c6478",
}
STATUS_ICONS: dict[Status, str] = {
    Status.RUNNING: "●",
    Status.DONE: "✓",
    Status.FAILED: "✗",
    Status.PENDING: "○",
    Status.STALE: "?",
}
DIM = "#6b7489"


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
