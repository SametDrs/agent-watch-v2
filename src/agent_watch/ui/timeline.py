"""Timeline view: one row per agent with a start-to-end bar scaled to the tab's time span."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from rich.cells import cell_len
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from agent_watch.model import Agent, main_agent_key
from agent_watch.ui.theme import (
    ACCENT,
    BG,
    BORDER,
    DIM,
    STATUS_COLORS,
    STATUS_ICONS,
    SURFACE_HI,
    TEXT,
    fmt_duration,
)
from agent_watch.ui.widgets import AgentCard, _as_utc, _ellipsize, _now

LABEL_W = 28  # status icon + space + ellipsized title
DUR_W = 7
MIN_BAR_W = 6
FALLBACK_W = 80
_FAR = datetime.max.replace(tzinfo=timezone.utc)


def _end_of(agent: Agent, now: datetime) -> datetime:
    """Return the agent's end (or now while running), never before its start."""
    assert agent.started is not None
    start = _as_utc(agent.started)
    end = _as_utc(agent.ended) if agent.ended is not None else now
    return max(start, end)


def time_span(agents: list[Agent], now: datetime) -> tuple[datetime, datetime] | None:
    """Return (t0, t1) covering all started agents, at least one second wide; None if none started."""
    started = [a for a in agents if a.started is not None]
    if not started:
        return None
    t0 = min(_as_utc(a.started) for a in started if a.started is not None)
    t1 = max(_end_of(a, now) for a in started)
    return t0, max(t1, t0 + timedelta(seconds=1))


def bar_text(
    agent: Agent, span: tuple[datetime, datetime] | None, width: int, now: datetime
) -> Text:
    """Render the agent's bar into exactly `width` cells using half blocks."""
    out = Text(no_wrap=True)
    if span is None or agent.started is None:
        out.append(" " * width)
        return out
    t0, t1 = span
    total = (t1 - t0).total_seconds()
    units = width * 2  # half-cell resolution
    start = (_as_utc(agent.started) - t0).total_seconds() / total
    end = (_end_of(agent, now) - t0).total_seconds() / total
    s = min(units - 1, max(0, round(start * units)))
    e = min(units, max(s + 1, round(end * units)))  # at least a half block, even for zero length
    color = STATUS_COLORS[agent.status]
    runs: list[tuple[str, str]] = []
    for cell in range(width):
        left = s <= 2 * cell < e
        right = s <= 2 * cell + 1 < e
        ch = "█" if left and right else "▌" if left else "▐" if right else " "
        if runs and runs[-1][0] == ch:
            runs[-1] = (ch, runs[-1][1] + ch)
        else:
            runs.append((ch, ch))
    for ch, chunk in runs:
        out.append(chunk, style=color if ch != " " else "")
    return out


