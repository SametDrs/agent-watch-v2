"""Read-only conversation screen and event renderers."""

from __future__ import annotations

from datetime import datetime, timezone

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, RichLog, Static

from agent_watch import config
from agent_watch.config import model_color
from agent_watch.model import Agent, Event, EventKind, Status, main_agent_key
from agent_watch.store import Store
from agent_watch.ui.theme import (
    ACCENT,
    DIM,
    SOURCE_COLORS,
    STATUS_COLORS,
    STATUS_ICONS,
    fmt_duration,
    fmt_tokens,
    short_model,
)

TOOL_ARGS_CAP = 500
TOOL_RESULT_CAP = 800
EMPTY_THINKING_TEXT = "(thought — text not stored by this tool)"


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _clip_body(text: str, limit: int) -> str:
    """Return up to the first limit characters of text, adding an ellipsis when clipped."""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def render_event(ev: Event) -> RenderableType | None:
    """Render a normalized Event as a role-labelled Rich block, or None to hide it."""
    if ev.kind in (EventKind.USAGE, EventKind.START):
        return None

    if ev.kind == EventKind.PROMPT:
        header = Text("▶ to agent", style=f"bold {ACCENT}")
        body = ev.body or ev.summary
        parts: list[RenderableType] = [header]
        if body:
            parts.append(Text(body))
        parts.append(Text(""))
        return Group(*parts)

    if ev.kind == EventKind.TEXT:
        color = model_color(ev.model) if ev.model else "#6ea8fe"
        header = Text("◀ agent", style=f"bold {color}")
        body = ev.body or ev.summary
        parts = [header]
        if body:
            parts.append(Markdown(body))
        parts.append(Text(""))
        return Group(*parts)

    if ev.kind == EventKind.THINKING:
        header = Text("… thinking", style=f"italic {DIM}")
        if ev.body.strip():
            body_part = Text(ev.body, style=DIM)
        else:
            body_part = Text(EMPTY_THINKING_TEXT, style=f"italic {DIM}")
        return Group(header, body_part, Text(""))

    if ev.kind == EventKind.TOOL_START:
        label = ev.summary or ev.tool or "tool"
        header = Text(f"⚙ {label}", style="bold #e6c07b")
        parts = [header]
        if ev.body:
            parts.append(Text(_clip_body(ev.body, TOOL_ARGS_CAP), style=DIM))
        parts.append(Text(""))
        return Group(*parts)

    if ev.kind == EventKind.TOOL_END:
        is_err = bool(ev.summary and "error" in ev.summary.lower())
        header_label = f"↳ result · {ev.summary}" if ev.summary else "↳ result"
        header = Text(header_label, style=STATUS_COLORS[Status.FAILED] if is_err else DIM)
        parts = [header]
        if ev.body:
            parts.append(Text(_clip_body(ev.body, TOOL_RESULT_CAP), style=DIM))
        parts.append(Text(""))
        return Group(*parts)

    if ev.kind == EventKind.ERROR:
        err_color = STATUS_COLORS[Status.FAILED]
        title = f"✗ error: {ev.summary}" if ev.summary else "✗ error"
        parts = [Text(title, style=f"bold {err_color}")]
        if ev.body:
            parts.append(Text(ev.body, style=err_color))
        parts.append(Text(""))
        return Group(*parts)

    if ev.kind == EventKind.END:
        status = ev.status if ev.status is not None else Status.DONE
        color = STATUS_COLORS[status]
        line = Text()
        line.append(f"{STATUS_ICONS[status]} {status.value}", style=f"bold {color}")
        if ev.summary:
            line.append(f" · {ev.summary}", style=DIM)
        parts = [line]
        if ev.body:
            parts.append(Text(ev.body, style=DIM))
        parts.append(Text(""))
        return Group(*parts)

    return None


class TranscriptLog(RichLog):
    """Scrollable read-only transcript log that follows tail until scrolled up."""

    def __init__(self, **kw) -> None:
        kw.setdefault("wrap", True)
        kw.setdefault("markup", False)
        kw.setdefault("auto_scroll", False)
        kw.setdefault("min_width", 1)
        super().__init__(**kw)
        self.following: bool = True
        self._pending_scroll_end: int = 0

    def _finish_pending_scroll_end(self) -> None:
        self._pending_scroll_end = max(0, self._pending_scroll_end - 1)

    def watch_scroll_target_y(self, old_value: float, new_value: float) -> None:
        if self.max_scroll_y > 0:
            self.following = round(new_value) >= self.max_scroll_y

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if self.max_scroll_y > 0:
            animating_to_end = (
                round(self.scroll_target_y) >= self.max_scroll_y
                and self.app.animator.is_being_animated(self, "scroll_y")
            )
            if round(new_value) < self.max_scroll_y:
                if not animating_to_end and self._pending_scroll_end == 0:
                    self.following = False
            elif (
                self._pending_scroll_end > 0
                or round(self.scroll_target_y) >= self.max_scroll_y
                or not self.app.animator.is_being_animated(self, "scroll_y")
            ):
                self.following = True

    def scroll_end(self, *args, **kwargs) -> None:
        if not kwargs.get("y_axis", True):
            super().scroll_end(*args, **kwargs)
            return
        self.following = True
        self._pending_scroll_end += 1
        if kwargs.get("immediate", False):
            try:
                super().scroll_end(*args, **kwargs)
            finally:
                self._finish_pending_scroll_end()
            return
        super().scroll_end(*args, **kwargs)
        if not self.call_after_refresh(self._finish_pending_scroll_end):
            self._finish_pending_scroll_end()

    def on_resize(self, event: events.Resize) -> None:
        was_following = self.following
        super().on_resize(event)
        if was_following and self.max_scroll_y > 0:
            self.scroll_end(animate=False, x_axis=False)


