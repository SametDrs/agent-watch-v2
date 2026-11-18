"""Textual widgets for the agent-watch dashboard."""

from __future__ import annotations

from datetime import datetime, timezone

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widgets import OptionList, RichLog, Static
from textual.widgets.option_list import Option

from agent_watch import config
from agent_watch.config import model_color
from agent_watch.model import Agent, Event, Status, main_agent_key
from agent_watch.sources.codex import CodexLimits
from agent_watch.ui.card_mascot import (
    CANVAS_W,
    compose,
    mascot_state,
    render as render_mascot,
    sprite,
)
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

CARD_WIDTH = 52
CARD_HEIGHT = 7
CARD_INNER = CARD_WIDTH - 4
MASCOT_GAP = 2
CARD_TEXT_WIDTH = CARD_INNER - CANVAS_W - MASCOT_GAP


def _ellipsize(text: str, width: int) -> str:
    """Truncate text to width terminal cells, adding an ellipsis."""
    text = " ".join(text.split())
    if cell_len(text) <= width:
        return text
    out = ""
    for ch in text:
        if cell_len(out + ch) > width - 1:
            break
        out += ch
    return out + "…"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _card_sort_key(agent: Agent) -> tuple[bool, bool, datetime, str]:
    """Order the main agent first, then others by start time and key."""
    far = datetime.max.replace(tzinfo=timezone.utc)
    is_main = agent.is_main or agent.key == main_agent_key(agent.session_key)
    started = _as_utc(agent.started) if agent.started is not None else far
    return (not is_main, agent.started is None, started, agent.key)


