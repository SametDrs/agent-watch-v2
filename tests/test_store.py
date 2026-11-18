"""Tests for the in-memory Store and event/status transition rules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agent_watch import config
from agent_watch.model import (
    STANDALONE_SESSION_KEY,
    UNLINKED_SESSION_KEY,
    Event,
    EventKind,
    Source,
    Status,
    main_agent_key,
)
from agent_watch.store import Store


def _ts(minutes: int = 0) -> datetime:
    """Return a tz-aware UTC datetime offset by minutes from a fixed baseline."""
    return datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)


def test_upsert_session_create_and_partial_update() -> None:
    store = Store()

    assert store.upsert_session("s1", name="proj", cwd="/repo") is True
    session = store.sessions["s1"]
    assert session.name == "proj"
    assert session.cwd == "/repo"
    assert session.status == "idle"
    assert session.started is None

    assert store.upsert_session("s1", name="proj") is False
    assert store.upsert_session("s1", name=None, cwd=None, status=None, started=None) is False

    assert store.upsert_session("s1", status="active", started=_ts(5)) is True
    assert session.name == "proj"
    assert session.cwd == "/repo"
    assert session.status == "active"
    assert session.started == _ts(5)

    assert store.upsert_session("s1", name="") is True
    assert session.name == ""


def test_apply_infers_source_sets_session_key_and_first_started() -> None:
    store = Store()
    assert store.apply([]) == set()

    changed = store.apply([
        Event(ts=_ts(1), session_key="s1", agent_key="claude:a1", kind=EventKind.TEXT, summary="hi"),
        Event(ts=_ts(2), session_key=STANDALONE_SESSION_KEY, agent_key="agy:c1", kind=EventKind.START),
    ])

    assert changed == {"s1", STANDALONE_SESSION_KEY}
    a1 = store.sessions["s1"].agents["claude:a1"]
    c1 = store.sessions[STANDALONE_SESSION_KEY].agents["agy:c1"]
    assert a1.source == Source.CLAUDE
    assert a1.session_key == "s1"
    assert a1.started == _ts(1)
    assert c1.source == Source.AGY
    assert c1.session_key == STANDALONE_SESSION_KEY
    assert c1.started == _ts(2)


def test_apply_model_title_last_update_and_last_seen() -> None:
    store = Store()
    store.apply([
        Event(
            ts=_ts(10),
            session_key="s1",
            agent_key="claude:a1",
            kind=EventKind.START,
            model="claude-opus-4",
            title="Explore",
        ),
        Event(
            ts=_ts(5),
            session_key="s1",
            agent_key="claude:a1",
            kind=EventKind.TEXT,
            model="",
            title=None,
            summary="early note",
        ),
    ])

    session = store.sessions["s1"]
    agent = session.agents["claude:a1"]
    assert agent.model == "claude-opus-4"
    assert agent.title == "Explore"
    assert session.last_update == _ts(10)
    assert store._last_seen["claude:a1"] == _ts(10)

    store.apply([
        Event(
            ts=_ts(12),
            session_key="s1",
            agent_key="claude:a1",
            kind=EventKind.TEXT,
            model="claude-sonnet-4",
            title="Edit",
        ),
    ])
    assert agent.model == "claude-sonnet-4"
    assert agent.title == "Edit"
    assert session.last_update == _ts(12)
    assert store._last_seen["claude:a1"] == _ts(12)


def test_apply_start_preserves_existing_started_and_runs() -> None:
    store = Store()
    store.apply([
        Event(ts=_ts(1), session_key="s1", agent_key="claude:a1", kind=EventKind.START),
        Event(ts=_ts(2), session_key="s1", agent_key="claude:a2", kind=EventKind.USAGE, tokens_in=10),
        Event(ts=_ts(4), session_key="s1", agent_key="claude:a2", kind=EventKind.START),
    ])

    a1 = store.sessions["s1"].agents["claude:a1"]
    a2 = store.sessions["s1"].agents["claude:a2"]
    assert a1.started == _ts(1)
    assert a1.status == Status.RUNNING
    assert a2.started == _ts(2)
    assert a2.status == Status.RUNNING


def test_apply_tool_start_tool_end_and_text() -> None:
    store = Store()
    store.apply([
        Event(ts=_ts(1), session_key="s1", agent_key="claude:a1", kind=EventKind.TOOL_START, summary="Read a.py"),
        Event(ts=_ts(2), session_key="s1", agent_key="claude:a1", kind=EventKind.TOOL_START, summary=""),
        Event(ts=_ts(3), session_key="s1", agent_key="claude:a2", kind=EventKind.TOOL_END, summary="Finished read"),
        Event(ts=_ts(4), session_key="s1", agent_key="claude:a3", kind=EventKind.TEXT, summary="Analyzing"),
        Event(ts=_ts(5), session_key="s1", agent_key="claude:a3", kind=EventKind.TEXT, summary=""),
    ])

    agents = store.sessions["s1"].agents
    assert agents["claude:a1"].status == Status.RUNNING
    assert agents["claude:a1"].calls == 2
    assert agents["claude:a1"].tool_times == [_ts(1), _ts(2)]
    assert agents["claude:a1"].last_action == "Read a.py"

    assert agents["claude:a2"].status == Status.RUNNING
    assert agents["claude:a2"].last_action == "Finished read"

    assert agents["claude:a3"].status == Status.RUNNING
    assert agents["claude:a3"].last_action == "Analyzing"


def test_apply_usage_counters_and_ctx_tokens() -> None:
    store = Store()
    store.apply([
        Event(
            ts=_ts(1),
            session_key="s1",
            agent_key="claude:a1",
            kind=EventKind.USAGE,
            tokens_in=100,
            tokens_out=40,
            ctx_tokens=500,
        ),
        Event(
            ts=_ts(2),
            session_key="s1",
            agent_key="claude:a1",
            kind=EventKind.USAGE,
            tokens_in=50,
            tokens_out=25,
            ctx_tokens=None,
        ),
        Event(
            ts=_ts(3),
            session_key="s1",
            agent_key="claude:a1",
            kind=EventKind.USAGE,
            tokens_in=10,
            tokens_out=5,
            ctx_tokens=800,
        ),
    ])

    agent = store.sessions["s1"].agents["claude:a1"]
    assert agent.status == Status.PENDING
    assert agent.in_tokens == 160
    assert agent.out_tokens == 70
    assert agent.ctx_tokens == 800


def test_apply_end_and_error() -> None:
    store = Store()
    store.apply([
        Event(ts=_ts(1), session_key="s1", agent_key="claude:a1", kind=EventKind.START),
        Event(ts=_ts(5), session_key="s1", agent_key="claude:a1", kind=EventKind.END),
        Event(ts=_ts(1), session_key="s1", agent_key="claude:a2", kind=EventKind.START),
        Event(ts=_ts(6), session_key="s1", agent_key="claude:a2", kind=EventKind.END, status=Status.FAILED),
        Event(ts=_ts(1), session_key="s1", agent_key="claude:a3", kind=EventKind.START),
        Event(ts=_ts(7), session_key="s1", agent_key="claude:a3", kind=EventKind.ERROR, summary="boom"),
        Event(ts=_ts(1), session_key="s1", agent_key="claude:a4", kind=EventKind.TEXT, summary="ok"),
        Event(ts=_ts(8), session_key="s1", agent_key="claude:a4", kind=EventKind.ERROR, summary=""),
    ])

    agents = store.sessions["s1"].agents
    assert agents["claude:a1"].status == Status.DONE
    assert agents["claude:a1"].ended == _ts(5)
    assert agents["claude:a2"].status == Status.FAILED
    assert agents["claude:a2"].ended == _ts(6)
    assert agents["claude:a3"].status == Status.FAILED
    assert agents["claude:a3"].ended == _ts(7)
    assert agents["claude:a3"].last_action == "boom"
    assert agents["claude:a4"].status == Status.FAILED
    assert agents["claude:a4"].last_action == "ok"


def test_apply_explicit_status_override() -> None:
    store = Store()
    store.apply([
        Event(ts=_ts(1), session_key="s1", agent_key="claude:a1", kind=EventKind.TEXT, status=Status.DONE),
    ])
    agent = store.sessions["s1"].agents["claude:a1"]
    assert agent.status == Status.DONE

    store.apply([
        Event(ts=_ts(2), session_key="s1", agent_key="claude:a1", kind=EventKind.USAGE, status=Status.RUNNING),
    ])
    assert agent.status == Status.RUNNING

    store.apply([
        Event(ts=_ts(3), session_key="s1", agent_key="claude:a1", kind=EventKind.TOOL_START, status=Status.PENDING),
    ])
    assert agent.status == Status.PENDING


def test_apply_terminal_statuses_sticky() -> None:
    store = Store()
    store.apply([
        Event(ts=_ts(1), session_key="s1", agent_key="claude:a1", kind=EventKind.START),
        Event(ts=_ts(2), session_key="s1", agent_key="claude:a1", kind=EventKind.END),
        Event(ts=_ts(3), session_key="s1", agent_key="claude:a1", kind=EventKind.START),
        Event(ts=_ts(4), session_key="s1", agent_key="claude:a1", kind=EventKind.TOOL_START, summary="late tool"),
        Event(ts=_ts(5), session_key="s1", agent_key="claude:a1", kind=EventKind.TOOL_END, summary="late end"),
        Event(ts=_ts(6), session_key="s1", agent_key="claude:a1", kind=EventKind.TEXT, summary="late text"),
        Event(
            ts=_ts(7),
            session_key="s1",
            agent_key="claude:a1",
            kind=EventKind.USAGE,
            tokens_in=30,
            tokens_out=20,
            ctx_tokens=900,
        ),
        Event(ts=_ts(1), session_key="s1", agent_key="claude:a2", kind=EventKind.ERROR, summary="err"),
        Event(ts=_ts(2), session_key="s1", agent_key="claude:a2", kind=EventKind.TOOL_START, summary="after err"),
    ])

    a1 = store.sessions["s1"].agents["claude:a1"]
    assert a1.status == Status.DONE
    assert a1.calls == 1
    assert a1.tool_times == [_ts(4)]
    assert a1.last_action == "late text"
    assert a1.in_tokens == 30
    assert a1.out_tokens == 20
    assert a1.ctx_tokens == 900

    a2 = store.sessions["s1"].agents["claude:a2"]
    assert a2.status == Status.FAILED
    assert a2.calls == 1
    assert a2.last_action == "after err"


def test_move_agent() -> None:
    store = Store()
    store.apply([
        Event(ts=_ts(1), session_key=STANDALONE_SESSION_KEY, agent_key="agy:1", kind=EventKind.START, title="Task"),
        Event(ts=_ts(2), session_key=STANDALONE_SESSION_KEY, agent_key="agy:1", kind=EventKind.TOOL_START, summary="Bash"),
        Event(ts=_ts(3), session_key=STANDALONE_SESSION_KEY, agent_key="agy:1", kind=EventKind.USAGE, tokens_out=99),
    ])

    affected = store.move_agent("agy:1", "claude-s1", parent_agent_key="claude:main")
    assert affected == {STANDALONE_SESSION_KEY, "claude-s1"}
    assert "agy:1" not in store.sessions[STANDALONE_SESSION_KEY].agents

    moved = store.sessions["claude-s1"].agents["agy:1"]
    assert moved.session_key == "claude-s1"
    assert moved.parent_agent_key == "claude:main"
    assert moved.title == "Task"
    assert moved.calls == 1
    assert moved.out_tokens == 99
    assert moved.status == Status.RUNNING

    assert store.move_agent("agy:1", "claude-s1", parent_agent_key="claude:main") == set()
    assert store.move_agent("agy:1", "claude-s1", parent_agent_key="claude:other") == {"claude-s1"}
    assert moved.parent_agent_key == "claude:other"

    assert store.move_agent("agy:missing", "new-sess") == set()
    assert "new-sess" not in store.sessions

    changed = store.apply([
        Event(ts=_ts(4), session_key=STANDALONE_SESSION_KEY, agent_key="agy:1", kind=EventKind.END),
    ])
    assert changed == {"claude-s1"}
    assert moved.status == Status.DONE
    assert "agy:1" not in store.sessions[STANDALONE_SESSION_KEY].agents


def test_find_agent() -> None:
    store = Store()
    assert store.find_agent("claude:missing") is None

    store.apply([
        Event(ts=_ts(1), session_key="s1", agent_key="claude:a1", kind=EventKind.START),
        Event(ts=_ts(2), session_key="s2", agent_key="agy:c1", kind=EventKind.START),
    ])

    found = store.find_agent("agy:c1")
    assert found is not None
    assert found.key == "agy:c1"
    assert found.session_key == "s2"
    assert store.find_agent("claude:missing") is None


def test_totals() -> None:
    store = Store()
    assert store.totals("missing") == {
        "agents_done": 0,
        "agents_total": 0,
        "calls": 0,
        "out_tokens": 0,
        "running": 0,
    }

    store.apply([
        Event(ts=_ts(1), session_key="s1", agent_key="claude:d1", kind=EventKind.TOOL_START),
        Event(ts=_ts(2), session_key="s1", agent_key="claude:d1", kind=EventKind.USAGE, tokens_out=100),
        Event(ts=_ts(3), session_key="s1", agent_key="claude:d1", kind=EventKind.END),
        Event(ts=_ts(1), session_key="s1", agent_key="claude:d2", kind=EventKind.END),
        Event(ts=_ts(1), session_key="s1", agent_key="claude:r1", kind=EventKind.TOOL_START),
        Event(ts=_ts(2), session_key="s1", agent_key="claude:r1", kind=EventKind.TOOL_START),
        Event(ts=_ts(3), session_key="s1", agent_key="claude:r1", kind=EventKind.USAGE, tokens_out=55),
        Event(ts=_ts(1), session_key="s1", agent_key="claude:p1", kind=EventKind.USAGE, tokens_out=10),
        Event(ts=_ts(1), session_key="s1", agent_key="claude:f1", kind=EventKind.ERROR),
    ])

    assert store.totals("s1") == {
        "agents_done": 2,
        "agents_total": 5,
        "calls": 3,
        "out_tokens": 165,
        "running": 1,
    }


def test_sessions_sorted() -> None:
    store = Store()
    store.upsert_session("s_none")
    store.apply([
        Event(ts=_ts(5), session_key="s_old", agent_key="claude:a1", kind=EventKind.START),
        Event(ts=_ts(99), session_key=STANDALONE_SESSION_KEY, agent_key="agy:1", kind=EventKind.START),
        Event(ts=_ts(15), session_key="s_new", agent_key="claude:a2", kind=EventKind.START),
    ])

    ordered = [session.key for session in store.sessions_sorted()]
    assert ordered == ["s_new", "s_old", "s_none", STANDALONE_SESSION_KEY]


def test_events_capped_and_bodies_truncated() -> None:
    store = Store()
    assert store.events("missing") == []
    assert store.event_count("missing") == 0

    big = "x" * 5_000_000
    total = config.EVENT_CAP + 10
    store.apply([
        Event(
            ts=_ts(0) + timedelta(seconds=i),
            session_key="s1",
            agent_key="claude:a1",
            kind=EventKind.TOOL_END,
            summary=f"step-{i}",
            body=big,
        )
        for i in range(total)
    ])

    evs = store.events("claude:a1")
    assert len(evs) == config.EVENT_CAP
    assert evs[0].summary == "step-10"
    assert evs[-1].summary == f"step-{total - 1}"
    assert len(evs[-1].body) <= config.BODY_CAP + 1 and evs[-1].body.endswith("…")
    assert store.event_count("claude:a1") == total

    store.apply([
        Event(
            ts=_ts(10),
            session_key="s1",
            agent_key="claude:a2",
            kind=EventKind.TEXT,
            body="short body",
        )
    ])
    assert store.events("claude:a2")[0].body == "short body"


def test_effort_and_tokens_accumulate() -> None:
    store = Store()
    store.apply([
        Event(
            ts=_ts(1),
            session_key="s1",
            agent_key="codex:r1",
            kind=EventKind.USAGE,
            effort="high",
            tokens_in=10,
            tokens_cached=4,
            tokens_out=3,
            tokens_reasoning=2,
        ),
        Event(
            ts=_ts(2),
            session_key="s1",
            agent_key="codex:r1",
            kind=EventKind.USAGE,
            tokens_in=5,
            tokens_out=1,
        ),
    ])

    a = store.find_agent("codex:r1")
    assert a is not None
    assert (a.effort, a.in_tokens, a.cached_tokens, a.out_tokens, a.reasoning_tokens) == (
        "high",
        15,
        4,
        4,
        2,
    )
    assert a.last_write == _ts(2)

    store.apply([
        Event(ts=_ts(3), session_key="s1", agent_key="codex:r1", kind=EventKind.TEXT, effort=""),
    ])
    assert a.effort == "high"
    assert a.last_write == _ts(3)

    store.apply([
        Event(ts=_ts(4), session_key="s1", agent_key="codex:r1", kind=EventKind.TEXT, effort="medium"),
    ])
    assert a.effort == "medium"
    assert a.last_write == _ts(4)


def test_dead_session_dropped_after_grace_only() -> None:
    store = Store()
    t0 = _ts(0)
    store.upsert_session("s1")
    store.apply([
        Event(ts=t0, session_key="s1", agent_key="claude:a1", kind=EventKind.START, body="hi"),
        Event(ts=t0, session_key=UNLINKED_SESSION_KEY, agent_key="agy:u1", kind=EventKind.START),
    ])

    assert store.drop_dead_sessions(t0, set(), 60) == set()
    assert store.drop_dead_sessions(t0 + timedelta(seconds=30), {"s1"}, 60) == set()
    assert store.drop_dead_sessions(t0 + timedelta(seconds=31), set(), 60) == set()
    assert store.drop_dead_sessions(t0 + timedelta(seconds=92), set(), 60) == {"s1"}

    assert "s1" not in store.sessions
    assert store.find_agent("claude:a1") is None
    assert store.event_count("claude:a1") == 0
    assert store.events("claude:a1") == []
    assert UNLINKED_SESSION_KEY in store.sessions
    assert store.find_agent("agy:u1") is not None


def test_finished_agent_kept_while_session_live() -> None:
    store = Store()
    t0 = _ts(0)
    store.apply([
        Event(ts=t0, session_key="s1", agent_key="claude:a1", kind=EventKind.END, status=Status.DONE),
    ])
    assert store.drop_dead_sessions(t0 + timedelta(hours=5), {"s1"}, 60) == set()
    agent = store.find_agent("claude:a1")
    assert agent is not None and agent.status is Status.DONE


def test_drop_finished_unlinked() -> None:
    store = Store()
    t0 = _ts(0)
    assert store.drop_finished_unlinked(t0, set(), 60) == set()

    store.apply([
        Event(ts=t0, session_key=UNLINKED_SESSION_KEY, agent_key="codex:run", kind=EventKind.START),
        Event(ts=t0, session_key=UNLINKED_SESSION_KEY, agent_key="agy:done", kind=EventKind.END),
        Event(ts=t0, session_key=UNLINKED_SESSION_KEY, agent_key="agy:moved", kind=EventKind.START),
    ])
    store.move_agent("agy:moved", "s1")

    assert store.drop_finished_unlinked(t0, {"codex:run"}, 60) == set()
    assert store.drop_finished_unlinked(t0 + timedelta(seconds=30), {"codex:run", "agy:done"}, 60) == set()
    assert store.drop_finished_unlinked(t0 + timedelta(seconds=31), {"codex:run"}, 60) == set()
    assert store.drop_finished_unlinked(t0 + timedelta(seconds=91), {"codex:run"}, 60) == {"agy:done"}

    assert store.find_agent("agy:done") is None
    assert store.event_count("agy:done") == 0
    assert store.find_agent("codex:run") is not None
    assert store.find_agent("agy:moved") is not None


def test_infer_source_is_main_and_activity_kinds() -> None:
    store = Store()
    main_key = main_agent_key("s1")
    store.apply([
        Event(ts=_ts(1), session_key="s1", agent_key=main_key, kind=EventKind.PROMPT, body="hello"),
        Event(ts=_ts(2), session_key="s1", agent_key="claude:sub1", kind=EventKind.THINKING, body="hmm"),
        Event(ts=_ts(3), session_key=UNLINKED_SESSION_KEY, agent_key="codex:c1", kind=EventKind.START),
        Event(ts=_ts(4), session_key=UNLINKED_SESSION_KEY, agent_key="agy:g1", kind=EventKind.START),
    ])

    main_agent = store.find_agent(main_key)
    sub_agent = store.find_agent("claude:sub1")
    codex_agent = store.find_agent("codex:c1")
    agy_agent = store.find_agent("agy:g1")

    assert main_agent is not None
    assert main_agent.source is Source.CLAUDE
    assert main_agent.is_main is True
    assert main_agent.status is Status.RUNNING
    assert main_agent.last_write == _ts(1)

    assert sub_agent is not None
    assert sub_agent.source is Source.CLAUDE
    assert sub_agent.is_main is False
    assert sub_agent.status is Status.RUNNING
    assert sub_agent.last_write == _ts(2)

    assert codex_agent is not None and codex_agent.source is Source.CODEX
    assert agy_agent is not None and agy_agent.source is Source.AGY


def test_reopen_after_end_clears_ended() -> None:
    store = Store()
    store.apply([
        Event(ts=_ts(0), session_key="s1", agent_key="codex:r1", kind=EventKind.START),
        Event(ts=_ts(5), session_key="s1", agent_key="codex:r1", kind=EventKind.END, status=Status.DONE),
    ])
    agent = store.find_agent("codex:r1")
    assert agent is not None
    assert agent.status is Status.DONE
    assert agent.ended == _ts(5)
    assert agent.duration_seconds(_ts(10)) == 300.0

    store.apply([
        Event(
            ts=_ts(7),
            session_key="s1",
            agent_key="codex:r1",
            kind=EventKind.PROMPT,
            status=Status.RUNNING,
            summary="continue",
        ),
    ])
    assert agent.status is Status.RUNNING
    assert agent.ended is None
    assert agent.duration_seconds(_ts(10)) == 600.0

    store.apply([
        Event(ts=_ts(12), session_key="s1", agent_key="agy:g1", kind=EventKind.ERROR, summary="stalled"),
    ])
    agy_agent = store.find_agent("agy:g1")
    assert agy_agent is not None
    assert agy_agent.status is Status.FAILED
    assert agy_agent.ended == _ts(12)

    store.apply([
        Event(ts=_ts(14), session_key="s1", agent_key="agy:g1", kind=EventKind.START, status=Status.RUNNING),
    ])
    assert agy_agent.status is Status.RUNNING
    assert agy_agent.ended is None


def test_reset_agent() -> None:
    store = Store()
    assert store.reset_agent("claude:missing") is False

    store.apply([
        Event(
            ts=_ts(1),
            session_key="s1",
            agent_key="claude:a1",
            kind=EventKind.START,
            title="Explore",
            model="claude-sonnet-5-5",
            effort="high",
        ),
        Event(
            ts=_ts(2),
            session_key="s1",
            agent_key="claude:a1",
            kind=EventKind.TOOL_START,
            summary="Read a.py",
        ),
        Event(
            ts=_ts(3),
            session_key="s1",
            agent_key="claude:a1",
            kind=EventKind.USAGE,
            tokens_in=100,
            tokens_cached=40,
            tokens_out=25,
            tokens_reasoning=5,
            ctx_tokens=100,
        ),
        Event(ts=_ts(4), session_key="s1", agent_key="claude:a1", kind=EventKind.END),
    ])
    assert store.reset_agent("claude:a1") is True
    agent = store.find_agent("claude:a1")
    assert agent is not None
    assert agent.status is Status.PENDING
    assert agent.started is None
    assert agent.ended is None
    assert agent.calls == 0
    assert agent.tool_times == []
    assert agent.last_action == ""
    assert (agent.in_tokens, agent.cached_tokens, agent.out_tokens, agent.reasoning_tokens) == (0, 0, 0, 0)
    assert agent.ctx_tokens is None
    assert agent.last_write is None
    assert store.events("claude:a1") == []
    assert store.event_count("claude:a1") == 0