class ConversationScreen(Screen):
    """Read-only screen showing an agent's header details and live event stream."""

    CSS = """
    ConversationScreen { background: #0d1117; }
    #conv-header {
        height: auto;
        padding: 0 1;
        background: #161b22;
        border-bottom: tall #30363d;
    }
    #conv-body {
        height: 1fr;
        padding: 0 1;
        background: #0d1117;
    }
    """

    BINDINGS = [
        Binding("escape", "pop_screen", "Geri"),
    ]

    def __init__(self, store: Store, agent_key: str, **kw) -> None:
        super().__init__(**kw)
        self.store = store
        self.agent_key = agent_key
        self._seen_count = 0
        self._ready = False

    def compose(self) -> ComposeResult:
        yield Static(id="conv-header")
        yield TranscriptLog(id="conv-body")
        yield Footer()

    def on_mount(self) -> None:
        self._ready = True
        self._render_header()
        self.refresh_events()
        self.query_one(TranscriptLog).focus()

    def on_unmount(self) -> None:
        self._ready = False

    def _parent_title(self, agent: Agent) -> str | None:
        if (
            not agent.parent_agent_key
            or agent.parent_agent_key == main_agent_key(agent.session_key)
        ):
            return None
        parent = self.store.find_agent(agent.parent_agent_key)
        if parent is None or parent.is_main:
            return None
        return parent.title or parent.key

    def _render_header(self, now: datetime | None = None) -> None:
        header = self.query_one("#conv-header", Static)
        agent = self.store.find_agent(self.agent_key)
        if agent is None:
            text = Text()
            text.append(f"? {self.agent_key}", style=DIM)
            text.append(" · unavailable", style=DIM)
            header.update(text)
            return

        now = now or datetime.now(timezone.utc)
        color = model_color(agent.model)
        title = "main" if agent.is_main else (agent.title or agent.key)
        parent_title = self._parent_title(agent)

        line1 = Text()
        line1.append(f"{STATUS_ICONS[agent.status]} ", style=STATUS_COLORS[agent.status])
        line1.append(title, style=f"bold {color}")
        if parent_title and parent_title != "main":
            line1.append(f" ← {parent_title}", style=DIM)
        line1.append("  ·  ", style=DIM)
        line1.append(agent.source.value, style=SOURCE_COLORS.get(agent.source, color))
        line1.append(" · ", style=DIM)
        line1.append(short_model(agent.model), style=color)
        if agent.effort:
            line1.append(f" · {agent.effort}", style=DIM)

        line2 = Text()
        line2.append(agent.status.value, style=STATUS_COLORS[agent.status])
        line2.append(f" · {fmt_duration(agent.duration_seconds(now))}", style=DIM)
        line2.append(f" · {agent.calls} calls", style=DIM)
        line2.append(
            f" · in {fmt_tokens(agent.in_tokens)}"
            f" / cached {fmt_tokens(agent.cached_tokens)}"
            f" / out {fmt_tokens(agent.out_tokens)}"
            f" / reasoning {fmt_tokens(agent.reasoning_tokens)}",
            style=DIM,
        )

        out = Text()
        out.append_text(line1)
        out.append("\n")
        out.append_text(line2)

        quiet_text = ""
        if agent.status == Status.RUNNING and agent.last_write is not None:
            quiet_s = (_as_utc(now) - _as_utc(agent.last_write)).total_seconds()
            if quiet_s >= config.QUIET_SECONDS:
                quiet_text = f"quiet {int(quiet_s // 60)}m"

        last_action = " ".join(agent.last_action.split()) if agent.last_action else ""
        if last_action or quiet_text:
            out.append("\n")
            if last_action:
                out.append(last_action, style=DIM)
            if quiet_text:
                if last_action:
                    out.append("  ·  ", style=DIM)
                out.append(quiet_text, style=DIM)

        header.update(out)

    def refresh_events(self) -> None:
        """Update header and incrementally append newly recorded events from Store."""
        if not self._ready:
            return
        self._render_header()
        log = self.query_one(TranscriptLog)
        total = self.store.event_count(self.agent_key)
        if total == 0:
            return
        if total < self._seen_count:
            log.clear()
            self._seen_count = 0
        new_count = total - self._seen_count
        if new_count <= 0:
            return

        follow = log.following
        events_buf = self.store.events(self.agent_key)
        start = max(0, len(events_buf) - new_count)
        appended = False
        for ev in events_buf[start:]:
            rendered = render_event(ev)
            if rendered is not None:
                log.write(rendered, expand=True, scroll_end=False)
                appended = True
        self._seen_count = total
        if appended and follow:
            log.scroll_end(animate=False, x_axis=False)

    def action_pop_screen(self) -> None:
        """Close the conversation screen and return to the dashboard."""
        self.app.pop_screen()
