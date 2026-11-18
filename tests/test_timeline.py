"""Tests for the timeline view and its dashboard wiring."""

from __future__ import annotations

from datetime import timedelta

from test_dashboard import T0, Host, ev

from agent_watch.model import Agent, EventKind, Source, Status
from agent_watch.store import Store
from agent_watch.ui.dashboard import DashboardScreen
from agent_watch.ui.theme import STATUS_COLORS
from agent_watch.ui.timeline import TimelineRows, TimelineView, axis_text, bar_text, time_span
from agent_watch.ui.widgets import AgentCard, CardGrid

NOW = T0 + timedelta(minutes=10)


def agent(key="a", status=Status.DONE, start=0, end: int | None = 60, **kw) -> Agent:
    return Agent(
        key=key, source=Source.CLAUDE, session_key="s", title=f"Agent {key}", status=status,
        started=T0 + timedelta(seconds=start),
        ended=None if end is None else T0 + timedelta(seconds=end), **kw,
    )


def test_bar_covers_span_and_is_status_colored() -> None:
    a = agent(start=0, end=100, status=Status.FAILED)
    span = time_span([a], NOW)
    bar = bar_text(a, span, 20, NOW)
    assert bar.cell_len == 20
    assert bar.plain == "█" * 20
    assert {str(s.style) for s in bar.spans} == {STATUS_COLORS[Status.FAILED]}


def test_bar_position_scales_to_span() -> None:
    first = agent("a", start=0, end=50)
    second = agent("b", start=50, end=100)
    span = time_span([first, second], NOW)
    assert bar_text(first, span, 20, NOW).plain == "█" * 10 + " " * 10
    assert bar_text(second, span, 20, NOW).plain == " " * 10 + "█" * 10


def test_zero_length_agent_gets_half_block_and_span_never_zero() -> None:
    a = agent(start=5, end=5)
    span = time_span([a], NOW)
    assert span is not None and span[1] > span[0]
    bar = bar_text(a, span, 12, NOW)
    assert bar.cell_len == 12
    assert bar.plain.strip() in {"▌", "▐"}


def test_running_agent_extends_to_now_and_unstarted_has_no_bar() -> None:
    running = agent("r", status=Status.RUNNING, start=0, end=None)
    pending = Agent(key="p", source=Source.AGY, session_key="s")
    span = time_span([running, pending], NOW)
    assert span == (T0, NOW)
    assert bar_text(running, span, 10, NOW).plain == "█" * 10
    assert bar_text(pending, span, 10, NOW).plain == " " * 10
    assert time_span([pending], NOW) is None
    assert bar_text(pending, None, 10, NOW).cell_len == 10


def test_axis_has_ticks_and_exact_width() -> None:
    span = time_span([agent(start=0, end=3600)], NOW)
    axis = axis_text(span, 60)
    assert axis.cell_len == 60
    labels = axis.plain.split()
    assert len(labels) >= 3 and all(len(x) == 5 and x[2] == ":" for x in labels)
    assert axis_text(None, 30).plain == " " * 30
    assert axis_text(span, 3).cell_len == 3


def _store(n: int = 3) -> Store:
    store = Store()
    store.upsert_session("s1", name="proj")
    events = []
    for i in range(n):
        key = f"a{i:02d}"
        events.append(ev("s1", key, EventKind.START, i * 10, title=f"Worker {i}", model="claude-sonnet-4-5"))
        if i % 2 == 0:
            events.append(ev("s1", key, EventKind.END, i * 10 + 30, status=Status.DONE))
    store.apply(events)
    return store


async def test_timeline_replaces_grid_and_toggles_with_t_and_esc() -> None:
    app = Host(_store())
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        grid, timeline = screen.query_one(CardGrid), screen.query_one(TimelineView)
        assert grid.display and not timeline.display
        await pilot.press("t")
        await pilot.pause()
        assert screen.has_class("-timeline") and timeline.display and not grid.display
        assert timeline.keys == ["a00", "a01", "a02"]
        await pilot.press("escape")
        await pilot.pause()
        assert grid.display and not timeline.display
        await pilot.press("t", "t")
        await pilot.pause()
        assert grid.display and not timeline.display


async def test_timeline_updates_on_poll_and_respects_filter() -> None:
    store = _store(3)
    app = Host(store)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        screen = app.screen
        timeline = screen.query_one(TimelineView)
        await pilot.press("t")
        await pilot.pause()
        changed = store.apply([ev("s1", "a99", EventKind.START, 5, title="Late joiner")])
        screen.refresh_sessions(changed)
        await pilot.pause()
        assert timeline.keys == ["a00", "a99", "a01", "a02"]
        screen.filter_text = "late"
        screen._refresh_view()
        await pilot.pause()
        assert timeline.keys == ["a99"]


async def test_timeline_single_agent_and_zero_duration() -> None:
    store = Store()
    store.upsert_session("s1", name="proj")
    store.apply([
        ev("s1", "z", EventKind.START, 0, title="Instant"),
        ev("s1", "z", EventKind.END, 0, status=Status.DONE),
    ])
    app = Host(store)
    async with app.run_test(size=(100, 20)) as pilot:
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        rows = app.screen.query_one(TimelineRows)
        assert rows.option_count == 1
        assert app.screen.query_one(TimelineView).keys == ["z"]


async def test_timeline_many_agents_scrolls() -> None:
    app = Host(_store(60))
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        rows = app.screen.query_one(TimelineRows)
        assert rows.option_count == 60
        assert rows.virtual_size.height >= 60 > rows.size.height
        await pilot.press("end")
        await pilot.pause()
        assert rows.highlighted == 59


class OpenHost(Host):
    def on_agent_card_opened(self, message: AgentCard.Opened) -> None:
        self.messages.append(message)


async def test_enter_on_row_opens_conversation_like_a_card() -> None:
    app = OpenHost(_store(3))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        await pilot.press("down", "enter")
        await pilot.pause()
        opened = [m for m in app.messages if isinstance(m, AgentCard.Opened)]
        assert [m.agent_key for m in opened] == ["a01"]
        assert isinstance(app.screen, DashboardScreen)
        assert app.screen.selected_agent_key == "a01"
