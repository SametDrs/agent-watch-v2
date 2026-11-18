"""Tests for dashboard theme helpers and widgets."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from textual.app import App, ComposeResult

from agent_watch import config
from agent_watch.model import Agent, Event, EventKind, Source, Status, main_agent_key
from agent_watch.sources.codex import CodexLimits
from agent_watch.ui.theme import STATUS_COLORS, STATUS_ICONS, fmt_duration, fmt_tokens, short_model
from agent_watch.ui.theme import DIM
from agent_watch.ui.widgets import AgentCard, AgentList, CardGrid, EventLog, QuotaPanel, TotalsBar

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def make_agent(key="a1", status=Status.RUNNING, **kw) -> Agent:
    base = dict(
        key=key, source=Source.CLAUDE, session_key="s", title=f"Agent {key}", model="claude-sonnet-5-5",
        status=status, started=T0, calls=34, in_tokens=80_000, out_tokens=2_000, ctx_tokens=82_000,
        last_action="Bash Run auth tests",
        tool_times=[T0 + timedelta(seconds=i * 10) for i in range(10)],
    )
    base.update(kw)
    return Agent(**base)


def text_of(widget) -> str:
    return str(widget.render())


def test_fmt_duration():
    assert fmt_duration(None) == "–"
    assert fmt_duration(0) == "0:00"
    assert fmt_duration(42) == "0:42"
    assert fmt_duration(580) == "9:40"
    assert fmt_duration(3723) == "1:02:03"
    assert fmt_duration(-5) == "0:00"


def test_fmt_tokens():
    assert fmt_tokens(None) == "–"
    assert fmt_tokens(950) == "950"
    assert fmt_tokens(53_000) == "53k"
    assert fmt_tokens(1_200_000) == "1.2M"
    assert fmt_tokens(0) == "0"


def test_short_model():
    assert short_model("gemini-4-hydrogen-xhigh") == "hydrogen"
    assert short_model("claude-sonnet-5-5") == "sonnet 5.5"
    assert short_model("claude-opus-5-5-low") == "opus 5.5"
    assert short_model("claude-opus-5-5") == "opus 5.5"
    assert short_model("gpt-5.6-terra") == "gpt-5.6-terra"
    assert short_model("gpt-oss-120b-medium") == "gpt-oss-120b"
    assert short_model("") == "?"
    assert short_model("claude-sonnet-4-5-20250929") == "sonnet 4.5"
    assert short_model("weird") == "weird"


def test_status_maps_complete():
    for status in Status:
        assert status in STATUS_COLORS and status in STATUS_ICONS


class WidgetApp(App):
    def __init__(self) -> None:
        super().__init__()
        self.selected: list[str] = []
        self.opened: list[str] = []

    def compose(self) -> ComposeResult:
        yield AgentCard(make_agent(), parent_title="Parent", id="card")
        yield CardGrid(id="grid")
        yield AgentList(id="list")
        yield EventLog(id="log")
        yield TotalsBar(id="totals")
        yield QuotaPanel(id="quota")

    def on_agent_list_selected(self, message: AgentList.Selected) -> None:
        self.selected.append(message.agent_key)

    def on_agent_card_opened(self, message: AgentCard.Opened) -> None:
        self.opened.append(message.agent_key)


class GridApp(App):
    def compose(self) -> ComposeResult:
        yield CardGrid(id="grid")


def test_card_lines():
    agent = make_agent(
        key="claude:op1",
        model="claude-opus-5-5",
        effort="low",
        in_tokens=45_000,
        out_tokens=7_000,
    )
    rendered = AgentCard(agent).render_agent(T0 + timedelta(seconds=95))
    t = rendered.plain
    assert len(t.splitlines()) == 5
    assert "opus" in t and "low" in t and "tok" in t and "out" in t
    assert "claude · opus 5.5 · low" in t
    assert "52k tok · 7k out" in t
    card = AgentCard(agent)
    card._update_borders(T0 + timedelta(seconds=95))
    assert "1:35" in str(card.border_subtitle)
    assert "● Bash Run auth tests" in t


def test_quiet_hint():
    now = T0 + timedelta(minutes=10)
    quiet_agent = make_agent(last_write=now - timedelta(minutes=6))
    assert "quiet 6m" in AgentCard(quiet_agent).render_agent(now).plain

    fresh_agent = make_agent(last_write=now - timedelta(seconds=config.QUIET_SECONDS - 1))
    assert "quiet" not in AgentCard(fresh_agent).render_agent(now).plain

    done_agent = make_agent(status=Status.DONE, last_write=now - timedelta(minutes=8))
    assert "quiet" not in AgentCard(done_agent).render_agent(now).plain


def test_nested_label():
    agy_agent = make_agent(
        key="agy:c1",
        source=Source.AGY,
        model="gemini-4-hydrogen-xhigh",
        effort="xhigh",
        parent_agent_key="claude:sub1",
    )
    assert "← Explore" in AgentCard(agy_agent, parent_title="Explore").render_agent(T0).plain

    # No "← main" parent label when parent is main.
    main_child = make_agent(
        key="codex:r1",
        source=Source.CODEX,
        session_key="s1",
        parent_agent_key=main_agent_key("s1"),
    )
    assert "← main" not in AgentCard(main_child, parent_title="main").render_agent(T0).plain
    assert "←" not in AgentCard(main_child, parent_title="Explore").render_agent(T0).plain


async def test_card_content():
    app = WidgetApp()
    async with app.run_test() as pilot:
        card = app.query_one("#card", AgentCard)
        card.update_agent(make_agent(), now=T0 + timedelta(seconds=310))
        await pilot.pause()
        out = text_of(card)
        assert "Agent a1" in str(card.border_title) and "← Parent" in out
        assert "claude · sonnet 5.5" in out
        assert "5:10" in str(card.border_subtitle) and "running" in str(card.border_subtitle)
        assert "82k tok · 2k out" in out
        assert "● Bash Run auth tests" in out
        assert card.has_class("-running")

        card.parent_title = None
        card.update_agent(make_agent(), now=T0 + timedelta(seconds=310))
        assert "← Parent" not in text_of(card)
        assert "Bash Run auth tests" in text_of(card)

        card.update_agent(make_agent(status=Status.DONE, ended=T0 + timedelta(seconds=282)))
        assert card.has_class("-done") and not card.has_class("-running")
        assert "4:42" in str(card.border_subtitle)
        assert "✓" in text_of(card) and "✓" in str(card.border_subtitle)

        card.update_agent(make_agent(status=Status.FAILED))
        assert "✗" in text_of(card) and "✗" in str(card.border_subtitle)


def test_card_sources_and_main_title():
    main = make_agent(key=main_agent_key("s"), title="", is_main=True, effort="")
    main_text = AgentCard(main).render_agent(T0).plain
    assert "main" in str(AgentCard(main).border_title)
    assert "claude · sonnet 5.5" in main_text
    assert "claude · sonnet 5.5 ·" not in main_text

    codex = make_agent(key="codex:r1", source=Source.CODEX, model="gpt-5.6-terra", effort="high")
    codex_text = AgentCard(codex).render_agent(T0).plain
    assert "codex · gpt-5.6-terra · high" in codex_text

    agy_t0 = AgentCard(make_agent(source=Source.AGY, model="gemini-4-hydrogen-xhigh")).render_agent(T0, tick=0).plain
    agy_t1 = AgentCard(make_agent(source=Source.AGY, model="gemini-4-hydrogen-xhigh")).render_agent(T0, tick=1).plain
    assert agy_t0 != agy_t1


async def test_card_opened_on_click_and_enter():
    app = WidgetApp()
    async with app.run_test() as pilot:
        card = app.query_one("#card", AgentCard)
        assert card.can_focus is True
        card.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.opened == ["a1"]
        await pilot.click("#card", offset=(5, 2))
        await pilot.pause()
        assert app.opened == ["a1", "a1"]


async def test_card_grid_main_first_and_started_order():
    app = GridApp()
    async with app.run_test(size=(120, 30)) as pilot:
        grid = app.query_one("#grid", CardGrid)
        sub_early = make_agent("claude:early", started=T0)
        sub_late = make_agent("claude:late", started=T0 + timedelta(seconds=30))
        main = make_agent(main_agent_key("s"), started=T0 + timedelta(seconds=60), is_main=True)
        grid.set_agents([sub_late, sub_early], {})
        await pilot.pause()
        assert list(grid.cards) == ["claude:early", "claude:late"]

        grid.set_agents([sub_late, main, sub_early], {})
        await pilot.pause()
        expected = [main_agent_key("s"), "claude:early", "claude:late"]
        assert list(grid.cards) == expected
        assert [c.agent.key for c in grid.query(AgentCard)] == expected


async def test_card_grid_reorders_subsequent_cards():
    app = GridApp()
    async with app.run_test(size=(120, 30)) as pilot:
        grid = app.query_one("#grid", CardGrid)
        a = make_agent("a", started=T0)
        b = make_agent("b", started=T0 + timedelta(seconds=10))
        c = make_agent("c", started=T0 + timedelta(seconds=20))
        grid.set_agents([a, b, c], {})
        await pilot.pause()
        assert [card.agent.key for card in grid.query(AgentCard)] == ["a", "b", "c"]

        b_later = make_agent("b", started=T0 + timedelta(seconds=30))
        grid.set_agents([a, b_later, c], {})
        await pilot.pause()
        assert [card.agent.key for card in grid.query(AgentCard)] == ["a", "c", "b"]


async def test_card_grid_reserves_scrollbar_width():
    app = GridApp()
    async with app.run_test(size=(160, 20)) as pilot:
        grid = app.query_one("#grid", CardGrid)
        agents = [make_agent(f"a{i}", started=T0 + timedelta(seconds=i)) for i in range(8)]
        grid.set_agents(agents, {})
        await pilot.pause()
        assert grid.styles.grid_size_columns == 2
        assert grid.show_vertical_scrollbar is True
        assert grid.max_scroll_x == 0


async def test_offscreen_cards_not_redrawn():
    app = GridApp()
    async with app.run_test(size=(60, 20)) as pilot:
        grid = app.query_one("#grid", CardGrid)
        agents = [make_agent(f"a{i}", started=T0 + timedelta(seconds=i)) for i in range(6)]
        grid.set_agents(agents, {})
        await pilot.pause()

        counts = {a.key: 0 for a in agents}
        for key, card in grid.cards.items():
            orig = card.render_agent

            def wrapped(now=None, tick=0, _orig=orig, _k=key):
                counts[_k] += 1
                return _orig(now, tick)

            card.render_agent = wrapped  # type: ignore[method-assign]

        await asyncio.sleep(config.MASCOT_TICK * 3.5)
        await pilot.pause()
        assert counts["a0"] >= 1
        assert counts["a5"] == 0

        grid.scroll_to(y=22, animate=False)
        await pilot.pause()
        prev_a0 = counts["a0"]
        prev_a5 = counts["a5"]
        await asyncio.sleep(config.MASCOT_TICK * 3.5)
        await pilot.pause()
        assert counts["a5"] > prev_a5
        assert counts["a0"] == prev_a0


async def test_card_grid_diff():
    app = WidgetApp()
    async with app.run_test() as pilot:
        grid = app.query_one("#grid", CardGrid)
        grid.set_agents([make_agent("a"), make_agent("b")], {})
        await pilot.pause()
        assert len(grid.query(AgentCard)) == 2
        first = grid.cards["a"]
        grid.set_agents([make_agent("a", status=Status.DONE), make_agent("b"), make_agent("c")], {"c": "P"})
        await pilot.pause()
        assert len(grid.query(AgentCard)) == 3
        assert grid.cards["a"] is first and first.has_class("-done")
        grid.set_agents([make_agent("c")], {})
        await pilot.pause()
        assert list(grid.cards) == ["c"] and len(grid.query(AgentCard)) == 1


async def test_agent_list_select():
    app = WidgetApp()
    async with app.run_test() as pilot:
        lst = app.query_one("#list", AgentList)
        lst.set_agents([
            make_agent("late", started=T0 + timedelta(seconds=9)),
            make_agent("early", started=T0),
        ])
        await pilot.pause()
        assert lst.option_count == 2
        assert lst.get_option_at_index(0).id == "early"
        lst.focus()
        lst.highlighted = 1
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.selected == ["late"]


async def test_event_log():
    app = WidgetApp()
    async with app.run_test() as pilot:
        log = app.query_one("#log", EventLog)
        assert log.max_lines == 500
        ev = Event(ts=T0, session_key="s", agent_key="a", kind=EventKind.TOOL_START, tool="Bash", summary="Run tests")
        log.add_event(ev, "Agent a", "#ff0000")
        await pilot.pause()
        line = "".join(seg.text for seg in log.lines[0])
        assert "Agent a" in line and "→ Run tests" in line


async def test_totals_bar():
    app = WidgetApp()
    async with app.run_test() as pilot:
        bar = app.query_one("#totals", TotalsBar)
        bar.update_totals({"agents_done": 5, "agents_total": 6, "running": 1, "calls": 147, "out_tokens": 53_000}, 580)
        await pilot.pause()
        assert text_of(bar) == "agents 5/6 done · running 1 · calls 147 · out 53k · time 9:40"


async def test_quota_panel():
    app = WidgetApp()
    async with app.run_test() as pilot:
        panel = app.query_one("#quota", QuotaPanel)
        panel.update_quota({"Gemini 5h": (0.83, T0), "Claude": (None, None)})
        await pilot.pause()
        out = text_of(panel)
        assert "Gemini 5h" in out and "83%" in out and "resets" in out
        assert "Claude n/a" in out


async def test_codex_quota_line():
    app = WidgetApp()
    async with app.run_test() as pilot:
        panel = app.query_one("#quota", QuotaPanel)
        limits = CodexLimits(used_percent=23.4, window_minutes=300, resets_at=T0)
        panel.update_quota({"Gemini 5h": (0.83, T0)}, codex=limits)
        await pilot.pause()
        out = text_of(panel)
        assert "Gemini 5h" in out
        assert "codex  23% used" in out and "resets" in out

        panel.update_quota({}, codex=CodexLimits(used_percent=61.0, window_minutes=300, resets_at=None))
        await pilot.pause()
        only_codex = text_of(panel)
        assert "codex  61% used" in only_codex and "resets" not in only_codex


def _log_text(log: EventLog) -> list[str]:
    return ["".join(seg.text for seg in line) for line in log.lines]


async def test_event_log_coalesces_identical_consecutive_rows():
    app = WidgetApp()
    async with app.run_test() as pilot:
        log = app.query_one("#log", EventLog)
        ev = Event(ts=T0, session_key="s", agent_key="a", kind=EventKind.TOOL_START, tool="Bash", summary="git status")
        for _ in range(3):
            log.add_event(ev, "Agent a", "#ff0000")
        await pilot.pause()
        rows = _log_text(log)
        assert len(rows) == 1
        assert rows[0].endswith("→ git status ×3")
        # the counter is dim, not part of the summary style
        tail = [seg for seg in log.lines[0] if seg.text.strip() == "×3"]
        assert tail and tail[0].style.color.get_truecolor().hex.lower() == DIM.lower()


async def test_event_log_does_not_merge_different_agents_kinds_or_text():
    app = WidgetApp()
    async with app.run_test() as pilot:
        log = app.query_one("#log", EventLog)
        a1 = Event(ts=T0, session_key="s", agent_key="a", kind=EventKind.TOOL_START, summary="x")
        b1 = Event(ts=T0, session_key="s", agent_key="b", kind=EventKind.TOOL_START, summary="x")
        a_end = Event(ts=T0, session_key="s", agent_key="a", kind=EventKind.TOOL_END, summary="x")
        a2 = Event(ts=T0, session_key="s", agent_key="a", kind=EventKind.TOOL_START, summary="y")
        for e in (a1, b1, a_end, a2, a2):
            log.add_event(e, "t", "#ff0000")
        await pilot.pause()
        rows = _log_text(log)
        assert len(rows) == 4
        assert rows[-1].endswith("→ y ×2") and "×" not in rows[0]


async def test_event_log_drops_bare_usage_rows_but_keeps_usage_with_summary():
    app = WidgetApp()
    async with app.run_test() as pilot:
        log = app.query_one("#log", EventLog)
        log.add_event(Event(ts=T0, session_key="s", agent_key="a", kind=EventKind.USAGE, tokens_out=5), "t", "#f00")
        log.add_event(Event(ts=T0, session_key="s", agent_key="a", kind=EventKind.USAGE, summary="note"), "t", "#f00")
        await pilot.pause()
        rows = _log_text(log)
        assert len(rows) == 1 and rows[0].endswith("→ note")


async def test_event_log_clear_resets_merge_state_and_keeps_following():
    app = WidgetApp()
    async with app.run_test() as pilot:
        log = app.query_one("#log", EventLog)
        ev = Event(ts=T0, session_key="s", agent_key="a", kind=EventKind.TEXT, summary="same")
        log.add_event(ev, "t", "#f00")
        log.clear()
        log.add_event(ev, "t", "#f00")
        await pilot.pause()
        assert _log_text(log)[0].endswith("→ same") and len(log.lines) == 1
        for i in range(60):
            log.add_event(Event(ts=T0, session_key="s", agent_key="a", kind=EventKind.TEXT, summary=f"n{i}"), "t", "#f00")
        log.add_event(Event(ts=T0, session_key="s", agent_key="a", kind=EventKind.TEXT, summary="n59"), "t", "#f00")
        await pilot.pause()
        assert log.scroll_y >= log.max_scroll_y - 1


def test_card_token_line_shows_cost_for_priced_model():
    agent = make_agent(key="claude:op1", model="claude-opus-5-5", in_tokens=45_000, out_tokens=7_000)
    t = AgentCard(agent).render_agent(T0 + timedelta(seconds=95)).plain
    assert "52k tok · 7k out · ≈ $0.32" in t


def test_card_token_line_no_cost_for_unpriced_model():
    agent = make_agent(key="agy:g1", source=Source.AGY, model="gemini-4-flash", in_tokens=45_000, out_tokens=7_000)
    t = AgentCard(agent).render_agent(T0 + timedelta(seconds=95)).plain
    assert "52k tok · 7k out" in t and "≈" not in t and "$" not in t


async def test_totals_bar_cost_exact_and_partial():
    app = WidgetApp()
    async with app.run_test() as pilot:
        bar = app.query_one("#totals", TotalsBar)
        base = {"agents_done": 1, "agents_total": 2, "running": 0, "calls": 3, "out_tokens": 1000}
        bar.update_totals({**base, "cost": 12.3, "cost_partial": False}, 60)
        await pilot.pause()
        assert text_of(bar).endswith("time 1:00 · cost ≈ $12.30")
        bar.update_totals({**base, "cost": 12.3, "cost_partial": True}, 60)
        await pilot.pause()
        assert text_of(bar).endswith("cost ≈ $12.30+")
        bar.update_totals({**base, "cost": None, "cost_partial": True}, 60)
        await pilot.pause()
        assert "cost" not in text_of(bar)
