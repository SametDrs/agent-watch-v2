"""Read-only conversation screen and event renderers."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

from rich import box
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.markdown import Markdown
from rich.measure import Measurement
from rich.panel import Panel
from rich.rule import Rule
from rich.segment import Segment
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, RichLog, Static

from agent_watch import config
from agent_watch.model import Agent, Event, EventKind, Source, Status, main_agent_key
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

BG = "#0f1117"
SURFACE = "#161b26"
SURFACE_HI = "#1f2533"
BORDER = "#2a3142"
TEXT = "#e6e9ef"
MUTED = "#9aa3b5"

TOOL_ARGS_CAP = 500
TOOL_RESULT_CAP = 800
EMPTY_THINKING_TEXT = "(thought — text not stored by this tool)"

_ARG_MAX_LINES = 4
_ARG_MAX_CHARS = 200
_ARG_MAX_KEYS = 12
_SUMMARY_ARG_KEYS = (
    "description",
    "command",
    "cmd",
    "CommandLine",
    "file_path",
    "notebook_path",
    "path",
    "Path",
    "AbsolutePath",
    "TargetFile",
    "File",
    "DirectoryPath",
    "pattern",
    "Pattern",
    "query",
    "Query",
    "url",
)


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _clip_body(text: str, limit: int) -> str:
    """Return up to the first limit characters of text, adding an ellipsis when clipped."""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _infer_source(agent_key: str) -> Source:
    """Infer source runtime from an event's agent key."""
    if agent_key.startswith("agy:"):
        return Source.AGY
    if agent_key.startswith("codex:"):
        return Source.CODEX
    return Source.CLAUDE


def _format_ts(ts: datetime | None) -> str:
    """Format an optional event timestamp as local HH:MM:SS."""
    if ts is None:
        return ""
    return _as_utc(ts).astimezone().strftime("%H:%M:%S")


def _label_row(left: Text, ts: datetime | None) -> RenderableType:
    """Place a block label on the left with an optional right-aligned DIM timestamp."""
    stamp = _format_ts(ts)
    if not stamp:
        return left
    row = Table.grid(expand=True)
    row.add_column(ratio=1, overflow="fold")
    row.add_column(justify="right", no_wrap=True)
    row.add_row(left, Text(f"  {stamp}", style=DIM))
    return row


class _QuotedPrompt:
    """Render a user prompt with an accent left bar on every wrapped line."""

    def __init__(self, body: str, ts: datetime | None = None) -> None:
        self.body = body.rstrip("\r\n")
        self.ts = ts

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        bar = Segment("▌ ", console.get_style(ACCENT))
        newline = Segment.line()
        inner_width = max(1, options.max_width - 2)
        inner_opts = options.update(width=inner_width)

        label = Text("you", style=f"bold {ACCENT}")
        stamp = _format_ts(self.ts)
        if stamp and inner_width >= label.cell_len + len(stamp) + 2:
            header_line = Text(no_wrap=True)
            header_line.append_text(label)
            pad = inner_width - label.cell_len - len(stamp)
            header_line.append(" " * pad + stamp, style=DIM)
        else:
            header_line = label

        for line in console.render_lines(header_line, inner_opts, pad=False):
            yield bar
            yield from line
            yield newline

        if self.body:
            body_text = Text(self.body, style=TEXT, overflow="fold")
            for line in console.render_lines(body_text, inner_opts, pad=False):
                yield bar
                yield from line
                yield newline

    def __rich_measure__(
        self, console: Console, options: ConsoleOptions
    ) -> Measurement:
        inner_width = max(1, options.max_width - 2)
        inner_opts = options.update(width=inner_width)
        measure = Measurement.get(console, inner_opts, Text(self.body or "you"))
        return Measurement(
            min(options.max_width, max(5, measure.minimum + 2)),
            min(options.max_width, max(16, measure.maximum + 2)),
        )


