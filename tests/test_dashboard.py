"""Tests for the dashboard screen using the Textual pilot."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from textual.app import App

from agent_watch.model import STANDALONE_SESSION_KEY, UNLINKED_SESSION_KEY, Event, EventKind, main_agent_key
from agent_watch.sources.codex import CodexLimits
from agent_watch.store import Store
from agent_watch.ui.dashboard import EMPTY_TEXT, DashboardScreen
from agent_watch.ui.widgets import AgentCard, AgentList, CardGrid, EventLog, QuotaPanel, TotalsBar

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def ev(session: str, agent: str, kind: EventKind, secs: int = 0, **kw) -> Event:
    return Event(ts=T0 + timedelta(seconds=secs), session_key=session, agent_key=agent, kind=kind, **kw)


def build_store() -> Store:
    store = Store()
    store.upsert_session("s1", name="proj")
    store.apply([
        ev("s1", "a1", EventKind.START, 0, title="Builder", model="claude-sonnet-4-5"),
        ev("s1", "a1", EventKind.TOOL_START, 1, tool="Bash", summary="ls"),
        ev(STANDALONE_SESSION_KEY, "agy:x", EventKind.START, 2, title="Nested", model="gemini-4-hydrogen-high"),
        ev(STANDALONE_SESSION_KEY, "agy:y", EventKind.START, 3, title="Solo", model="gemini-4-flash"),
    ])
    store.move_agent("agy:x", "s1", parent_agent_key="a1")
    return store


class Host(App):
    def __init__(self, store: Store) -> None:
        super().__init__()
        self.store = store
        self.messages: list[object] = []

    def on_mount(self) -> None:
        self.push_screen(DashboardScreen(self.store))

    def on_dashboard_screen_open_requested(self, m) -> None:
        self.messages.append(m)

    def on_dashboard_screen_quota_refresh_requested(self, m) -> None:
        self.messages.append(m)


async def test_tabs_cards_and_parent_label() -> None:
    app = Host(build_store())
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DashboardScreen)
        assert screen.tab_labels == ["1 proj (0/2)", "2 Unlinked (0/1)"]
        grid = screen.query_one(CardGrid)
        assert set(grid.cards) == {"a1", "agy:x"}
        assert "← Builder" in grid.cards["agy:x"].render_agent(None).plain
        assert "←" not in grid.cards["a1"].render_agent(None).plain
        assert "agents [0/2 done]" in screen.query_one(TotalsBar).render().plain


async def test_switch_tabs() -> None:
    app = Host(build_store())
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        grid = screen.query_one(CardGrid)
        await pilot.press("2")
        await pilot.pause()
        assert set(grid.cards) == {"agy:y"}
        await pilot.press("tab")  # wraps to first
        await pilot.pause()
        assert set(grid.cards) == {"a1", "agy:x"}
        await pilot.press("shift+tab")
        await pilot.pause()
        assert set(grid.cards) == {"agy:y"}
        assert len(screen.query_one(AgentList)._keys) == 1


async def test_refresh_sessions_updates_calls() -> None:
    store = build_store()
    app = Host(store)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        grid = screen.query_one(CardGrid)
        assert "● ls" in grid.cards["a1"].render_agent(None).plain
        changed = store.apply([ev("s1", "a1", EventKind.TOOL_START, 10, tool="Read", summary="Read foo.py")])
        screen.refresh_sessions(changed)
        await pilot.pause()
        assert "● Read foo.py" in grid.cards["a1"].render_agent(None).plain
        assert screen.tab_labels[0] == "1 proj (0/2)"


async def test_add_events_filters_by_session() -> None:
    app = Host(build_store())
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        log = screen.query_one(EventLog)
        screen.add_events([
            ev("s1", "a1", EventKind.TOOL_START, 5, summary="in-s1"),
            ev(STANDALONE_SESSION_KEY, "agy:y", EventKind.TEXT, 6, summary="in-solo"),
        ])
        await pilot.pause()
        assert len(log.lines) == 1
        await pilot.press("2")
        await pilot.pause()
        assert len(log.lines) == 1
        assert "in-solo" in "".join(s.text for s in log.lines[0])
        await pilot.press("1")
        await pilot.pause()
        assert "in-s1" in "".join(s.text for s in log.lines[0])


async def test_quota_panel_text() -> None:
    app = Host(build_store())
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.update_quota({"Gemini 5h": (0.8, None), "Argon 7d": (None, None)})
        await pilot.pause()
        plain = app.screen.query_one(QuotaPanel).render().plain
        assert "Gemini 5h" in plain and "80%" in plain and "n/a" in plain


async def test_empty_state() -> None:
    app = Host(Store())
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert screen.has_class("-empty")
        assert str(screen.query_one("#empty").render()) == EMPTY_TEXT
        assert "Codex" in EMPTY_TEXT
        assert screen.tab_labels == []


async def test_action_messages() -> None:
    app = Host(build_store())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r", "o")
        await pilot.pause()
        kinds = [type(m).__name__ for m in app.messages]
        assert kinds == ["QuotaRefreshRequested", "OpenRequested"]
        assert app.messages[1].agent_key in {"a1", "agy:x"}


async def test_compact_toggle() -> None:
    app = Host(build_store())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("v")
        assert app.screen.has_class("-compact")
        await pilot.press("v")
        assert not app.screen.has_class("-compact")


async def test_unlinked_group_name_and_no_main_parent_label() -> None:
    store = Store()
    store.upsert_session("s1", name="proj")
    store.upsert_session(UNLINKED_SESSION_KEY, name="Unlinked")
    main_key = main_agent_key("s1")
    store.apply([
        ev(UNLINKED_SESSION_KEY, "codex:r1", EventKind.START, 1, title="Worker", model="gpt-5.6-terra"),
        ev(UNLINKED_SESSION_KEY, "codex:r2", EventKind.START, 2, title="Loose", model="gpt-5.6-terra"),
        ev("s1", main_key, EventKind.START, 5, title="main", model="claude-sonnet-5-5"),
    ])
    store.move_agent("codex:r1", "s1", parent_agent_key=main_key)

    app = Host(store)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DashboardScreen)
        assert screen.tab_labels == ["1 proj (0/2)", "2 Unlinked (0/1)"]
        grid = screen.query_one(CardGrid)
        assert next(iter(grid.cards)) == main_key
        assert [c.agent.key for c in grid.query(AgentCard)][0] == main_key
        assert "←" not in grid.cards["codex:r1"].render_agent(None).plain
        assert "main" in grid.cards[main_key].render_agent(None).plain


async def test_main_card_first() -> None:
    store = Store()
    store.upsert_session("s1", name="proj")
    main_key = main_agent_key("s1")
    store.apply([
        ev("s1", "claude:sub1", EventKind.START, 0, title="Explore", model="claude-haiku-5-5"),
        ev("s1", main_key, EventKind.START, 10, title="main", model="claude-opus-5-5"),
    ])

    app = Host(store)
    async with app.run_test() as pilot:
        await pilot.pause()
        grid = app.screen.query_one(CardGrid)
        assert next(iter(grid.cards)) == main_key
        assert [c.agent.key for c in grid.query(AgentCard)][0] == main_key


async def test_codex_limits_preserved_across_quota_updates() -> None:
    app = Host(build_store())
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DashboardScreen)
        screen.update_quota({"Gemini 5h": (0.8, None)})
        screen.update_codex_limits(CodexLimits(used_percent=42.0, window_minutes=300, resets_at=T0))
        await pilot.pause()
        plain = screen.query_one(QuotaPanel).render().plain
        assert "Gemini 5h" in plain and "80%" in plain
        assert "codex  42% used" in plain and "resets" in plain

        screen.update_quota({"Gemini 5h": (0.7, None)})
        await pilot.pause()
        plain2 = screen.query_one(QuotaPanel).render().plain
        assert "70%" in plain2 and "codex  42% used" in plain2


async def test_agent_list_default_highlight_and_fallback() -> None:
    store = build_store()
    app = Host(store)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DashboardScreen)
        agent_list = screen.query_one(AgentList)
        assert agent_list.highlighted == 0
        assert agent_list.highlighted_option is not None
        assert agent_list.highlighted_option.id == "a1"

        screen.selected_agent_key = "agy:x"
        agent_list.highlighted = None
        screen._refresh_view()
        await pilot.pause()
        assert agent_list.highlighted == 1
        assert agent_list.highlighted_option is not None
        assert agent_list.highlighted_option.id == "agy:x"

        await pilot.press("2")
        await pilot.pause()
        assert agent_list.highlighted == 0
        assert agent_list.highlighted_option is not None
        assert agent_list.highlighted_option.id == "agy:y"

        agent_list.set_agents([])
        assert agent_list.highlighted is None
