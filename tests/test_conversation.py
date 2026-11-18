"""Tests for the read-only ConversationScreen and event rendering."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console
from test_app import make_app, wait_until
from test_claude import SID
from test_poller import SUB_ID
from textual.app import App
from textual.widgets import Static

from agent_watch import config
from agent_watch.app import AgentWatchApp
from agent_watch.model import Event, EventKind, Status, main_agent_key
from agent_watch.store import Store
from agent_watch.ui.conversation import ConversationScreen, TranscriptLog, render_event
from agent_watch.ui.dashboard import DashboardScreen
from agent_watch.ui.theme import STATUS_COLORS
from agent_watch.ui.widgets import AgentCard, AgentList, CardGrid

T0 = datetime(2026, 11, 17, 16, 0, 0, tzinfo=timezone.utc)


def ev(
    kind: EventKind,
    secs: int = 0,
    session: str = "s1",
    agent: str = "claude:a1",
    **kw,
) -> Event:
    return Event(
        ts=T0 + timedelta(seconds=secs),
        session_key=session,
        agent_key=agent,
        kind=kind,
        **kw,
    )


def _log_text(screen: ConversationScreen) -> str:
    log = screen.query_one(TranscriptLog)
    return "\n".join("".join(seg.text for seg in line) for line in log.lines)


def _cards_ready(app: AgentWatchApp, agent_key: str | None = None) -> bool:
    if not app.first_poll_done or not isinstance(app.screen, DashboardScreen):
        return False
    cards = list(app.screen.query(AgentCard))
    if not cards:
        return False
    if agent_key is not None:
        card = next((c for c in cards if c.agent.key == agent_key), None)
        return card is not None and card.is_mounted and card.region.area > 0
    return all(c.is_mounted and c.region.area > 0 for c in cards)


class _ConvHost(App):
    def __init__(self, store: Store, agent_key: str) -> None:
        super().__init__()
        self.store = store
        self.agent_key = agent_key

    def on_mount(self) -> None:
        self.push_screen(ConversationScreen(self.store, self.agent_key))


def test_render_event_kinds() -> None:
    assert render_event(ev(EventKind.START)) is None
    assert render_event(ev(EventKind.USAGE, tokens_in=100, tokens_out=50)) is None

    c = Console(record=True, width=100, force_terminal=True, color_system="truecolor")

    c.print(render_event(ev(EventKind.PROMPT, body="run [all] checks")))
    prompt_out = c.export_text()
    assert "▶ to agent" in prompt_out
    assert "run [all] checks" in prompt_out

    c.print(render_event(ev(EventKind.TEXT, body="Result is **ok**", model="claude-sonnet-5-5")))
    text_out = c.export_text()
    assert "◀ agent" in text_out
    assert "Result is ok" in text_out
    assert "**ok**" not in text_out

    c.print(render_event(ev(EventKind.THINKING, body="weighing options")))
    think_out = c.export_text()
    assert "… thinking" in think_out
    assert "weighing options" in think_out

    long_args = "A" * 500 + "Z" * 120
    c.print(render_event(ev(EventKind.TOOL_START, tool="Bash", summary="Bash pytest", body=long_args)))
    tstart_out = c.export_text().replace("\n", "")
    assert "⚙ Bash pytest" in tstart_out
    assert "A" * 500 in tstart_out
    assert "Z" not in tstart_out

    long_res = "B" * 800 + "Q" * 120
    c.print(render_event(ev(EventKind.TOOL_END, tool="Bash", body=long_res)))
    tend_out = c.export_text().replace("\n", "")
    assert "↳ result" in tend_out
    assert "B" * 800 in tend_out
    assert "Q" not in tend_out

    err_renderable = render_event(ev(EventKind.ERROR, summary="crashed", body="traceback"))
    assert err_renderable is not None
    c.print(err_renderable)
    err_out = c.export_text()
    assert "crashed" in err_out and "traceback" in err_out
    assert any(
        seg.style is not None and STATUS_COLORS[Status.FAILED] in str(seg.style)
        for seg in c.render(err_renderable)
    )

    c.print(render_event(ev(EventKind.END, status=Status.DONE, summary="all good")))
    end_out = c.export_text()
    assert "✓" in end_out and "done" in end_out and "all good" in end_out

    err_tend = render_event(ev(EventKind.TOOL_END, tool="Bash", summary="error: exit 1", body="stderr"))
    assert err_tend is not None
    c.print(err_tend)
    err_tend_out = c.export_text()
    assert "↳ result · error: exit 1" in err_tend_out and "stderr" in err_tend_out
    assert any(
        seg.style is not None and STATUS_COLORS[Status.FAILED] in str(seg.style)
        for seg in c.render(err_tend)
    )

    failed_end = render_event(ev(EventKind.END, status=Status.FAILED, summary="aborted", body="detail"))
    assert failed_end is not None
    c.print(failed_end)
    failed_end_out = c.export_text()
    assert "✗" in failed_end_out and "failed" in failed_end_out and "aborted" in failed_end_out
    assert any(
        seg.style is not None and STATUS_COLORS[Status.FAILED] in str(seg.style)
        for seg in c.render(failed_end)
    )


def test_empty_thinking_marker() -> None:
    c = Console(record=True, width=80)
    c.print(render_event(ev(EventKind.THINKING, body="")))
    assert "text not stored" in c.export_text()


async def test_header_shows_card_fields_and_full_tokens() -> None:
    store = Store()
    store.upsert_session("s1", name="proj")
    main_key = main_agent_key("s1")
    store.apply([
        ev(EventKind.START, 0, agent=main_key, title="main", model="claude-opus-5-5"),
        ev(EventKind.START, 1, agent="claude:p1", title="Planner", model="claude-sonnet-5-5"),
        ev(
            EventKind.START,
            2,
            agent="agy:c1",
            title="Worker",
            model="gemini-4-hydrogen-xhigh",
            effort="xhigh",
        ),
        ev(
            EventKind.USAGE,
            3,
            agent="agy:c1",
            tokens_in=12_000,
            tokens_cached=4_000,
            tokens_out=2_500,
            tokens_reasoning=700,
        ),
        ev(EventKind.TOOL_START, 5, agent="agy:c1", tool="Read", summary="Read foo.py"),
        ev(EventKind.END, 12, agent="agy:c1", status=Status.DONE),
    ])
    store.move_agent("agy:c1", "s1", parent_agent_key="claude:p1")

    app = _ConvHost(store, "agy:c1")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        header = str(app.screen.query_one("#conv-header", Static).render())
        assert "Worker" in header
        assert "← Planner" in header
        assert "agy" in header
        assert "hydrogen" in header
        assert "xhigh" in header
        assert "done" in header
        assert "0:10" in header
        assert "1 calls" in header
        assert "Read foo.py" in header
        assert "in 12k" in header
        assert "cached 4k" in header
        assert "out 2.5k" in header
        assert "reasoning 700" in header


async def test_header_collapses_multiline_last_action() -> None:
    store = Store()
    store.upsert_session("s1", name="proj")
    store.apply([
        ev(EventKind.START, 0, title="Worker", model="claude-sonnet-5-5"),
        ev(EventKind.TOOL_START, 1, tool="Bash", summary="line one\n  line two\tline three"),
    ])
    app = _ConvHost(store, "claude:a1")
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        header_text = str(app.screen.query_one("#conv-header", Static).render())
        assert "line one line two line three" in header_text
        assert len(header_text.splitlines()) == 3


async def test_refresh_events_noop_before_mount() -> None:
    store = Store()
    store.apply([
        ev(EventKind.START, 0, title="Worker"),
        ev(EventKind.TEXT, 1, body="hello"),
    ])
    screen = ConversationScreen(store, "claude:a1")
    screen.refresh_events()
    assert screen._seen_count == 0

    app = App()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.push_screen(screen)
        screen.refresh_events()
        await pilot.pause()
        assert screen._seen_count == 2
        assert "hello" in _log_text(screen)


async def test_startup_enter_opens_first_agent(tmp_path: Path) -> None:
    app, _ = make_app(tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        assert await wait_until(pilot, lambda: _cards_ready(app), 5.0)
        assert isinstance(app.screen, DashboardScreen)
        agent_list = app.screen.query_one(AgentList)
        assert app.screen.focused is agent_list
        assert agent_list.highlighted == 0
        expected_key = str(agent_list.get_option_at_index(0).id)

        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConversationScreen)
        assert app.screen.agent_key == expected_key


async def test_opens_on_enter_and_closes_on_escape(tmp_path: Path) -> None:
    app, _ = make_app(tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        assert await wait_until(
            pilot,
            lambda: _cards_ready(app),
            5.0,
        )
        assert isinstance(app.screen, DashboardScreen)

        # Open by clicking an AgentCard, then close with Escape.
        first_card = app.screen.query(AgentCard).first()
        await pilot.click(first_card, offset=(2, 2))
        await pilot.pause()
        assert isinstance(app.screen, ConversationScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)

        # Open by focusing an AgentCard and pressing Enter.
        first_card = app.screen.query(AgentCard).first()
        first_card.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConversationScreen)
        assert app.screen.agent_key == first_card.agent.key
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)

        # Open by selecting an AgentList row and pressing Enter.
        agent_list = app.screen.query_one(AgentList)
        target_idx = 1 if agent_list.option_count > 1 else 0
        expected_key = str(agent_list.get_option_at_index(target_idx).id)
        agent_list.focus()
        agent_list.highlighted = target_idx
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConversationScreen)
        assert app.screen.agent_key == expected_key
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, DashboardScreen)


async def test_no_input_widgets() -> None:
    store = Store()
    store.apply([
        ev(EventKind.START, 0, title="ReadOnly"),
        ev(EventKind.PROMPT, 1, body="<prompt>"),
    ])
    app = _ConvHost(store, "claude:a1")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, ConversationScreen)
        assert not app.screen.query("Input, TextArea")
        assert not app.screen.query("Button")


async def test_follows_tail_until_scrolled_up() -> None:
    store = Store()
    store.apply([
        ev(EventKind.TEXT, i, body=f"turn-{i}")
        for i in range(40)
    ])
    app = _ConvHost(store, "claude:a1")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ConversationScreen)
        log = screen.query_one(TranscriptLog)
        assert log.max_scroll_y > 10
        assert log.scroll_y == log.max_scroll_y

        # Appending while at bottom keeps tail visible.
        store.apply([ev(EventKind.TEXT, i, body=f"turn-{i}") for i in range(40, 45)])
        screen.refresh_events()
        await pilot.pause()
        assert log.scroll_y == log.max_scroll_y
        assert "turn-44" in _log_text(screen)

        # Scrolling up disables tail following.
        await pilot.press("pageup")
        await pilot.wait_for_scheduled_animations()
        await pilot.pause()
        paused_y = log.scroll_y
        assert paused_y < log.max_scroll_y
        assert not log.following

        store.apply([ev(EventKind.TEXT, i, body=f"turn-{i}") for i in range(45, 50)])
        screen.refresh_events()
        await pilot.pause()
        assert log.scroll_y == paused_y
        assert log.scroll_y < log.max_scroll_y

        # Returning to bottom resumes tail following.
        await pilot.press("end")
        await pilot.wait_for_scheduled_animations()
        await pilot.pause()
        assert log.scroll_y == log.max_scroll_y
        assert log.following

        store.apply([ev(EventKind.TEXT, i, body=f"turn-{i}") for i in range(50, 55)])
        screen.refresh_events()
        await pilot.pause()
        assert log.scroll_y == log.max_scroll_y


async def test_refresh_events_mid_animation_after_end_keeps_following() -> None:
    store = Store()
    store.apply([
        ev(EventKind.TEXT, i, body=f"turn-{i}")
        for i in range(60)
    ])
    app = _ConvHost(store, "claude:a1")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ConversationScreen)
        log = screen.query_one(TranscriptLog)
        assert log.max_scroll_y > 20

        await pilot.press("home")
        await pilot.wait_for_scheduled_animations()
        await pilot.pause()
        assert log.scroll_y == 0
        assert not log.following

        app.simulate_key("end")
        assert await wait_until(
            pilot,
            lambda: (
                app.animator.is_being_animated(log, "scroll_y")
                and 0 < round(log.scroll_y) < log.max_scroll_y
            ),
            1.5,
        )
        assert log.following

        store.apply([ev(EventKind.TEXT, i, body=f"turn-{i}") for i in range(60, 65)])
        screen.refresh_events()
        assert log.following
        await pilot.wait_for_scheduled_animations()
        await pilot.pause()

        assert log.following
        assert log.scroll_y == log.max_scroll_y
        assert "turn-64" in _log_text(screen)


async def test_incremental_append_after_event_cap() -> None:
    store = Store()
    total = config.EVENT_CAP + 5
    store.apply([
        ev(EventKind.TEXT, i, body=f"msg-{i}")
        for i in range(total)
    ])
    app = _ConvHost(store, "claude:a1")
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ConversationScreen)
        assert store.event_count("claude:a1") == total
        assert screen._seen_count == total
        lines_before = len(screen.query_one(TranscriptLog).lines)

        store.apply([
            ev(EventKind.TEXT, total + 1, body="after-cap-one"),
            ev(EventKind.TEXT, total + 2, body="after-cap-two"),
            ev(EventKind.TEXT, total + 3, body="after-cap-three"),
        ])
        screen.refresh_events()
        await pilot.pause()

        assert screen._seen_count == total + 3
        assert len(screen.query_one(TranscriptLog).lines) > lines_before
        rendered = _log_text(screen)
        assert rendered.count("after-cap-one") == 1
        assert rendered.count("after-cap-two") == 1
        assert rendered.count("after-cap-three") == 1


async def test_live_poll_refreshes_open_conversation(tmp_path: Path) -> None:
    app, claude_home = make_app(tmp_path)
    app.poll_seconds = 0.05
    async with app.run_test(size=(160, 50)) as pilot:
        target_key = f"claude:{SUB_ID}"
        assert await wait_until(
            pilot,
            lambda: _cards_ready(app, target_key),
            5.0,
        )
        # Focus + Enter instead of a click: a grid reflow between query and click
        # could land the click on a different card.
        card = app.screen.query_one(CardGrid).cards[target_key]
        card.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConversationScreen)
        assert app.screen.agent_key == target_key

        path = next(claude_home.glob(f"projects/*/{SID}/subagents/agent-{SUB_ID}.jsonl"))
        line = {
            "type": "assistant",
            "timestamp": "2026-11-17T16:59:00Z",
            "message": {
                "role": "assistant",
                "model": "claude-haiku-5-5",
                "content": [{
                    "type": "tool_use",
                    "id": "toolu_conv_live",
                    "name": "Grep",
                    "input": {"pattern": "live-needle"},
                }],
            },
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")

        assert await wait_until(
            pilot,
            lambda: "Grep live-needle" in _log_text(app.screen),
            5.0,
        )


def _make_snapshot_app(tmp_path: Path) -> AgentWatchApp:
    now = datetime.now(timezone.utc)
    codex_home = tmp_path / "codex"
    rid = "00000000-0000-0000-0000-000000000999"
    rollout = (
        codex_home
        / "sessions"
        / f"{now:%Y}"
        / f"{now:%m}"
        / f"{now:%d}"
        / f"rollout-2026-11-17T16-00-00-{rid}.jsonl"
    )
    rollout.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "timestamp": "2026-11-17T16:00:00Z",
            "type": "session_meta",
            "payload": {"id": rid, "timestamp": "2026-11-17T16:00:00Z", "cwd": "C:\\work\\demo"},
        },
        {
            "timestamp": "2026-11-17T16:00:00Z",
            "type": "turn_context",
            "payload": {"cwd": "C:\\work\\demo", "model": "gpt-5.6-terra", "effort": "high"},
        },
        {
            "timestamp": "2026-11-17T16:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "<prompt>"},
        },
        {
            "timestamp": "2026-11-17T16:00:02Z",
            "type": "response_item",
            "payload": {"type": "reasoning", "summary": [], "encrypted_content": "opaque"},
        },
        {
            "timestamp": "2026-11-17T16:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "arguments": '{"command": "<cmd>"}',
                "call_id": "call_1",
            },
        },
        {
            "timestamp": "2026-11-17T16:00:04Z",
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "call_1", "output": "<result>"},
        },
        {
            "timestamp": "2026-11-17T16:00:05Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "Done: **verified** `app.py`."},
        },
        {
            "timestamp": "2026-11-17T16:00:06Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 12000,
                        "cached_input_tokens": 4000,
                        "output_tokens": 850,
                        "reasoning_output_tokens": 300,
                    }
                },
            },
        },
        {
            "timestamp": "2026-11-17T16:00:07Z",
            "type": "event_msg",
            "payload": {"type": "task_complete"},
        },
    ]
    rollout.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return AgentWatchApp(
        claude_home=tmp_path / "claude",
        agy_home=tmp_path / "agy",
        codex_home=codex_home,
        no_quota=True,
        window_hours=24 * 365,
        poll_ms=60_000,
        proc_provider=lambda: [SimpleNamespace(cmdline=["codex.exe", rid])],
    )


def test_snapshot(snap_compare, tmp_path: Path) -> None:
    app = _make_snapshot_app(tmp_path)

    async def _wait_for_poll(pilot) -> None:
        assert await wait_until(
            pilot,
            lambda: _cards_ready(app),
            5.0,
        )

    assert snap_compare(app, press=["enter"], run_before=_wait_for_poll)