def _decode_partial_json_string(fragment: str) -> str:
    """Decode a truncated JSON string literal starting with a quote."""
    body = fragment[1:]
    if body.endswith("…"):
        body = body[:-1]
    for trim in range(min(6, len(body)) + 1):
        candidate = body if trim == 0 else body[:-trim]
        backslashes = len(candidate) - len(candidate.rstrip("\\"))
        if backslashes % 2 == 1:
            continue
        try:
            decoded = json.loads(f'"{candidate}"')
            return f"{decoded}…"
        except ValueError:
            continue
    return body.replace("\\n", "\n").replace("\\t", "\t") + "…"


def _parse_truncated_json_dict(text: str) -> dict[str, Any] | None:
    """Extract available top-level key/value pairs from a truncated JSON object string."""
    if text.endswith("…"):
        text = text[:-1]
    decoder = json.JSONDecoder()
    n = len(text)
    idx = text.find("{")
    if idx < 0:
        return None
    idx += 1
    out: dict[str, Any] = {}
    while idx < n:
        while idx < n and text[idx] in " \t\r\n,":
            idx += 1
        if idx >= n or text[idx] == "}":
            break
        if text[idx] != '"':
            break
        try:
            key, idx = decoder.raw_decode(text, idx)
        except ValueError:
            break
        if not isinstance(key, str):
            break
        while idx < n and text[idx] in " \t\r\n":
            idx += 1
        if idx >= n or text[idx] != ":":
            break
        idx += 1
        while idx < n and text[idx] in " \t\r\n":
            idx += 1
        if idx >= n:
            out[key] = "…"
            break
        try:
            val, idx = decoder.raw_decode(text, idx)
            out[key] = val
        except ValueError:
            if text[idx] == '"':
                out[key] = _decode_partial_json_string(text[idx:])
            else:
                snippet = text[idx:].strip()
                out[key] = f"{snippet}…" if snippet else "…"
            break
    return out or None