class AgentCard(Static):
    """Bordered card summarizing one agent with a pixel mascot."""

    can_focus = True

    class Opened(Message):
        """Posted when an agent card is clicked or activated with Enter."""

        def __init__(self, agent_key: str) -> None:
            super().__init__()
            self.agent_key = agent_key

    DEFAULT_CSS = f"""
    AgentCard {{
        width: {CARD_WIDTH};
        height: {CARD_HEIGHT};
        border: round {STATUS_COLORS[Status.PENDING]};
        padding: 0 1;
        margin: 0 1 0 0;
    }}
    AgentCard:focus {{ background: #161b22; }}
    AgentCard.-running {{ border: round {STATUS_COLORS[Status.RUNNING]}; }}
    AgentCard.-done {{ border: round {STATUS_COLORS[Status.DONE]}; }}
    AgentCard.-failed {{ border: round {STATUS_COLORS[Status.FAILED]}; }}
    AgentCard.-pending {{ border: round {STATUS_COLORS[Status.PENDING]}; }}
    AgentCard.-stale {{ border: round {STATUS_COLORS[Status.STALE]}; }}
    """

    def __init__(
        self,
        agent: Agent,
        parent_title: str | None = None,
        tick: int = 0,
        **kw,
    ) -> None:
        self.agent = agent
        self.parent_title = parent_title
        self._tick = tick
        super().__init__(self.render_agent(None, tick=tick), **kw)
        self._apply_status_class()

    def _apply_status_class(self) -> None:
        for status in Status:
            self.set_class(status == self.agent.status, f"-{status.value}")

    def render_agent(self, now: datetime | None = None, tick: int = 0) -> Text:
        """Build the five-line mascot + details card content."""
        agent = self.agent
        now = now or _now()
        color = model_color(agent.model)
        width = CARD_TEXT_WIDTH

        # Line 1: bold title + dim parent subagent label
        line1 = Text(no_wrap=True)
        title = "main" if agent.is_main else (agent.title or agent.key)
        show_parent = bool(
            self.parent_title
            and self.parent_title != "main"
            and (
                agent.parent_agent_key is None
                or agent.parent_agent_key != main_agent_key(agent.session_key)
            )
        )
        if show_parent:
            suffix = f" ← {self.parent_title}"
            if cell_len(suffix) > width // 2:
                suffix = _ellipsize(suffix, width // 2)
            line1.append(_ellipsize(title, max(4, width - cell_len(suffix))), style=f"bold {color}")
            line1.append(suffix, style=DIM)
        else:
            line1.append(_ellipsize(title, width), style=f"bold {color}")

        # Line 2: source · short_model · effort (effort omitted when empty)
        line2 = Text(no_wrap=True)
        src = agent.source.value
        mdl = short_model(agent.model)
        eff = agent.effort
        eff_suffix = f" · {eff}" if eff else ""
        prefix = f"{src} · "
        avail_mdl = max(1, width - cell_len(prefix) - cell_len(eff_suffix))
        mdl_str = _ellipsize(mdl, avail_mdl) if cell_len(mdl) > avail_mdl else mdl
        line2.append(src, style=SOURCE_COLORS.get(agent.source, color))
        line2.append(" · ", style=DIM)
        line2.append(mdl_str, style=color)
        if eff:
            line2.append(" · ", style=DIM)
            line2.append(eff, style=DIM)

        # Line 3: elapsed · <in+out> tok + dim (<out> out)
        line3 = Text(no_wrap=True)
        elapsed = fmt_duration(agent.duration_seconds(now))
        total_tok = fmt_tokens(agent.in_tokens + agent.out_tokens)
        out_tok = fmt_tokens(agent.out_tokens)
        head = f"{elapsed} · {total_tok} tok"
        tail = f" ({out_tok} out)"
        if cell_len(head) + cell_len(tail) > width:
            head = _ellipsize(head, max(1, width - cell_len(tail)))
        line3.append(head)
        line3.append(tail, style=DIM)

        # Line 4: status icon + last_action
        line4 = Text(no_wrap=True)
        icon = STATUS_ICONS[agent.status]
        line4.append(icon, style=STATUS_COLORS[agent.status])
        if agent.last_action:
            line4.append(" " + _ellipsize(agent.last_action, max(1, width - cell_len(icon) - 1)))

        # Line 5: dim quiet hint when RUNNING and quiet >= QUIET_SECONDS
        line5 = Text(no_wrap=True)
        if agent.status == Status.RUNNING and agent.last_write is not None:
            quiet_s = (_as_utc(now) - _as_utc(agent.last_write)).total_seconds()
            if quiet_s >= config.QUIET_SECONDS:
                line5.append(f"quiet {int(quiet_s // 60)}m", style=DIM)

        state = mascot_state(agent, now)
        mascot = render_mascot(compose(sprite(agent.source, state, tick), state, tick))
        mascot_lines = mascot.split("\n", allow_blank=True)

        out = Text(no_wrap=True, overflow="ellipsis")
        for i, right in enumerate((line1, line2, line3, line4, line5)):
            if i:
                out.append("\n")
            if i < len(mascot_lines):
                out.append_text(mascot_lines[i])
            if right.plain:
                out.append(" " * MASCOT_GAP)
                out.append_text(right)
        return out

    def update_agent(
        self,
        agent: Agent,
        now: datetime | None = None,
        tick: int | None = None,
    ) -> None:
        """Replace the displayed agent and re-render."""
        self.agent = agent
        if tick is not None:
            self._tick = tick
        self._apply_status_class()
        self.update(self.render_agent(now, tick=self._tick), layout=False)

    def on_click(self, event: events.Click) -> None:
        """Post Opened when clicked."""
        event.stop()
        self.post_message(self.Opened(self.agent.key))

    def on_key(self, event: events.Key) -> None:
        """Post Opened when Enter is pressed while focused."""
        if event.key == "enter":
            event.stop()
            self.post_message(self.Opened(self.agent.key))


class CardGrid(VerticalScroll):
    """Wrapping grid of agent cards keyed by agent key."""

    DEFAULT_CSS = f"""
    CardGrid {{
        layout: grid;
        grid-size: 3;
        grid-rows: {CARD_HEIGHT};
        grid-gutter: 0;
        height: 1fr;
    }}
    """

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self._cards: dict[str, AgentCard] = {}
        self._tick = 0

    def on_mount(self) -> None:
        """Start the shared mascot animation timer."""
        self.set_interval(config.MASCOT_TICK, self._advance_tick)

    @property
    def cards(self) -> dict[str, AgentCard]:
        """Mapping of agent key to its card widget."""
        return self._cards

    def _advance_tick(self) -> None:
        """Advance the shared mascot tick and redraw only visible cards."""
        self._tick += 1
        if not self._cards:
            return
        viewport = self.scrollable_content_region
        if not viewport:
            return
        now = _now()
        for card in self._cards.values():
            if not card.display:
                continue
            region = card.region
            if region and viewport.intersection(region):
                card.update_agent(card.agent, now, tick=self._tick)

    def set_agents(self, agents: list[Agent], parents: dict[str, str] | None = None) -> None:
        """Add, update and remove cards ordered with main first, then by started."""
        parents = parents or {}
        ordered = sorted(agents, key=_card_sort_key)
        wanted = {a.key for a in ordered}
        for key in [k for k in self._cards if k not in wanted]:
            self._cards.pop(key).remove()
        now = _now()
        reordered: dict[str, AgentCard] = {}
        for agent in ordered:
            card = self._cards.get(agent.key)
            parent_title = parents.get(agent.key)
            if card is None:
                card = AgentCard(agent, parent_title, tick=self._tick)
                self.mount(card)
            else:
                card.parent_title = parent_title
                card.update_agent(agent, now, tick=self._tick)
            reordered[agent.key] = card
        self._cards = reordered

        desired = list(reordered.values())
        desired_set = set(desired)
        current = [c for c in self.children if c in desired_set]
        if current != desired:
            for i, card in enumerate(desired):
                if i == 0:
                    if current and current[0] is not card:
                        self.move_child(card, before=current[0])
                        current = [c for c in self.children if c in desired_set]
                else:
                    prev = desired[i - 1]
                    children = self.children
                    if children.index(card) != children.index(prev) + 1:
                        self.move_child(card, after=prev)

    def on_resize(self) -> None:
        """Fit as many fixed-width columns as the width minus vertical scrollbar allows."""
        avail = max(0, self.size.width - self.styles.scrollbar_size_vertical)
        cols = max(1, avail // (CARD_WIDTH + 1))
        self.styles.grid_size_columns = cols


class AgentList(OptionList):
    """Sidebar list of agents ordered by start time."""

    DEFAULT_CSS = "AgentList { width: 32; height: 1fr; }"

    class Selected(Message):
        """Posted when an agent row is selected."""

        def __init__(self, agent_key: str) -> None:
            super().__init__()
            self.agent_key = agent_key

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self._keys: list[str] = []

    def _row(self, agent: Agent) -> Text:
        width = max(20, (self.size.width or 32) - 4)
        icon = STATUS_ICONS[agent.status]
        dur = fmt_duration(agent.duration_seconds())
        title = _ellipsize(agent.title or agent.key, max(5, width - len(dur) - 3))
        pad = max(1, width - cell_len(title) - len(dur) - 2)
        running = agent.status == Status.RUNNING
        text = Text(no_wrap=True)
        text.append(f"{icon} ", style=STATUS_COLORS[agent.status])
        text.append(title, style=f"bold {ACCENT}" if running else "")
        text.append(" " * pad + dur, style=DIM)
        return text

    def set_agents(self, agents: list[Agent], default_key: str | None = None) -> None:
        """Rebuild rows, keeping the highlighted agent or falling back to default_key / index 0."""
        far = datetime.max.replace(tzinfo=timezone.utc)
        ordered = sorted(agents, key=lambda a: (a.started is None, a.started or far, a.key))
        idx = self.highlighted
        keep = self._keys[idx] if idx is not None and idx < len(self._keys) else None
        self._keys = [a.key for a in ordered]
        self.clear_options()
        self.add_options([Option(self._row(a), id=a.key) for a in ordered])
        if keep in self._keys:
            self.highlighted = self._keys.index(keep)
        elif default_key in self._keys:
            self.highlighted = self._keys.index(default_key)
        elif self._keys:
            self.highlighted = 0

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Translate option selection into AgentList.Selected."""
        event.stop()
        if event.option.id is not None:
            self.post_message(self.Selected(str(event.option.id)))


class EventLog(RichLog):
    """Scrolling log of agent events (last 500 lines)."""

    def __init__(self, **kw) -> None:
        kw.setdefault("max_lines", 500)
        kw.setdefault("markup", False)
        kw.setdefault("wrap", False)
        super().__init__(**kw)

    def add_event(self, event: Event, title: str, color: str) -> None:
        """Append one formatted event line."""
        ts = event.ts.astimezone().strftime("%H:%M:%S")
        summary = event.summary or (event.tool or event.kind.value)
        line = Text(no_wrap=True)
        line.append(f"{ts}  ", style=DIM)
        line.append(title, style=color)
        line.append("  → ", style=DIM)
        line.append(" ".join(summary.split()))
        self.write(line)


class TotalsBar(Static):
    """One-line totals summary."""

    DEFAULT_CSS = "TotalsBar { height: 1; padding: 0 1; }"

    def update_totals(self, totals: dict, elapsed: float | None) -> None:
        """Render the totals line."""
        text = Text(no_wrap=True)
        items = [
            ("agents", f"{totals.get('agents_done', 0)}/{totals.get('agents_total', 0)} done"),
            ("running", str(totals.get("running", 0))),
            ("calls", str(totals.get("calls", 0))),
            ("out", fmt_tokens(totals.get("out_tokens", 0))),
            ("time", fmt_duration(elapsed)),
        ]
        for i, (label, value) in enumerate(items):
            if i:
                text.append("  ")
            text.append(f"{label} ", style=DIM)
            text.append("[", style=DIM)
            text.append(value, style=f"bold {ACCENT}")
            text.append("]", style=DIM)
        self.update(text)


class QuotaPanel(Static):
    """Remaining-quota bars per provider."""

    def update_quota(
        self,
        data: dict[str, tuple[float | None, datetime | None]],
        codex: CodexLimits | None = None,
    ) -> None:
        """Render one bar row per quota entry plus optional Codex rate-limit line."""
        text = Text(no_wrap=True)
        for i, (name, (frac, reset)) in enumerate(data.items()):
            if i:
                text.append("\n")
            text.append(f"{name} ")
            if frac is None:
                text.append("n/a", style=DIM)
            else:
                frac = min(1.0, max(0.0, frac))
                filled = round(frac * 10)
                color = "#3fb950" if frac > 0.5 else ACCENT if frac > 0.2 else "#f85149"
                text.append("█" * filled, style=color)
                text.append("░" * (10 - filled), style=DIM)
                text.append(f" {round(frac * 100)}%")
            if reset is not None:
                text.append(f"  resets {_as_utc(reset).astimezone().strftime('%H:%M')}", style=DIM)
        if codex is not None:
            if data:
                text.append("\n")
            used = round(min(100.0, max(0.0, codex.used_percent)))
            color = "#3fb950" if used < 50 else ACCENT if used < 80 else "#f85149"
            text.append("codex  ")
            text.append(f"{used}% used", style=color)
            if codex.resets_at is not None:
                reset_str = _as_utc(codex.resets_at).astimezone().strftime("%H:%M")
                text.append(f" · resets {reset_str}", style=DIM)
        self.update(text)
