"""Unit tests for agent_watch.model and agent_watch.config."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agent_watch import config
from agent_watch.model import (
    STANDALONE_SESSION_KEY,
    UNLINKED_SESSION_KEY,
    Agent,
    Event,
    EventKind,
    Session,
    ShellCall,
    Source,
    Status,
    effort_from_model,
    main_agent_key,
    truncate_body,
    utc,
)


def test_utc_parsing() -> None:
    dt_z = utc("2026-11-17T15:38:27Z")
    assert dt_z.tzinfo == timezone.utc
    assert dt_z == datetime(2026, 11, 17, 15, 38, 27, tzinfo=timezone.utc)

    dt_frac = utc("2026-11-17T15:38:27.123456Z")
    assert dt_frac.tzinfo == timezone.utc
    assert dt_frac == datetime(2026, 11, 17, 15, 38, 27, 123456, tzinfo=timezone.utc)

    dt_ms = utc("2026-11-17T15:38:27.5Z")
    assert dt_ms.tzinfo == timezone.utc
    assert dt_ms.microsecond == 500_000

    dt_offset = utc("2026-11-17T18:38:27+03:00")
    assert dt_offset.tzinfo == timezone.utc
    assert dt_offset == dt_z


def test_session_counters() -> None:
    empty = Session(key=STANDALONE_SESSION_KEY)
    assert empty.done_count() == 0
    assert empty.total_calls() == 0
    assert empty.total_out_tokens() == 0

    session = Session(
        key="sess-1",
        agents={
            "claude:a1": Agent(
                key="claude:a1",
                source=Source.CLAUDE,
                session_key="sess-1",
                status=Status.DONE,
                calls=4,
                out_tokens=150,
            ),
            "claude:a2": Agent(
                key="claude:a2",
                source=Source.CLAUDE,
                session_key="sess-1",
                status=Status.RUNNING,
                calls=2,
                out_tokens=80,
            ),
            "agy:c1": Agent(
                key="agy:c1",
                source=Source.AGY,
                session_key="sess-1",
                status=Status.DONE,
                calls=1,
                out_tokens=20,
            ),
            "claude:a3": Agent(
                key="claude:a3",
                source=Source.CLAUDE,
                session_key="sess-1",
                status=Status.FAILED,
                calls=3,
                out_tokens=50,
            ),
        },
    )
    assert session.done_count() == 2
    assert session.total_calls() == 10
    assert session.total_out_tokens() == 300


def test_agent_duration_seconds() -> None:
    t0 = utc("2026-11-17T15:00:00Z")
    t1 = t0 + timedelta(seconds=42.5)
    t2 = t0 + timedelta(seconds=10)

    unstarted = Agent(key="claude:0", source=Source.CLAUDE, session_key="s")
    assert unstarted.duration_seconds() is None

    running = Agent(key="claude:1", source=Source.CLAUDE, session_key="s", started=t0)
    assert running.duration_seconds(now=t1) == 42.5
    assert running.duration_seconds() is not None

    finished = Agent(
        key="claude:2",
        source=Source.CLAUDE,
        session_key="s",
        started=t0,
        ended=t2,
    )
    assert finished.duration_seconds(now=t1) == 10.0


def test_model_color() -> None:
    assert config.model_color("gemini-4-hydrogen-high") == "#6ea8fe"
    assert config.model_color("claude-opus-4-6") == "#c792ea"
    assert config.model_color("unknown") == "#9aa0a6"


def test_env_override_agy_home(monkeypatch, tmp_path) -> None:
    custom_agy = tmp_path / "custom-agy"
    monkeypatch.setenv("AGY_HOME", str(custom_agy))
    assert config.agy_brain_dir() == custom_agy / "brain"
    assert not hasattr(config, "agy_metadata_file")

    custom_claude = tmp_path / "custom-claude"
    monkeypatch.setenv("CLAUDE_HOME", str(custom_claude))
    assert config.claude_projects_dir() == custom_claude / "projects"
    assert config.claude_sessions_dir() == custom_claude / "sessions"


def test_event_and_shell_call_models() -> None:
    ts = utc("2026-11-17T15:38:27Z")
    ev = Event(ts=ts, session_key="s", agent_key="claude:1", kind=EventKind.START)
    assert ev.tokens_in == 0
    assert ev.summary == ""

    call = ShellCall(
        session_id="s",
        agent_key="claude:1",
        tool_use_id="tu-1",
        ts_start=ts,
        ts_end=None,
        cwd="/tmp",
        command="pytest",
    )
    assert call.ts_end is None
    assert call.command == "pytest"


def test_effort_from_model() -> None:
    assert effort_from_model("gemini-4-hydrogen-xhigh") == "xhigh"
    assert effort_from_model("gpt-5.6-terra") == ""
    assert effort_from_model("claude-opus-4-6") == ""
    assert effort_from_model("") == ""
    assert effort_from_model("high") == ""
    for level in ("minimal", "low", "medium", "high", "xhigh", "max"):
        assert effort_from_model(f"model-{level}") == level


def test_main_agent_key_and_v2_model_fields() -> None:
    assert main_agent_key("sess-1") == "sess-1:main"
    assert not main_agent_key("sess-1").startswith("claude:")
    assert UNLINKED_SESSION_KEY == STANDALONE_SESSION_KEY
    assert Source.CODEX == "codex"
    assert EventKind.PROMPT == "prompt"
    assert EventKind.THINKING == "thinking"

    ev = Event(ts=utc("2026-11-17T15:38:27Z"), session_key="s", agent_key="codex:1", kind=EventKind.PROMPT)
    assert ev.body == ""
    assert ev.effort is None
    assert ev.tokens_cached == 0
    assert ev.tokens_reasoning == 0

    agent = Agent(key="codex:1", source=Source.CODEX, session_key="s")
    assert agent.effort == ""
    assert agent.is_main is False
    assert agent.cached_tokens == 0
    assert agent.reasoning_tokens == 0
    assert agent.last_write is None


def test_truncate_body(monkeypatch) -> None:
    assert truncate_body("hello", cap=5) == "hello"
    assert truncate_body("hello!", cap=5) == "hell…"
    assert truncate_body("hello", cap=1) == "…"
    assert truncate_body("hello", cap=0) == ""
    assert truncate_body("", cap=0) == ""

    monkeypatch.setattr(config, "BODY_CAP", 4)
    assert truncate_body("abcd") == "abcd"
    assert truncate_body("abcde") == "abc…"
