"""Shared data model contract for agent-watch."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import re

from agent_watch import config


class Status(str, Enum):
    """Execution status of an agent."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    STALE = "stale"


class Source(str, Enum):
    """Originating runtime for an agent."""

    CLAUDE = "claude"
    AGY = "agy"
    CODEX = "codex"


class EventKind(str, Enum):
    """Normalized event kind emitted by log watchers."""

    START = "start"
    PROMPT = "prompt"
    THINKING = "thinking"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    TEXT = "text"
    USAGE = "usage"
    END = "end"
    ERROR = "error"


@dataclass
class Event:
    """Single normalized event from an agent log stream."""

    ts: datetime
    session_key: str
    agent_key: str
    kind: EventKind
    tool: str | None = None
    summary: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    ctx_tokens: int | None = None
    status: Status | None = None
    model: str | None = None
    title: str | None = None
    body: str = ""
    effort: str | None = None
    tokens_cached: int = 0
    tokens_reasoning: int = 0
    tool_id: str | None = None


@dataclass
class Agent:
    """Tracked Claude subagent, main agent, agy conversation, or Codex rollout."""

    key: str
    source: Source
    session_key: str
    title: str = ""
    model: str = ""
    status: Status = Status.PENDING
    parent_agent_key: str | None = None
    started: datetime | None = None
    ended: datetime | None = None
    calls: int = 0
    tool_times: list[datetime] = field(default_factory=list)
    last_action: str = ""
    in_tokens: int = 0
    out_tokens: int = 0
    ctx_tokens: int | None = None
    tool_use_id: str | None = None
    effort: str = ""
    is_main: bool = False
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    last_write: datetime | None = None

    def duration_seconds(self, now: datetime | None = None) -> float | None:
        """Return elapsed seconds between started and ended (or now)."""
        if self.started is None:
            return None
        end = self.ended if self.ended is not None else (now if now is not None else datetime.now(timezone.utc))
        return max(0.0, (end - self.started).total_seconds())


@dataclass
class Session:
    """Session grouping one or more agents."""

    key: str
    name: str = ""
    cwd: str = ""
    status: str = "idle"
    started: datetime | None = None
    last_update: datetime | None = None
    agents: dict[str, Agent] = field(default_factory=dict)

    def done_count(self) -> int:
        """Return the number of agents with DONE status."""
        return sum(1 for agent in self.agents.values() if agent.status == Status.DONE)

    def total_calls(self) -> int:
        """Return total tool call count across all agents."""
        return sum(agent.calls for agent in self.agents.values())

    def total_out_tokens(self) -> int:
        """Return total output tokens across all agents."""
        return sum(agent.out_tokens for agent in self.agents.values())


@dataclass
class ShellCall:
    """Shell command execution record."""

    session_id: str
    agent_key: str | None
    tool_use_id: str
    ts_start: datetime
    ts_end: datetime | None
    cwd: str
    command: str


STANDALONE_SESSION_KEY = "agy-standalone"
UNLINKED_SESSION_KEY = STANDALONE_SESSION_KEY

_EFFORT_LEVELS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max"})


def main_agent_key(session_key: str) -> str:
    """Return the synthetic agent key for a session's main agent."""
    return f"{session_key}:main"


def effort_from_model(model: str) -> str:
    """Return trailing reasoning effort segment from model name, or ''."""
    if not model or "-" not in model:
        return ""
    seg = model.rsplit("-", 1)[-1].strip().lower()
    return seg if seg in _EFFORT_LEVELS else ""


def utc(dt_str: str) -> datetime:
    """Parse an ISO-8601 timestamp string into a tz-aware UTC datetime."""
    normalized = dt_str.strip()
    if normalized.endswith(("Z", "z")):
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        # Clamp sub-microsecond precision (>6 digits) to 6 digits if present.
        clamped = re.sub(r"(\.\d{6})\d+", r"\1", normalized)
        dt = datetime.fromisoformat(clamped)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def truncate_body(text: str, cap: int | None = None) -> str:
    """Truncate text to cap (default config.BODY_CAP) chars with trailing ellipsis when exceeded."""
    limit = config.BODY_CAP if cap is None else cap
    if len(text) <= limit:
        return text
    if limit <= 0:
        return ""
    return text[: limit - 1] + "…"