def _parse_tool_args(raw: Any) -> dict[str, Any] | None:
    """Parse tool arguments into a dict when JSON-encoded (including truncated dicts)."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else None
    except (ValueError, RecursionError):
        return _parse_truncated_json_dict(stripped)


def _stringify_value(val: Any, depth: int = 0) -> str:
    """Format a parsed JSON value cleanly with real newlines."""
    if isinstance(val, str):
        return val.replace("\r\n", "\n").replace("\r", "\n")
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, list):
        if not val:
            return "[]"
        parts = [_stringify_value(item, depth + 1) for item in val]
        if depth == 0 and not any("\n" in p for p in parts) and sum(len(p) for p in parts) <= 80:
            return ", ".join(parts)
        indent = "  " * depth
        lines: list[str] = []
        for p in parts:
            sub = p.splitlines() or [""]
            lines.append(f"{indent}- {sub[0]}")
            for cont in sub[1:]:
                lines.append(f"{indent}  {cont}")
        return "\n".join(lines)
    if isinstance(val, dict):
        if not val:
            return "{}"
        indent = "  " * depth
        lines = []
        for k, v in val.items():
            s = _stringify_value(v, depth + 1)
            if "\n" in s:
                lines.append(f"{indent}{k}:")
                for sub in s.splitlines():
                    lines.append(f"{indent}  {sub}")
            else:
                lines.append(f"{indent}{k}: {s}")
        return "\n".join(lines)
    return str(val)


def _clip_arg_value(text: str, max_lines: int = _ARG_MAX_LINES, max_chars: int = _ARG_MAX_CHARS) -> str:
    """Clip a formatted argument value to max_lines and max_chars with ellipsis."""
    lines = text.splitlines()
    if not lines:
        return ""
    clipped = len(lines) > max_lines
    lines = lines[:max_lines]
    out_lines: list[str] = []
    remaining = max_chars
    for idx, line in enumerate(lines):
        if remaining <= 0:
            clipped = True
            break
        if len(line) > remaining:
            out_lines.append(line[: max(1, remaining - 1)] + "…")
            clipped = clipped or idx < len(lines) - 1
            remaining = 0
            break
        out_lines.append(line)
        remaining -= len(line) + 1
    if clipped and (not out_lines or not out_lines[-1].endswith("…")):
        out_lines.append("…")
    return "\n".join(out_lines)


def _tool_header(ev: Event, args_dict: dict[str, Any] | None) -> Text:
    """Build the '▸ <ToolName> <summary>' header line for a tool call."""
    tool_name = (ev.tool or "").strip()
    raw_summary = " ".join(ev.summary.split()) if ev.summary else ""
    detail = ""
    if tool_name:
        if raw_summary == tool_name:
            detail = ""
        elif raw_summary.startswith(f"{tool_name} "):
            detail = raw_summary[len(tool_name) + 1 :].strip()
        elif raw_summary:
            detail = raw_summary
    elif raw_summary:
        tool_name = raw_summary
    else:
        tool_name = "tool"

    if not detail and args_dict:
        for key in _SUMMARY_ARG_KEYS:
            val = args_dict.get(key)
            if isinstance(val, str) and val.strip():
                collapsed = " ".join(val.split())
                detail = collapsed[:79] + "…" if len(collapsed) > 80 else collapsed
                break

    header = Text()
    header.append(f"▸ {tool_name}", style=f"bold {ACCENT}")
    if detail:
        header.append(f" {detail}", style=MUTED)
    return header


def _render_tool_panel(args_dict: dict[str, Any] | None, raw_body: Any) -> Panel | None:
    """Render tool arguments inside a calm rounded BORDER panel without a title."""
    if args_dict is not None:
        if not args_dict:
            return None
        table = Table.grid(padding=(0, 0))
        table.add_column(no_wrap=True)
        table.add_column(overflow="fold")
        items = list(args_dict.items())
        for key, val in items[:_ARG_MAX_KEYS]:
            formatted = _clip_arg_value(_stringify_value(val))
            val_text = Text('""', style=DIM) if formatted == "" else Text(formatted, style=TEXT)
            table.add_row(Text(f"{key}  ", style=MUTED), val_text)
        if len(items) > _ARG_MAX_KEYS:
            table.add_row(Text("", style=MUTED), Text(f"… (+{len(items) - _ARG_MAX_KEYS} more)", style=DIM))
        return Panel(table, box=box.ROUNDED, border_style=BORDER, padding=(0, 1), safe_box=False)

    if raw_body is None:
        return None
    raw_text = str(raw_body).replace("\r\n", "\n").replace("\r", "\n")
    if not raw_text:
        return None
    return Panel(
        Text(_clip_body(raw_text, TOOL_ARGS_CAP), style=TEXT),
        box=box.ROUNDED,
        border_style=BORDER,
        padding=(0, 1),
        safe_box=False,
    )


def render_event(ev: Event) -> RenderableType | None:
    """Render a normalized Event as a role-labelled Rich block, or None to hide it."""
    if ev.kind in (EventKind.USAGE, EventKind.START):
        return None

    if ev.kind == EventKind.PROMPT:
        body = ev.body or ev.summary
        return Group(_QuotedPrompt(body, ev.ts), Text(""))

    if ev.kind == EventKind.TEXT:
        source_color = SOURCE_COLORS.get(_infer_source(ev.agent_key), ACCENT)
        header = _label_row(Text("assistant", style=f"bold {source_color}"), ev.ts)
        body = ev.body or ev.summary
        parts: list[RenderableType] = [header]
        if body:
            parts.append(Markdown(body, style=TEXT))
        parts.append(Text(""))
        return Group(*parts)

    if ev.kind == EventKind.THINKING:
        header = _label_row(Text("◌ thinking", style=f"italic {DIM}"), ev.ts)
        if ev.body.strip():
            body_part = Text(_clip_body(ev.body, TOOL_RESULT_CAP), style=f"italic {DIM}")
        else:
            body_part = Text(EMPTY_THINKING_TEXT, style=f"italic {DIM}")
        return Group(header, body_part, Text(""))

    if ev.kind == EventKind.TOOL_START:
        args_dict = _parse_tool_args(ev.body)
        header = _label_row(_tool_header(ev, args_dict), ev.ts)
        parts = [header]
        if (panel := _render_tool_panel(args_dict, ev.body)) is not None:
            parts.append(panel)
        parts.append(Text(""))
        return Group(*parts)

    if ev.kind == EventKind.TOOL_END:
        err_color = STATUS_COLORS[Status.FAILED]
        is_err = ev.status == Status.FAILED or bool(ev.summary and "error" in ev.summary.lower())
        header_label = f"↳ result · {ev.summary}" if ev.summary else "↳ result"
        header = _label_row(Text(header_label, style=err_color if is_err else DIM), ev.ts)
        parts = [header]
        if ev.body:
            parts.append(
                Text(_clip_body(ev.body, TOOL_RESULT_CAP), style=err_color if is_err else MUTED)
            )
        parts.append(Text(""))
        return Group(*parts)

    if ev.kind == EventKind.ERROR:
        err_color = STATUS_COLORS[Status.FAILED]
        title = Text("✗ error", style=f"bold {err_color}")
        if ev.summary:
            title.append(" · ", style=DIM)
            title.append(ev.summary, style=err_color)
        parts = [_label_row(title, ev.ts)]
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
        parts = [Rule(line, characters="─", style=color, align="center")]
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

    CSS = f"""
    ConversationScreen {{
        background: {BG};
        color: {TEXT};
    }}
    #conv-header {{
        height: auto;
        padding: 0 1;
        background: {SURFACE};
        color: {TEXT};
        border: round {BORDER};
    }}
    #conv-body {{
        height: 1fr;
        padding: 0 2;
        background: {BG};
        color: {TEXT};
        scrollbar-size: 1 1;
        scrollbar-color: {BORDER};
        scrollbar-color-hover: {ACCENT};
        scrollbar-color-active: {ACCENT};
        scrollbar-background: {SURFACE};
        scrollbar-background-hover: {SURFACE};
        scrollbar-background-active: {SURFACE};
        &:focus {{
            background-tint: transparent;
        }}
    }}
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
        title = "main" if agent.is_main else (agent.title or agent.key)
        parent_title = self._parent_title(agent)

        line1 = Text()
        line1.append(f"{STATUS_ICONS[agent.status]} ", style=STATUS_COLORS[agent.status])
        line1.append(title, style=f"bold {TEXT}")
        if parent_title and parent_title != "main":
            line1.append(f" ← {parent_title}", style=DIM)
        line1.append(" · ", style=DIM)
        line1.append(agent.source.value, style=SOURCE_COLORS.get(agent.source, TEXT))
        line1.append(" · ", style=DIM)
        line1.append(short_model(agent.model), style=TEXT)
        if agent.effort:
            line1.append(" · ", style=DIM)
            line1.append(agent.effort, style=MUTED)

        line2 = Text()
        line2.append(agent.status.value, style=STATUS_COLORS[agent.status])
        line2.append(" · ", style=DIM)
        line2.append(fmt_duration(agent.duration_seconds(now)), style=TEXT)
        line2.append(" · ", style=DIM)
        line2.append(str(agent.calls), style=TEXT)
        line2.append(" calls", style=MUTED)
        for label, value in (
            ("in ", fmt_tokens(agent.in_tokens)),
            ("cached ", fmt_tokens(agent.cached_tokens)),
            ("out ", fmt_tokens(agent.out_tokens)),
            ("reasoning ", fmt_tokens(agent.reasoning_tokens)),
        ):
            line2.append(" · ", style=DIM)
            line2.append(label, style=MUTED)
            line2.append(value, style=TEXT)

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
                out.append(last_action, style=MUTED)
            if quiet_text:
                if last_action:
                    out.append(" · ", style=DIM)
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
