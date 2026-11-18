"""Tests for the dashboard screen using the Textual pilot."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from textual.app import App
from textual.color import Color
from textual.geometry import Region
from textual.widgets import Static

from agent_watch.model import STANDALONE_SESSION_KEY, UNLINKED_SESSION_KEY, Event, EventKind, Status, main_agent_key
from agent_watch.sources.codex import CodexLimits
from agent_watch.store import Store
from agent_watch.ui.dashboard import EMPTY_TEXT, DashboardScreen, TabBar
from agent_watch.ui.theme import ACCENT, BG, BORDER, DIM, MUTED, STATUS_COLORS, SURFACE, SURFACE_HI, TEXT
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
        assert "agents 0/2 done" in screen.query_one(TotalsBar).render().plain


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
        assert "main" in str(grid.cards[main_key].border_title)


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


async def test_header_pill_tabs_and_layout_styles() -> None:
    app = Host(build_store())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DashboardScreen)

        header = screen.query_one("#header", Static).content
        assert header.plain.startswith("◆ agent watch · ")
        assert header.plain.endswith(" · ● live")
        header_styles = [str(span.style) for span in header.spans]
        assert ACCENT in header_styles
        assert f"bold {TEXT}" in header_styles
        assert STATUS_COLORS[Status.DONE] in header_styles
        assert MUTED in header_styles

        tabs = screen.query_one(TabBar).content
        assert tabs.plain == " 1 proj (0/2)    2 Unlinked (0/1) "
        tab_styles = [str(span.style) for span in tabs.spans]
        assert not any("underline" in style for style in tab_styles)
        assert f"bold {ACCENT} on {SURFACE_HI}" in tab_styles
        assert f"bold {TEXT} on {SURFACE_HI}" in tab_styles
        assert DIM in tab_styles
        assert MUTED in tab_styles

        assert screen.styles.background.hex.lower() == BG
        assert screen.query_one("#sidebar").styles.background.hex.lower() == SURFACE
        assert screen.query_one("#sidebar").styles.border_right == ("solid", Color.parse(BORDER))
        assert screen.query_one("#grid").styles.background.hex.lower() == BG
        assert screen.query_one("#events").styles.background.hex.lower() == BG
        assert screen.query_one("#events").styles.border_top == ("solid", Color.parse(BORDER))


async def test_tab_click_selects_tab_without_underline() -> None:
    app = Host(build_store())
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DashboardScreen)
        tabs = screen.query_one(TabBar)
        assert not any("underline" in str(span.style) for span in tabs.content.spans)
        assert len(tabs.ranges) == len(screen.tab_keys) >= 2
        start, end = tabs.ranges[1]
        await pilot.click(TabBar, offset=(start + 2, 0))
        await pilot.pause()
        assert screen.selected_key == screen.tab_keys[1]
        line = tabs.render_lines(Region(0, 0, 140, 1))[0]
        assert not any(seg.style and seg.style.underline for seg in line)


def build_filter_store() -> Store:
    store = Store()
    store.upsert_session("s1", name="proj")
    store.upsert_session("s2", name="other")
    store.apply([
        ev("s1", "a1", EventKind.START, 0, title="Builder", model="claude-sonnet-4-5"),
        ev("s1", "a2", EventKind.START, 1, title="Reviewer", model="claude-opus-4-5"),
        ev("s1", "a2", EventKind.END, 5, status=Status.DONE),
        ev("s1", "agy:z", EventKind.START, 2, title="Gemini job", model="gemini-4-flash"),
        ev("s1", "a3", EventKind.START, 3, title="Builder two", model="claude-sonnet-4-5"),
        ev("s1", "a3", EventKind.END, 6, status=Status.FAILED),
        ev("s2", "b1", EventKind.START, 4, title="Builder other", model="claude-sonnet-4-5"),
    ])
    return store


async def test_filter_matches_title_source_status_model_and_ands_terms() -> None:
    app = Host(build_filter_store())
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        grid = screen.query_one(CardGrid)
        await pilot.press("slash")
        assert screen.has_class("-filtering")
        for text, expected in [
            ("BUILDER", {"a1", "a3"}),
            ("agy", {"agy:z"}),
            ("done", {"a2"}),
            ("failed", {"a3"}),
            ("opus", {"a2"}),
            ("builder failed", {"a3"}),
            ("builder opus", set()),
        ]:
            screen.query_one("#filter-input").value = text
            await pilot.pause()
            assert set(grid.cards) == expected, text
            assert set(screen.query_one(AgentList)._keys) == expected, text
        screen.query_one("#filter-input").value = "builder"
        await pilot.pause()
        count = screen.query_one("#filter-count").render().plain
        assert count == "filter: builder · 2/4"
        # totals still reflect the whole tab
        assert "agents 1/4 done" in screen.query_one(TotalsBar).render().plain


async def test_filter_main_agent_only_if_matches() -> None:
    store = build_filter_store()
    store.apply([ev("s1", main_agent_key("s1"), EventKind.START, 0, title="proj", model="claude-opus-4-5")])
    app = Host(store)
    async with app.run_test() as pilot:
        await pilot.pause()
        grid = app.screen.query_one(CardGrid)
        assert main_agent_key("s1") in grid.cards
        await pilot.press("slash")
        app.screen.query_one("#filter-input").value = "builder"
        await pilot.pause()
        assert main_agent_key("s1") not in grid.cards
        app.screen.query_one("#filter-input").value = "main"
        await pilot.pause()
        assert set(grid.cards) == {main_agent_key("s1")}


async def test_filter_escape_clears_and_closes() -> None:
    app = Host(build_filter_store())
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        grid = screen.query_one(CardGrid)
        await pilot.press("slash", "a", "g", "y")
        await pilot.pause()
        assert set(grid.cards) == {"agy:z"}
        await pilot.press("escape")
        await pilot.pause()
        assert not screen.has_class("-filtering")
        assert screen.filter_text == ""
        assert set(grid.cards) == {"a1", "a2", "agy:z", "a3"}


async def test_filter_survives_enter_polls_and_tab_switch() -> None:
    store = build_filter_store()
    app = Host(store)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        grid = screen.query_one(CardGrid)
        await pilot.press("slash", "b", "u", "i", "l", "d", "enter")
        await pilot.pause()
        assert screen.has_class("-filtering")
        assert set(grid.cards) == {"a1", "a3"}
        # live poll adds a matching and a non-matching agent
        changed = store.apply([
            ev("s1", "a4", EventKind.START, 20, title="Builder three", model="claude-sonnet-4-5"),
            ev("s1", "a5", EventKind.START, 21, title="Other", model="claude-sonnet-4-5"),
        ])
        screen.refresh_sessions(changed)
        await pilot.pause()
        assert set(grid.cards) == {"a1", "a3", "a4"}
        assert screen.query_one("#filter-count").render().plain == "filter: build · 3/6"
        # global filter: applies to the other tab too
        await pilot.press("2")
        await pilot.pause()
        assert screen.filter_text == "build"
        assert set(grid.cards) == {"b1"}
        await pilot.press("1")
        await pilot.pause()
        assert set(grid.cards) == {"a1", "a3", "a4"}
