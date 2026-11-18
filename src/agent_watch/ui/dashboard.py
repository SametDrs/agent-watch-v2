"""Mission-control dashboard screen rendering a Store."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual import events
from textual.containers import Horizontal
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Footer, Input, Static

from agent_watch.config import model_color
from agent_watch.model import UNLINKED_SESSION_KEY, Agent, Event, Session, Status, main_agent_key
from agent_watch.poller import UNLINKED_NAME
from agent_watch.sources.codex import CodexLimits
from agent_watch.store import Store
from agent_watch.ui.theme import (
    ACCENT, BG, BORDER, DIM, MUTED, STATUS_COLORS, SURFACE, SURFACE_HI, TEXT, fmt_duration, short_model,
)
from agent_watch.ui.widgets import AgentCard, AgentList, CardGrid, EventLog, QuotaPanel, TotalsBar

EMPTY_TEXT = "Henüz ajan yok — Claude, agy ya da Codex çalıştırınca burada görünecek."
LOG_BUFFER = 200
_UNSET: Any = object()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def agent_matches(agent: Agent, query: str) -> bool:
    """Return True when every space-separated term is found (case-insensitive) in the agent.

    The searched text is the title, source, status word and model (raw and short form).
    The main agent also answers to "main". An empty query matches everything.
    """
    terms = query.lower().split()
    if not terms:
        return True
    title = "main" if agent.is_main else (agent.title or agent.key)
    hay = " ".join(
        (title, agent.source.value, agent.status.value, agent.model, short_model(agent.model) if agent.model else "")
    ).lower()
    return all(term in hay for term in terms)


class TabBar(Static):
    """Single-line clickable session tab strip."""

    DEFAULT_CSS = f"""
    TabBar {{
        height: 1;
        padding: 0 1;
        background: {BG};
    }}
    """

    def __init__(self, *args, **kw) -> None:
        super().__init__(*args, **kw)
        self.ranges: list[tuple[int, int]] = []  # [start, end) text columns per tab

    def on_click(self, event: events.Click) -> None:
        """Select the tab under the pointer (plain spans, so no link underline)."""
        col = event.x - self.styles.padding.left
        for number, (start, end) in enumerate(self.ranges, 1):
            if start <= col < end:
                event.stop()
                self.screen.action_select_tab(number)
                return


class DashboardScreen(Screen):
    """Dashboard with session tabs, agent cards, quota panel and event log."""

    class OpenRequested(Message):
        """User asked to open the transcript folder of an agent."""

        def __init__(self, agent_key: str) -> None:
            super().__init__()
            self.agent_key = agent_key

    class QuotaRefreshRequested(Message):
        """User asked for a quota refresh."""

    CSS = f"""
    DashboardScreen {{ background: {BG}; color: {TEXT}; }}
    #header {{ height: 1; padding: 0 1; background: {SURFACE}; color: {TEXT}; }}
    #filterbar {{ height: 1; padding: 0 1; background: {SURFACE}; display: none; }}
    DashboardScreen.-filtering #filterbar {{ display: block; }}
    DashboardScreen.-empty #filterbar {{ display: none; }}
    #filter-prefix {{ width: 2; color: {ACCENT}; background: {SURFACE}; }}
    #filter-input {{ width: 1fr; height: 1; border: none; padding: 0; background: {SURFACE}; color: {TEXT}; }}
    #filter-input:focus {{ border: none; background-tint: transparent; }}
    #filter-count {{ width: auto; margin-left: 2; background: {SURFACE}; }}
    #body {{ height: 1fr; background: {BG}; }}
    #empty {{ height: 1fr; content-align: center middle; color: {DIM}; display: none; }}
    #quota {{ width: 36; padding: 0 1; background: {SURFACE}; border-left: solid {BORDER}; display: none; }}
    #quota.-has-data {{ display: block; }}
    #sidebar {{ background: {SURFACE}; border-right: solid {BORDER}; }}
    #grid {{ background: {BG}; }}
    #events {{ height: 8; background: {BG}; border-top: solid {BORDER}; }}
    DashboardScreen.-empty #body {{ display: none; }}
    DashboardScreen.-empty #empty {{ display: block; }}
    DashboardScreen.-compact #quota {{ display: none; }}
    DashboardScreen.-compact #sidebar {{ display: none; }}
    DashboardScreen AgentCard.-selected {{ border: round {ACCENT}; background: {SURFACE_HI}; }}
    """

    AUTO_FOCUS = "AgentList"  # never autofocus the (hidden) filter input

    BINDINGS = [
        Binding("1", "select_tab(1)", "Sekme", key_display="1-9"),
        *[Binding(str(n), f"select_tab({n})", f"Sekme {n}", show=False) for n in range(2, 10)],
        Binding("tab", "next_tab", "Sonraki", priority=True),
        Binding("shift+tab", "prev_tab", "Önceki", show=False, priority=True),
        Binding("/", "open_filter", "Filtre"),
        Binding("escape", "escape", "Temizle", show=False),
        Binding("n", "toggle_notifications", "Bildirim"),
        Binding("v", "toggle_compact", "Kompakt"),
        Binding("o", "open_transcript", "Klasör"),
        Binding("r", "refresh_quota", "Kota"),
        Binding("q", "quit_app", "Çıkış"),
    ]

    def __init__(self, store: Store, **kw) -> None:
        super().__init__(**kw)
        self.store = store
        self.tab_keys: list[str] = []
        self.tab_labels: list[str] = []
        self.selected_key: str | None = None
        self.selected_agent_key: str | None = None
        self._logs: dict[str, deque[tuple[Event, str, str]]] = {}
        self._quota: dict[str, tuple[float | None, datetime | None]] = {}
        self._codex_limits: CodexLimits | None = None
        self.filter_text = ""  # global across tabs; applied to cards, list and timeline
        self.notifications_enabled = True
        self._seen_status: dict[str, Status] = {}
        self._ready = False

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        yield TabBar(id="tabs")
        with Horizontal(id="filterbar"):
            yield Static("/ ", id="filter-prefix")
            yield Input(placeholder="agent, source, status, model", id="filter-input")
            yield Static(id="filter-count")
        yield Static(EMPTY_TEXT, id="empty")
        with Horizontal(id="body"):
            yield AgentList(id="sidebar")
            yield CardGrid(id="grid")
            yield QuotaPanel(id="quota")
        yield EventLog(id="events")
        yield TotalsBar(id="totals")
        yield Footer()

    def on_mount(self) -> None:
        self._ready = True
        self._check_transitions(notify=False)  # prime: agents present now never notify
        self.set_interval(1.0, self._tick)
        self._rebuild_tabs()
        self._refresh_view()
        self._render_header()
        if self._quota or self._codex_limits is not None:
            self.query_one(QuotaPanel).update_quota(self._quota, self._codex_limits)

    # ----- tabs -----

    def _visible_sessions(self) -> list[Session]:
        """Return sessions shown as tabs (unlinked only when it has agents)."""
        return [
            s for s in self.store.sessions_sorted()
            if s.key != UNLINKED_SESSION_KEY or s.agents
        ]

    def _label(self, index: int, session: Session) -> str:
        fallback = UNLINKED_NAME if session.key == UNLINKED_SESSION_KEY else session.key[:8]
        name = session.name or fallback
        return f"{index} {name} ({session.done_count()}/{len(session.agents)})"

    def _rebuild_tabs(self) -> bool:
        """Recompute tab keys/labels; keep selection by key. Return True if selection changed."""
        sessions = self._visible_sessions()
        self.tab_keys = [s.key for s in sessions]
        self.tab_labels = [self._label(i, s) for i, s in enumerate(sessions, 1)]
        previous = self.selected_key
        if previous not in self.tab_keys:
            self.selected_key = self.tab_keys[0] if self.tab_keys else None
        self._render_tabbar()
        self.set_class(not self.tab_keys, "-empty")
        return self.selected_key != previous

    def _render_tabbar(self) -> None:
        text = Text(no_wrap=True)
        ranges: list[tuple[int, int]] = []
        for i, (key, label) in enumerate(zip(self.tab_keys, self.tab_labels), 1):
            if i > 1:
                text.append("  ")
            active = key == self.selected_key
            num = str(i)
            rest = label[len(num):]
            start = len(text)
            if active:
                text.append(" ", style=f"on {SURFACE_HI}")
                text.append(num, style=f"bold {ACCENT} on {SURFACE_HI}")
                text.append(rest, style=f"bold {TEXT} on {SURFACE_HI}")
                text.append(" ", style=f"on {SURFACE_HI}")
            else:
                text.append(" ")
                text.append(num, style=DIM)
                text.append(rest, style=MUTED)
                text.append(" ")
            ranges.append((start, len(text)))
        if not self.tab_keys:
            text.append("(oturum yok)", style=DIM)
        tabbar = self.query_one(TabBar)
        tabbar.ranges = ranges
        tabbar.update(text)

    def action_select_tab(self, n: int) -> None:
        """Select tab number n (1-based)."""
        if 1 <= n <= len(self.tab_keys):
            self._select(self.tab_keys[n - 1])

    def action_next_tab(self) -> None:
        self._step_tab(1)

    def action_prev_tab(self) -> None:
        self._step_tab(-1)

    def _step_tab(self, delta: int) -> None:
        if not self.tab_keys:
            return
        idx = self.tab_keys.index(self.selected_key) if self.selected_key in self.tab_keys else 0
        self._select(self.tab_keys[(idx + delta) % len(self.tab_keys)])

    def _select(self, key: str) -> None:
        if key == self.selected_key:
            return
        self.selected_key = key
        self.selected_agent_key = None
        self._render_tabbar()
        self._refresh_view()
        self._repopulate_log()

    # ----- data -----

    def _session_agents(self) -> list[Agent]:
        session = self.store.sessions.get(self.selected_key or "")
        return list(session.agents.values()) if session else []

    def _visible_agents(self, agents: list[Agent]) -> list[Agent]:
        """Return the agents that match the active filter."""
        return [a for a in agents if agent_matches(a, self.filter_text)]

    def _render_filter_count(self, matched: int, total: int) -> None:
        text = Text(no_wrap=True)
        if self.filter_text.strip():
            text.append(f"filter: {self.filter_text.strip()}", style=MUTED)
            text.append(" · ", style=DIM)
            text.append(f"{matched}/{total}", style=MUTED)
        else:
            text.append(f"{total} agents", style=DIM)
            text.append(" · ", style=DIM)
            text.append("enter apply · esc clear", style=DIM)
        self.query_one("#filter-count", Static).update(text)

    def _refresh_view(self, now: datetime | None = None) -> None:
        """Push the selected session's agents, list and totals into the widgets."""
        if not self._ready:
            return
        now = now or _now()
        all_agents = self._session_agents()
        agents = self._visible_agents(all_agents)
        self._render_filter_count(len(agents), len(all_agents))
        parents: dict[str, str] = {}
        for agent in agents:
            if (
                agent.parent_agent_key
                and agent.parent_agent_key != main_agent_key(agent.session_key)
            ):
                parent = self.store.find_agent(agent.parent_agent_key)
                if parent is not None and not parent.is_main:
                    parents[agent.key] = parent.title or parent.key
        self.query_one(CardGrid).set_agents(agents, parents)
        self.query_one(AgentList).set_agents(agents, default_key=self.selected_agent_key)
        starts = [a.started for a in all_agents if a.started is not None]
        elapsed = (now - min(starts)).total_seconds() if starts else None
        totals = self.store.totals(self.selected_key or "")
        self.query_one(TotalsBar).update_totals(totals, elapsed)
        self._apply_card_selection()

    def refresh_sessions(self, changed: set[str], now: datetime | None = None) -> None:
        """Refresh tabs and, when the selected session changed, its widgets."""
        if not self._ready:
            return
        self._check_transitions()
        switched = self._rebuild_tabs()
        if switched or (self.selected_key is not None and self.selected_key in changed):
            self._refresh_view(now)
        if switched:
            self._repopulate_log()

    # ----- notifications -----

    def _check_transitions(self, notify: bool = True) -> None:
        """Notify when a known, unfinished agent became done/failed since the last check.

        Agents first seen already finished (first load, or discovered late) and the main
        agent never notify; only transitions observed during this run do.
        """
        current: dict[str, Status] = {}
        finished: list[Agent] = []
        for session in self.store.sessions.values():
            for agent in session.agents.values():
                current[agent.key] = agent.status
                before = self._seen_status.get(agent.key)
                if (
                    before is not None
                    and before not in (Status.DONE, Status.FAILED)
                    and agent.status in (Status.DONE, Status.FAILED)
                    and not agent.is_main
                    and agent.key != main_agent_key(agent.session_key)
                ):
                    finished.append(agent)
        self._seen_status = current
        if notify and self.notifications_enabled:
            for agent in finished:
                self._notify_finished(agent)

    def _notify_finished(self, agent: Agent) -> None:
        failed = agent.status == Status.FAILED
        duration = fmt_duration(agent.duration_seconds())
        self.app.notify(
            f"{agent.status.value} · {duration}",
            title=agent.title or agent.key,
            severity="error" if failed else "information",
        )
        if failed:
            self.app.bell()

    def action_toggle_notifications(self) -> None:
        self.notifications_enabled = not self.notifications_enabled
        self._render_header()
        self.app.notify("Bildirimler açık" if self.notifications_enabled else "Bildirimler kapalı")

    # ----- events -----

    def _event_session(self, event: Event) -> str:
        agent = self.store.find_agent(event.agent_key)
        return agent.session_key if agent is not None else event.session_key

    def _event_row(self, event: Event) -> tuple[Event, str, str]:
        agent = self.store.find_agent(event.agent_key)
        model = (agent.model if agent else "") or event.model or ""
        title = (agent.title if agent else "") or event.title or event.agent_key
        return event, title, model_color(model)

    def add_events(self, events: list[Event]) -> None:
        """Buffer events per session and show those of the selected session."""
        log = self.query_one(EventLog) if self._ready else None
        for event in events:
            key = self._event_session(event)
            row = self._event_row(event)
            self._logs.setdefault(key, deque(maxlen=LOG_BUFFER)).append(row)
            if log is not None and key == self.selected_key:
                log.add_event(*row)

    def _repopulate_log(self) -> None:
        if not self._ready:
            return
        log = self.query_one(EventLog)
        log.clear()
        for row in self._logs.get(self.selected_key or "", ()):
            log.add_event(*row)

    # ----- quota / header / tick -----

    def update_quota(
        self,
        data: dict[str, tuple[float | None, datetime | None]],
        codex: CodexLimits | None = _UNSET,
    ) -> None:
        """Show quota data and optional Codex limits in the right panel."""
        self._quota = data
        if codex is not _UNSET:
            self._codex_limits = codex
        if self._ready:
            self.query_one(QuotaPanel).update_quota(self._quota, self._codex_limits)

    def update_codex_limits(self, codex: CodexLimits | None) -> None:
        """Update Codex rate limits in the right panel without replacing agy quota."""
        if codex == self._codex_limits:
            return
        self._codex_limits = codex
        if self._ready:
            self.query_one(QuotaPanel).update_quota(self._quota, self._codex_limits)

    def _render_header(self) -> None:
        text = Text(no_wrap=True)
        text.append("◆ ", style=ACCENT)
        text.append("agent watch", style=f"bold {TEXT}")
        text.append(" · ", style=DIM)
        text.append(datetime.now().strftime("%H:%M:%S"), style=MUTED)
        text.append(" · ", style=DIM)
        text.append("● ", style=STATUS_COLORS[Status.DONE])
        text.append("live", style=MUTED)
        if not self.notifications_enabled:
            text.append(" · ", style=DIM)
            text.append("notify off", style=DIM)
        self.query_one("#header", Static).update(text)

    def _tick(self) -> None:
        """Refresh the header clock."""
        self._render_header()

    # ----- selection of agents -----

    def on_agent_card_opened(self, message: AgentCard.Opened) -> None:
        """Select the opened card while allowing the message to bubble."""
        self.selected_agent_key = message.agent_key
        self._apply_card_selection()

    def on_agent_list_selected(self, message: AgentList.Selected) -> None:
        """Select the chosen agent card while allowing the message to bubble."""
        self.selected_agent_key = message.agent_key
        self._apply_card_selection(scroll=True)

    def _apply_card_selection(self, scroll: bool = False) -> None:
        cards = self.query_one(CardGrid).cards
        for key, card in cards.items():
            card.set_class(key == self.selected_agent_key, "-selected")
        card = cards.get(self.selected_agent_key or "")
        if scroll and card is not None:
            card.scroll_visible()
            card.focus()

    def _current_agent_key(self) -> str | None:
        cards = self.query_one(CardGrid).cards
        if self.selected_agent_key in cards:
            return self.selected_agent_key
        return next(iter(cards), None)

    # ----- filter -----

    def action_open_filter(self) -> None:
        self.add_class("-filtering")
        self.query_one("#filter-input", Input).focus()

    def on_input_changed(self, message: Input.Changed) -> None:
        if message.input.id != "filter-input":
            return
        message.stop()
        self.filter_text = message.value
        self._refresh_view()

    def on_input_submitted(self, message: Input.Submitted) -> None:
        """Enter keeps the filter but returns focus to the cards."""
        if message.input.id != "filter-input":
            return
        message.stop()
        self.set_focus(None)

    def action_escape(self) -> None:
        """Clear the filter and close the bar."""
        if self.has_class("-filtering"):
            self.remove_class("-filtering")
            self.filter_text = ""
            self.query_one("#filter-input", Input).value = ""
            self.set_focus(None)
            self._refresh_view()

    # ----- actions -----

    def action_toggle_compact(self) -> None:
        self.toggle_class("-compact")

    def action_open_transcript(self) -> None:
        key = self._current_agent_key()
        if key is not None:
            self.post_message(self.OpenRequested(key))

    def action_refresh_quota(self) -> None:
        self.post_message(self.QuotaRefreshRequested())

    def action_quit_app(self) -> None:
        self.app.exit()
