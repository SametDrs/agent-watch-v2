"""Configuration paths, timing constants, and model metadata for agent-watch."""

from __future__ import annotations

import os
from pathlib import Path

POLL_MS = 500
WINDOW_HOURS = 12
QUOTA_CACHE_SECONDS = 60
EVENT_CAP = 2000
BODY_CAP = 4_000
SESSION_GRACE_SECONDS = 60
UNLINKED_GRACE_SECONDS = 60
QUIET_SECONDS = 300
MASCOT_TICK = 0.15
CODEX_LINK_WINDOW_SECONDS = 10

MODEL_COLORS: list[tuple[str, str]] = [
    ("hydrogen", "#6ea8fe"),
    ("flash", "#4dd0e1"),
    ("argon", "#b388ff"),
    ("gemini", "#7c9cff"),
    ("opus", "#c792ea"),
    ("sonnet", "#f78c6c"),
    ("haiku", "#7fd88f"),
    ("gpt", "#e6c07b"),
]


def claude_home() -> Path:
    """Return the Claude Code home directory from CLAUDE_HOME or ~/.claude."""
    if env := os.environ.get("CLAUDE_HOME"):
        return Path(env).expanduser()
    return Path.home() / ".claude"


def agy_home() -> Path:
    """Return the Antigravity CLI home directory from AGY_HOME or ~/.gemini/antigravity-cli."""
    if env := os.environ.get("AGY_HOME"):
        return Path(env).expanduser()
    return Path.home() / ".gemini" / "antigravity-cli"


def codex_home() -> Path:
    """Return the Codex CLI home directory from CODEX_HOME or ~/.codex."""
    if env := os.environ.get("CODEX_HOME"):
        return Path(env).expanduser()
    return Path.home() / ".codex"


def agy_bin() -> str:
    """Return the agy executable path from AGY_BIN, %LOCALAPPDATA%, or 'agy'."""
    if env := os.environ.get("AGY_BIN"):
        return env
    if local_app_data := os.environ.get("LOCALAPPDATA"):
        candidate = Path(local_app_data) / "agy" / "bin" / "agy.exe"
        if candidate.exists():
            return str(candidate)
    return "agy"


CLAUDE_HOME = claude_home()
AGY_HOME = agy_home()
CODEX_HOME = codex_home()
AGY_BIN = agy_bin()


def claude_projects_dir() -> Path:
    """Return the Claude projects directory."""
    return claude_home() / "projects"


def claude_sessions_dir() -> Path:
    """Return the Claude sessions directory."""
    return claude_home() / "sessions"


def agy_brain_dir() -> Path:
    """Return the agy brain directory."""
    return agy_home() / "brain"


def codex_sessions_dir() -> Path:
    """Return the Codex sessions directory."""
    return codex_home() / "sessions"


def model_color(model: str) -> str:
    """Return the display hex color for a model name."""
    if not model:
        return "#9aa0a6"
    lowered = model.lower()
    for substr, hex_color in MODEL_COLORS:
        if substr in lowered:
            return hex_color
    return "#9aa0a6"