def axis_text(span: tuple[datetime, datetime] | None, width: int) -> Text:
    """Render a few time ticks (local HH:MM, or HH:MM:SS for spans under 10 min) over `width` cells."""
    out = Text(no_wrap=True)
    if span is None or width <= 0:
        out.append(" " * max(0, width))
        return out
    t0, t1 = span
    total = (t1 - t0).total_seconds()
    fmt = "%H:%M" if total >= 600 else "%H:%M:%S"
    n = max(2, min(6, width // 16))
    cells = [" "] * width
    last_end = -2
    for k in range(n):
        frac = k / (n - 1)
        label = (t0 + timedelta(seconds=total * frac)).astimezone().strftime(fmt)
        if len(label) > width:
            continue
        x = round(frac * (width - 1))
        start = min(max(0, x - len(label) // 2), width - len(label))
        if k == 0:
            start = 0
        elif k == n - 1:
            start = width - len(label)
        if start <= last_end + 1:
            continue
        cells[start:start + len(label)] = list(label)
        last_end = start + len(label) - 1
    out.append("".join(cells), style=DIM)
    return out


class TimelineRows(OptionList):
    """Scrollable rows of the timeline."""

    DEFAULT_CSS = f"""
    TimelineRows {{
        height: 1fr;
        background: {BG};
        color: {TEXT};
        border: none;
        padding: 0 1;
    }}
    TimelineRows:focus {{
        border: none;
        background-tint: transparent;
    }}
    TimelineRows > .option-list--option-highlighted,
    TimelineRows:focus > .option-list--option-highlighted {{
        color: {ACCENT};
        background: {SURFACE_HI};
        text-style: none;
    }}
    TimelineRows > .option-list--option-hover {{
        background: {SURFACE_HI};
    }}
    """


class TimelineView(Vertical):
    """Timeline of one tab's agents sorted by start time, with a time axis below."""

    DEFAULT_CSS = f"""
    TimelineView {{
        height: 1fr;
        background: {BG};
    }}
    TimelineView > #timeline-axis {{
        height: 2;
        padding: 0 1;
        background: {BG};
        border-top: solid {BORDER};
    }}
    """

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self._agents: list[Agent] = []
        self._scale_agents: list[Agent] = []
        self._now: datetime | None = None
        self._default_key: str | None = None
        self._keys: list[str] = []
        self._is_ready = False

    def compose(self) -> ComposeResult:
        yield TimelineRows(id="timeline-rows")
        yield Static(id="timeline-axis")

    def on_mount(self) -> None:
        self._is_ready = True
        self._draw()

    def on_resize(self) -> None:
        self._draw()

    @property
    def keys(self) -> list[str]:
        """Agent keys in row order."""
        return list(self._keys)

    def set_agents(
        self,
        agents: list[Agent],
        scale_agents: list[Agent] | None = None,
        now: datetime | None = None,
        default_key: str | None = None,
    ) -> None:
        """Show `agents`; the time scale comes from `scale_agents` (default: the same list)."""
        self._agents = list(agents)
        self._scale_agents = list(scale_agents) if scale_agents is not None else self._agents
        self._now = now
        self._default_key = default_key
        if self._is_ready:
            self._draw()

    def _bar_width(self) -> int:
        width = (self.size.width or FALLBACK_W) - 4 - 1  # row padding, option padding, vertical scrollbar
        return max(MIN_BAR_W, width - LABEL_W - 2 - DUR_W)

    def _row(self, agent: Agent, span, bar_w: int, now: datetime) -> Text:
        is_main = agent.is_main or agent.key == main_agent_key(agent.session_key)
        raw = "main" if agent.is_main else (agent.title or agent.key)
        title = _ellipsize(raw, LABEL_W - 2)
        row = Text(no_wrap=True)
        row.append(f"{STATUS_ICONS[agent.status]} ", style=STATUS_COLORS[agent.status])
        row.append(title, style="bold" if is_main else "")
        row.append(" " * (LABEL_W - 2 - cell_len(title) + 1))
        row.append_text(bar_text(agent, span, bar_w, now))
        dur = fmt_duration(agent.duration_seconds(now) if agent.started else None)
        if len(dur) > DUR_W:
            dur = ">99h"
        row.append(" " + " " * (DUR_W - len(dur)) + dur, style=DIM)
        return row

    def _draw(self) -> None:
        now = _as_utc(self._now) if self._now is not None else _now()
        rows = self.query_one(TimelineRows)
        ordered = sorted(
            self._agents,
            key=lambda a: (a.started is None, _as_utc(a.started) if a.started else _FAR, a.key),
        )
        span = time_span(self._scale_agents, now)
        bar_w = self._bar_width()
        prompts = [self._row(a, span, bar_w, now) for a in ordered]
        keys = [a.key for a in ordered]
        if keys == self._keys and rows.option_count == len(keys):
            for i, prompt in enumerate(prompts):
                rows.replace_option_prompt_at_index(i, prompt)
        else:
            idx = rows.highlighted
            keep = self._keys[idx] if idx is not None and idx < len(self._keys) else None
            self._keys = keys
            rows.clear_options()
            rows.add_options([Option(p, id=k) for p, k in zip(prompts, keys)])
            if keep in keys:
                rows.highlighted = keys.index(keep)
            elif self._default_key in keys:
                rows.highlighted = keys.index(self._default_key)
            elif keys:
                rows.highlighted = 0
        axis = Text(no_wrap=True)
        axis.append(" " * (LABEL_W + 1))
        axis.append_text(axis_text(span, bar_w))
        self.query_one("#timeline-axis", Static).update(axis)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Open the chosen agent's conversation exactly like clicking its card."""
        event.stop()
        if event.option.id is not None:
            self.post_message(AgentCard.Opened(str(event.option.id)))
