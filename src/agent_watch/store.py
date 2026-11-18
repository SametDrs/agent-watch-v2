"""In-memory store for sessions, agents, and status transitions."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from agent_watch import config
from agent_watch.model import (
    STANDALONE_SESSION_KEY,
    UNLINKED_SESSION_KEY,
    Agent,
    Event,
    EventKind,
    Session,
    Source,
    Status,
    main_agent_key,
    truncate_body,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ACTIVITY_KINDS = frozenset({
    EventKind.START,
    EventKind.PROMPT,
    EventKind.THINKING,
    EventKind.TOOL_START,
    EventKind.TOOL_END,
    EventKind.TEXT,
})


def _infer_source(agent_key: str) -> Source:
    """Infer agent source runtime from its key prefix."""
    if agent_key.startswith("agy:"):
        return Source.AGY
    if agent_key.startswith("codex:"):
        return Source.CODEX
    return Source.CLAUDE


def _expired_after_grace(
    keys: Iterable[str],
    active_keys: set[str],
    since_map: dict[str, datetime],
    now: datetime,
    grace_seconds: int,
) -> set[str]:
    """Track inactive keys in since_map and return keys inactive for >= grace_seconds."""
    expired: set[str] = set()
    for key in keys:
        if key in active_keys:
            since_map.pop(key, None)
            continue
        since = since_map.setdefault(key, now)
        if (now - since).total_seconds() >= grace_seconds:
            expired.add(key)
    return expired


def _sort_key(session: Session) -> tuple[bool, bool, int]:
    """Return sort key: regular before standalone, dated before None, newest first."""
    is_standalone = session.key == STANDALONE_SESSION_KEY
    if session.last_update is None:
        return (is_standalone, True, 0)
    dt = session.last_update
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    micros = (aware - _EPOCH) // timedelta(microseconds=1)
    return (is_standalone, False, -micros)


class Store:
    """In-memory store holding monitored sessions and agents."""

    _ACTIVITY_KINDS = _ACTIVITY_KINDS

    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self._last_seen: dict[str, datetime] = {}
        self._events: dict[str, deque[Event]] = {}
        self._event_counts: dict[str, int] = {}
        self._dead_since: dict[str, datetime] = {}
        self._unlinked_finished_since: dict[str, datetime] = {}

    def upsert_session(
        self,
        key: str,
        *,
        name: str | None = None,
        cwd: str | None = None,
        status: str | None = None,
        started: datetime | None = None,
    ) -> bool:
        """Create or update a session with non-None fields; return True if changed."""
        created = key not in self.sessions
        if created:
            session = Session(key=key)
            self.sessions[key] = session
        else:
            session = self.sessions[key]

        changed = created
        if name is not None and session.name != name:
            session.name = name
            changed = True
        if cwd is not None and session.cwd != cwd:
            session.cwd = cwd
            changed = True
        if status is not None and session.status != status:
            session.status = status
            changed = True
        if started is not None and session.started != started:
            session.started = started
            changed = True
        return changed

    def apply(self, events: Iterable[Event]) -> set[str]:
        """Apply normalized events to the store and return changed session keys."""
        changed: set[str] = set()
        for event in events:
            session, agent = self._resolve_session_and_agent(event)
            self._apply_event(session, agent, event)
            changed.add(session.key)
        return changed

    def _resolve_session_and_agent(self, event: Event) -> tuple[Session, Agent]:
        """Get or create the target session and agent for an event."""
        target_key = event.session_key
        session = self.sessions.get(target_key)

        # Keep updating an agy agent after it was moved out of STANDALONE_SESSION_KEY.
        if target_key == STANDALONE_SESSION_KEY and (
            session is None or event.agent_key not in session.agents
        ):
            if existing := self.find_agent(event.agent_key):
                target_key = existing.session_key
                session = self.sessions[target_key]

        if session is None:
            session = Session(key=target_key)
            self.sessions[target_key] = session

        agent = session.agents.get(event.agent_key)
        if agent is None:
            agent = Agent(
                key=event.agent_key,
                source=_infer_source(event.agent_key),
                session_key=target_key,
            )
            session.agents[event.agent_key] = agent
        else:
            agent.session_key = target_key

        return session, agent

    def _apply_event(self, session: Session, agent: Agent, event: Event) -> None:
        """Apply a single event's metadata, counters, and status transitions."""
        if event.agent_key == main_agent_key(session.key):
            agent.is_main = True
        if event.model:
            agent.model = event.model
        if event.effort:
            agent.effort = event.effort
        if event.title:
            agent.title = event.title
        if agent.started is None:
            agent.started = event.ts

        if session.last_update is None or event.ts > session.last_update:
            session.last_update = event.ts
        prev_seen = self._last_seen.get(agent.key)
        if prev_seen is None or event.ts > prev_seen:
            self._last_seen[agent.key] = event.ts
        if agent.last_write is None or event.ts > agent.last_write:
            agent.last_write = event.ts

        if event.kind == EventKind.TOOL_START:
            agent.calls += 1
            agent.tool_times.append(event.ts)
            if event.summary:
                agent.last_action = event.summary
        elif event.kind in (EventKind.TOOL_END, EventKind.TEXT):
            if event.summary:
                agent.last_action = event.summary
        elif event.kind == EventKind.END:
            agent.ended = event.ts
        elif event.kind == EventKind.ERROR:
            agent.ended = event.ts
            if event.summary:
                agent.last_action = event.summary

        if event.tokens_in:
            agent.in_tokens += event.tokens_in
        if event.tokens_cached:
            agent.cached_tokens += event.tokens_cached
        if event.tokens_out:
            agent.out_tokens += event.tokens_out
        if event.tokens_reasoning:
            agent.reasoning_tokens += event.tokens_reasoning
        if event.ctx_tokens is not None:
            agent.ctx_tokens = event.ctx_tokens

        if event.kind == EventKind.END:
            agent.status = event.status if event.status is not None else Status.DONE
        elif event.kind == EventKind.ERROR:
            agent.status = event.status if event.status is not None else Status.FAILED
        elif event.status is not None:
            agent.status = event.status
        elif event.kind in _ACTIVITY_KINDS and agent.status in (Status.PENDING, Status.STALE):
            agent.status = Status.RUNNING

        if agent.status == Status.RUNNING:
            agent.ended = None

        self._record_event(agent.key, event)

    def _record_event(self, agent_key: str, event: Event) -> None:
        """Truncate body if needed, append to per-agent deque, and increment count."""
        stored = event
        if event.body and len(event.body) > config.BODY_CAP:
            stored = replace(event, body=truncate_body(event.body))
        buf = self._events.get(agent_key)
        if buf is None or buf.maxlen != config.EVENT_CAP:
            buf = deque(buf or (), maxlen=config.EVENT_CAP)
            self._events[agent_key] = buf
        buf.append(stored)
        self._event_counts[agent_key] = self._event_counts.get(agent_key, 0) + 1

    def events(self, agent_key: str) -> list[Event]:
        """Return up to config.EVENT_CAP recent events for agent_key in order."""
        buf = self._events.get(agent_key)
        return list(buf) if buf else []

    def event_count(self, agent_key: str) -> int:
        """Return total number of events ever applied for agent_key."""
        return self._event_counts.get(agent_key, 0)

    def drop_dead_sessions(
        self,
        now: datetime,
        live_session_keys: set[str],
        grace_seconds: int,
    ) -> set[str]:
        """Remove non-live sessions (except UNLINKED_SESSION_KEY) after grace_seconds."""
        for stale_key in [k for k in self._dead_since if k not in self.sessions]:
            self._dead_since.pop(stale_key, None)

        candidates = [k for k in self.sessions if k != UNLINKED_SESSION_KEY]
        self._dead_since.pop(UNLINKED_SESSION_KEY, None)
        removed = _expired_after_grace(
            candidates, live_session_keys, self._dead_since, now, grace_seconds
        )
        for key in removed:
            self._remove_session(key)
        return removed

    def drop_finished_unlinked(
        self,
        now: datetime,
        running_agent_keys: set[str],
        grace_seconds: int,
    ) -> set[str]:
        """Remove agents in UNLINKED_SESSION_KEY not in running_agent_keys for grace_seconds."""
        session = self.sessions.get(UNLINKED_SESSION_KEY)
        if session is None:
            self._unlinked_finished_since.clear()
            return set()

        for stale_key in [k for k in self._unlinked_finished_since if k not in session.agents]:
            self._unlinked_finished_since.pop(stale_key, None)

        removed = _expired_after_grace(
            list(session.agents),
            running_agent_keys,
            self._unlinked_finished_since,
            now,
            grace_seconds,
        )
        for agent_key in removed:
            session.agents.pop(agent_key, None)
            self._drop_agent_state(agent_key)
        return removed

    def _drop_agent_state(self, agent_key: str) -> None:
        """Clear per-agent tracking state when an agent is removed."""
        self._last_seen.pop(agent_key, None)
        self._events.pop(agent_key, None)
        self._event_counts.pop(agent_key, None)
        self._unlinked_finished_since.pop(agent_key, None)

    def reset_agent(self, agent_key: str) -> bool:
        """Reset an existing agent's counters, timestamps, status, and recorded events."""
        agent = self.find_agent(agent_key)
        if agent is None:
            return False
        agent.title = ""
        agent.model = ""
        agent.effort = ""
        agent.status = Status.PENDING
        agent.started = None
        agent.ended = None
        agent.calls = 0
        agent.tool_times.clear()
        agent.last_action = ""
        agent.in_tokens = 0
        agent.out_tokens = 0
        agent.cached_tokens = 0
        agent.reasoning_tokens = 0
        agent.ctx_tokens = None
        agent.last_write = None
        self._drop_agent_state(agent_key)
        return True

    def _remove_session(self, session_key: str) -> None:
        """Remove a session and all tracking state for its agents."""
        self._dead_since.pop(session_key, None)
        session = self.sessions.pop(session_key, None)
        if session is not None:
            for agent_key in session.agents:
                self._drop_agent_state(agent_key)

    def move_agent(
        self,
        agent_key: str,
        to_session_key: str,
        parent_agent_key: str | None = None,
    ) -> set[str]:
        """Move an agent to another session and update parent_agent_key."""
        from_session: Session | None = None
        agent: Agent | None = None
        for session in self.sessions.values():
            if found := session.agents.get(agent_key):
                from_session = session
                agent = found
                break

        if from_session is None or agent is None:
            return set()

        from_session_key = from_session.key
        if from_session_key == to_session_key and agent.parent_agent_key == parent_agent_key:
            return set()

        agent.parent_agent_key = parent_agent_key
        if from_session_key == to_session_key:
            return {to_session_key}

        from_session.agents.pop(agent_key, None)
        if from_session_key == UNLINKED_SESSION_KEY:
            self._unlinked_finished_since.pop(agent_key, None)

        to_session = self.sessions.get(to_session_key)
        if to_session is None:
            to_session = Session(key=to_session_key)
            self.sessions[to_session_key] = to_session

        agent.session_key = to_session_key
        to_session.agents[agent_key] = agent

        last_seen = self._last_seen.get(agent_key)
        if last_seen is not None and (to_session.last_update is None or last_seen > to_session.last_update):
            to_session.last_update = last_seen

        return {from_session_key, to_session_key}

    def find_agent(self, agent_key: str) -> Agent | None:
        """Return the Agent matching agent_key across all sessions, or None."""
        for session in self.sessions.values():
            if agent := session.agents.get(agent_key):
                return agent
        return None

    def totals(self, session_key: str) -> dict[str, int]:
        """Return aggregate agent/tool/token counts for a session."""
        session = self.sessions.get(session_key)
        if session is None:
            return {
                "agents_done": 0,
                "agents_total": 0,
                "calls": 0,
                "out_tokens": 0,
                "running": 0,
            }
        return {
            "agents_done": session.done_count(),
            "agents_total": len(session.agents),
            "calls": session.total_calls(),
            "out_tokens": session.total_out_tokens(),
            "running": sum(1 for agent in session.agents.values() if agent.status == Status.RUNNING),
        }

    def sessions_sorted(self) -> list[Session]:
        """Return sessions ordered by last_update desc, None last, standalone always last."""
        return sorted(self.sessions.values(), key=_sort_key)
