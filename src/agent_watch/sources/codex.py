"""OpenAI Codex CLI rollout scanner: turns rollout JSONL logs into normalized events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
from typing import Any, Callable

from agent_watch import config
from agent_watch.model import (
    Event,
    EventKind,
    Status,
    UNLINKED_SESSION_KEY,
    effort_from_model,
    truncate_body,
    utc,
)
from agent_watch.procs import ProcInfo
from agent_watch.tail import JsonlTail
from agent_watch.titles import short_title

_RUNNING_MTIME_SECONDS = 15.0
_MAX_TITLE_LEN = 60
_MAX_SUMMARY_LEN = 80
_PROMPT_PREFIX_LEN = 40
_ROLLOUT_STEM_RE = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(.+)$"
)
_PATCH_FILE_RE = re.compile(
    r"\*\*\*\s+(?:Update|Add|Delete)\s+File:\s*(.+)"
)
_HARNESS_TAG_PAIRS: tuple[tuple[str, str], ...] = (
    ("<environment_context>", "</environment_context>"),
    ("<permissions instructions>", "</permissions instructions>"),
    ("<user_instructions>", "</user_instructions>"),
    ("<collaboration_mode>", "</collaboration_mode>"),
    ("<app-context>", "</app-context>"),
)
_PATH_ARG_KEYS = ("file_path", "path")
_TOOL_ARG_KEYS = (
    "command",
    "cmd",
    *_PATH_ARG_KEYS,
    "pattern",
    "query",
    "url",
    "explanation",
    "workdir",
)


@dataclass
class CodexConv:
    """Tracked metadata for a single Codex CLI rollout."""

    rollout_id: str
    path: Path
    created: datetime
    first_prompt: str
    cwd: str
    model: str
    effort: str


@dataclass
class CodexLimits:
    """Rate-limit snapshot extracted from Codex token_count events."""

    used_percent: float
    window_minutes: int
    resets_at: datetime | None
    secondary_used_percent: float | None = None


@dataclass
class CodexScanResult:
    """Output of a single CodexScanner.scan() pass."""

    events: list[Event] = field(default_factory=list)
    convs: list[CodexConv] = field(default_factory=list)
    running: set[str] = field(default_factory=set)
    limits: CodexLimits | None = None


@dataclass
class _RolloutState:
    """Incremental tail and parse state for one rollout JSONL file."""

    tail: JsonlTail
    rollout_id: str
    parse_state: dict[str, Any] = field(default_factory=dict)
    emitted_any: bool = False


def _clip(text: str, n: int = _MAX_SUMMARY_LEN) -> str:
    """Collapse whitespace to one line and clip to n chars."""
    return " ".join(text.split())[:n]


def _rel(path: str, cwd: str) -> str:
    """Return path relative to cwd when under it, else unchanged."""
    if not cwd or not path:
        return path
    try:
        return str(Path(path).relative_to(cwd))
    except ValueError:
        return path


def _parse_ts(raw: Any, fallback: datetime | None = None) -> datetime:
    """Parse an ISO-8601 timestamp string into UTC datetime, falling back safely."""
    if isinstance(raw, str) and raw.strip():
        try:
            return utc(raw)
        except (ValueError, TypeError):
            pass
    return fallback if fallback is not None else datetime.now(timezone.utc)


def _parse_reset_ts(raw: Any) -> datetime | None:
    """Parse a Unix epoch (seconds/ms) or ISO timestamp into UTC datetime, or None."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        if not math.isfinite(raw) or raw <= 0:
            return None
        sec = raw / 1000.0 if raw > 1e12 else float(raw)
        try:
            return datetime.fromtimestamp(sec, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(raw, str) and raw.strip():
        try:
            return utc(raw)
        except (ValueError, TypeError):
            return None
    return None


def _as_int(raw: Any) -> int:
    """Convert numeric JSON values to int, ignoring bools and malformed values."""
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and math.isfinite(raw):
        return int(raw)
    return 0


def _as_float(raw: Any) -> float | None:
    """Convert numeric JSON values to finite float, ignoring bools and invalid values."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    val = float(raw)
    return val if math.isfinite(val) else None


def _is_harness_user_block(text: str) -> bool:
    """Return True if text is a Codex harness context preamble rather than user prompt."""
    stripped = text.strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    if lowered.startswith("# agents.md instructions"):
        return True
    for open_tag, close_tag in _HARNESS_TAG_PAIRS:
        if lowered.startswith(open_tag) and lowered.endswith(close_tag):
            return True
    return False


def _extract_message_text(content: Any, *, filter_harness: bool = False) -> str:
    """Extract text from a Codex message content string or block list."""
    if isinstance(content, str):
        if filter_harness and _is_harness_user_block(content):
            return ""
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            text: str | None = None
            if isinstance(block, dict):
                raw = block.get("text")
                if isinstance(raw, str) and raw:
                    text = raw
            elif isinstance(block, str) and block:
                text = block
            if text is None:
                continue
            if filter_harness and _is_harness_user_block(text):
                continue
            parts.append(text)
        return "\n".join(parts)
    return ""


def _extract_reasoning_text(payload: dict[str, Any]) -> str:
    """Extract reasoning summary text, returning '' for encrypted-only reasoning."""
    summary = payload.get("summary")
    if isinstance(summary, list):
        parts: list[str] = []
        for item in summary:
            if isinstance(item, dict):
                raw = item.get("text")
                if isinstance(raw, str) and raw:
                    parts.append(raw)
            elif isinstance(item, str) and item:
                parts.append(item)
        if parts:
            return "\n".join(parts)

    content = payload.get("content")
    if isinstance(content, str) and content:
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                raw = item.get("text") or item.get("thinking")
                if isinstance(raw, str) and raw:
                    parts.append(raw)
            elif isinstance(item, str) and item:
                parts.append(item)
        if parts:
            return "\n".join(parts)
    return ""


def _has_nonzero_exit(obj: Any) -> bool:
    """Return True if obj is a dict with a non-zero integer metadata.exit_code."""
    if not isinstance(obj, dict):
        return False
    meta = obj.get("metadata")
    if not isinstance(meta, dict):
        return False
    code = meta.get("exit_code")
    return isinstance(code, int) and not isinstance(code, bool) and code != 0


def _extract_output_text(output: Any) -> tuple[str, bool]:
    """Return (output_text, is_error) from a Codex tool output payload."""
    if isinstance(output, str):
        stripped = output.strip()
        parsed = None
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
            except (ValueError, RecursionError):
                parsed = None
        return output, _has_nonzero_exit(parsed)

    if isinstance(output, list):
        return _extract_message_text(output), False

    if isinstance(output, dict):
        inner = output.get("output")
        text = inner if isinstance(inner, str) else json.dumps(output, default=str)
        return text, _has_nonzero_exit(output)

    return "", False


def summarize_codex_tool(name: str, raw_args: str, cwd: str = "") -> str:
    """Format a concise <=80-char summary for a Codex tool call."""
    tool_name = name.strip() if isinstance(name, str) else str(name or "")
    if raw_args and raw_args.lstrip().startswith("{"):
        try:
            parsed = json.loads(raw_args)
        except (ValueError, RecursionError):
            parsed = None
        if isinstance(parsed, dict):
            for key in _TOOL_ARG_KEYS:
                val = parsed.get(key)
                if not isinstance(val, str) or not val.strip():
                    continue
                detail = _rel(val.strip(), cwd) if key in _PATH_ARG_KEYS else val.strip()
                return _clip(f"{tool_name} {detail}".rstrip())
            return _clip(tool_name)

    if tool_name == "apply_patch" and raw_args:
        if match := _PATCH_FILE_RE.search(raw_args):
            return _clip(f"{tool_name} {_rel(match.group(1).strip(), cwd)}")

    stripped = raw_args.strip() if isinstance(raw_args, str) else ""
    if stripped and stripped != "{}":
        return _clip(f"{tool_name} {stripped}".rstrip())
    return _clip(tool_name)


def _update_context_fields(payload: dict[str, Any], state: dict[str, Any]) -> None:
    """Extract cwd, model, and effort from session_meta or turn_context payloads."""
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        state["cwd"] = cwd

    collab = payload.get("collaboration_mode")
    collab_settings = collab.get("settings") if isinstance(collab, dict) else None

    prev_model = state.get("model")
    raw_model = payload.get("model")
    if not isinstance(raw_model, str) or not raw_model:
        raw_model = collab_settings.get("model") if isinstance(collab_settings, dict) else None
    model_changed = False
    if isinstance(raw_model, str) and raw_model:
        model_changed = raw_model != prev_model
        state["model"] = raw_model

    raw_effort = payload.get("effort") or payload.get("reasoning_effort")
    if not isinstance(raw_effort, str) or not raw_effort:
        raw_effort = (
            collab_settings.get("reasoning_effort")
            if isinstance(collab_settings, dict)
            else None
        )
    if isinstance(raw_effort, str) and raw_effort:
        state["effort"] = raw_effort
        state["effort_explicit"] = True
    elif state.get("model") and (not state.get("effort") or model_changed):
        inferred = effort_from_model(state["model"])
        if inferred:
            state["effort"] = inferred
            state["effort_explicit"] = False
        elif not state.get("effort_explicit"):
            state["effort"] = ""


def _reopen_status(state: dict[str, Any], *, any_end: bool = False) -> Status | None:
    """Clear END tracking and return RUNNING when reopening after an END."""
    had_end = bool(state.get("had_end"))
    is_timeout_failed = had_end and state.get("end_status") is Status.FAILED
    if not (is_timeout_failed or (any_end and had_end)):
        return None
    state["end_emitted"] = False
    state["had_end"] = False
    state.pop("end_status", None)
    return Status.RUNNING


def _emit_prompt(
    ts: datetime,
    session_key: str,
    agent_key: str,
    text: str,
    source: str,
    state: dict[str, Any],
) -> list[Event]:
    """Emit a deduplicated PROMPT event and update first_prompt/title state."""
    norm = " ".join(text.split())
    if not norm:
        return []
    dedup = state.get("_prompt_dedup")
    if isinstance(dedup, tuple) and len(dedup) == 2 and dedup[0] != source and dedup[1] == norm:
        state.pop("_prompt_dedup", None)
        state["task_complete"] = False
        return []
    state["_prompt_dedup"] = (source, norm)

    if not state.get("first_prompt"):
        state["first_prompt"] = text

    title: str | None = None
    if not state.get("titled"):
        state["titled"] = True
        title = short_title(text, _MAX_TITLE_LEN) or None

    status = _reopen_status(state, any_end=True)
    state["task_complete"] = False

    return [
        Event(
            ts=ts,
            session_key=session_key,
            agent_key=agent_key,
            kind=EventKind.PROMPT,
            summary=_clip(text),
            body=text,
            title=title,
            status=status,
            model=state.get("model") or None,
            effort=state.get("effort") or None,
        )
    ]


def _emit_text(
    ts: datetime,
    session_key: str,
    agent_key: str,
    text: str,
    source: str,
    state: dict[str, Any],
) -> list[Event]:
    """Emit a deduplicated TEXT event for an assistant/agent message."""
    norm = " ".join(text.split())
    if not norm:
        return []
    dedup = state.get("_text_dedup")
    if isinstance(dedup, tuple) and len(dedup) == 2 and dedup[0] != source and dedup[1] == norm:
        state.pop("_text_dedup", None)
        state["task_complete"] = False
        return []
    state["_text_dedup"] = (source, norm)
    status = _reopen_status(state)
    state["task_complete"] = False

    return [
        Event(
            ts=ts,
            session_key=session_key,
            agent_key=agent_key,
            kind=EventKind.TEXT,
            summary=_clip(text),
            body=text,
            status=status,
            model=state.get("model") or None,
            effort=state.get("effort") or None,
        )
    ]


def _parse_rate_limits(rate_limits: Any) -> CodexLimits | None:
    """Parse rate_limits.primary (and optional secondary) into CodexLimits."""
    if not isinstance(rate_limits, dict):
        return None
    primary = rate_limits.get("primary")
    if not isinstance(primary, dict):
        return None
    used_percent = _as_float(primary.get("used_percent"))
    if used_percent is None:
        return None
    window_minutes = _as_int(primary.get("window_minutes"))
    resets_at = _parse_reset_ts(primary.get("resets_at"))

    secondary_used: float | None = None
    secondary = rate_limits.get("secondary")
    if isinstance(secondary, dict):
        secondary_used = _as_float(secondary.get("used_percent"))

    return CodexLimits(
        used_percent=used_percent,
        window_minutes=window_minutes,
        resets_at=resets_at,
        secondary_used_percent=secondary_used,
    )


def _handle_token_count(
    ts: datetime,
    session_key: str,
    agent_key: str,
    payload: dict[str, Any],
    state: dict[str, Any],
) -> list[Event]:
    """Update rate limits and emit delta USAGE event from cumulative token counts."""
    if (limits := _parse_rate_limits(payload.get("rate_limits"))) is not None:
        state["limits"] = limits
        state["limits_ts"] = ts

    info = payload.get("info")
    if not isinstance(info, dict):
        return []

    total = info.get("total_token_usage")
    last = info.get("last_token_usage")
    if isinstance(total, dict):
        prev = state.get("last_total_usage")
        if not isinstance(prev, dict):
            prev = {
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
            }
        cur_in = _as_int(total.get("input_tokens"))
        cur_cached = _as_int(total.get("cached_input_tokens"))
        cur_out = _as_int(total.get("output_tokens"))
        cur_reason = _as_int(total.get("reasoning_output_tokens"))

        d_in = max(0, cur_in - _as_int(prev.get("input_tokens")))
        d_cached = max(0, cur_cached - _as_int(prev.get("cached_input_tokens")))
        d_out = max(0, cur_out - _as_int(prev.get("output_tokens")))
        d_reason = max(0, cur_reason - _as_int(prev.get("reasoning_output_tokens")))

        state["last_total_usage"] = {
            "input_tokens": cur_in,
            "cached_input_tokens": cur_cached,
            "output_tokens": cur_out,
            "reasoning_output_tokens": cur_reason,
        }
    elif isinstance(last, dict):
        d_in = max(0, _as_int(last.get("input_tokens")))
        d_cached = max(0, _as_int(last.get("cached_input_tokens")))
        d_out = max(0, _as_int(last.get("output_tokens")))
        d_reason = max(0, _as_int(last.get("reasoning_output_tokens")))
    else:
        return []

    if d_in == 0 and d_cached == 0 and d_out == 0 and d_reason == 0:
        return []

    ctx_tokens = (
        _as_int(last.get("input_tokens"))
        if isinstance(last, dict) and "input_tokens" in last
        else d_in
    )
    return [
        Event(
            ts=ts,
            session_key=session_key,
            agent_key=agent_key,
            kind=EventKind.USAGE,
            tokens_in=d_in,
            tokens_cached=d_cached,
            tokens_out=d_out,
            tokens_reasoning=d_reason,
            ctx_tokens=ctx_tokens,
            model=state.get("model") or None,
            effort=state.get("effort") or None,
        )
    ]


def _handle_response_item(
    ts: datetime,
    session_key: str,
    agent_key: str,
    payload: dict[str, Any],
    state: dict[str, Any],
) -> list[Event]:
    """Parse one Codex response_item payload into normalized Event objects."""
    ptype = payload.get("type")
    model = state.get("model") or None
    effort = state.get("effort") or None

    if ptype == "message":
        role = payload.get("role")
        if role == "user":
            text = _extract_message_text(payload.get("content"), filter_harness=True)
            return _emit_prompt(ts, session_key, agent_key, text, "response_item", state)
        if role == "assistant":
            text = _extract_message_text(payload.get("content"), filter_harness=False)
            return _emit_text(ts, session_key, agent_key, text, "response_item", state)
        return []

    if ptype == "reasoning":
        body = _extract_reasoning_text(payload)
        status = _reopen_status(state)
        state["task_complete"] = False
        return [
            Event(
                ts=ts,
                session_key=session_key,
                agent_key=agent_key,
                kind=EventKind.THINKING,
                summary=_clip(body),
                body=body,
                status=status,
                model=model,
                effort=effort,
            )
        ]

    if ptype in ("function_call", "custom_tool_call"):
        raw_name = payload.get("name")
        name = raw_name if isinstance(raw_name, str) else str(raw_name or "")
        call_id = str(payload.get("call_id") or "")
        if call_id and name:
            tool_names = state.setdefault("tool_names", {})
            if isinstance(tool_names, dict):
                tool_names[call_id] = name
        if ptype == "function_call":
            raw_args = payload.get("arguments")
        else:
            raw_args = payload.get("arguments") if "arguments" in payload else payload.get("input")
        if isinstance(raw_args, str):
            body = raw_args
        elif raw_args is not None:
            body = json.dumps(raw_args, default=str)
        else:
            body = ""
        status = _reopen_status(state)
        state["task_complete"] = False
        return [
            Event(
                ts=ts,
                session_key=session_key,
                agent_key=agent_key,
                kind=EventKind.TOOL_START,
                tool=name or None,
                summary=summarize_codex_tool(name, body, str(state.get("cwd") or "")),
                body=body,
                status=status,
                model=model,
                effort=effort,
            )
        ]

    if ptype in ("function_call_output", "custom_tool_call_output"):
        call_id = str(payload.get("call_id") or "")
        tool_names = state.get("tool_names")
        tool = tool_names.get(call_id, "tool") if isinstance(tool_names, dict) else "tool"
        out_text, inferred_err = _extract_output_text(payload.get("output"))
        is_err = bool(payload.get("is_error")) or inferred_err
        status = _reopen_status(state)
        state["task_complete"] = False
        return [
            Event(
                ts=ts,
                session_key=session_key,
                agent_key=agent_key,
                kind=EventKind.TOOL_END,
                tool=tool,
                summary=f"error: {tool}" if is_err else "",
                body=truncate_body(out_text),
                status=status,
                model=model,
                effort=effort,
            )
        ]

    return []


def _handle_event_msg(
    ts: datetime,
    session_key: str,
    agent_key: str,
    payload: dict[str, Any],
    state: dict[str, Any],
) -> list[Event]:
    """Parse one Codex event_msg payload into normalized Event objects."""
    ptype = payload.get("type")

    if ptype == "user_message":
        raw = payload.get("message")
        if not isinstance(raw, str):
            raw = payload.get("text")
        if isinstance(raw, str):
            return _emit_prompt(ts, session_key, agent_key, raw, "event_msg", state)
        return []

    if ptype == "agent_message":
        raw = payload.get("message")
        if not isinstance(raw, str):
            raw = payload.get("text")
        if isinstance(raw, str):
            return _emit_text(ts, session_key, agent_key, raw, "event_msg", state)
        return []

    if ptype == "token_count":
        return _handle_token_count(ts, session_key, agent_key, payload, state)

    if ptype in ("task_complete", "turn_complete"):
        state["task_complete"] = True
        state.pop("_prompt_dedup", None)
        state.pop("_text_dedup", None)
        if state.get("end_emitted") and state.get("end_status") is not Status.FAILED:
            return []
        state["end_emitted"] = True
        state["had_end"] = True
        state["end_status"] = Status.DONE
        return [
            Event(
                ts=ts,
                session_key=session_key,
                agent_key=agent_key,
                kind=EventKind.END,
                status=Status.DONE,
                model=state.get("model") or None,
                effort=state.get("effort") or None,
            )
        ]

    if ptype in ("task_started", "turn_started"):
        state["task_complete"] = False
        state["end_emitted"] = False
        state.pop("_prompt_dedup", None)
        state.pop("_text_dedup", None)
        return []

    if ptype == "turn_aborted":
        state["task_complete"] = False
        return []

    return []


def parse_rollout_line(
    d: dict[str, Any],
    rollout_id: str,
    session_key: str,
    state: dict[str, Any],
) -> list[Event]:
    """Parse one Codex rollout JSONL dict into normalized Event objects."""
    if not isinstance(d, dict) or not rollout_id:
        return []

    raw_ts = d.get("timestamp")
    payload = d.get("payload")
    if not isinstance(raw_ts, str) and isinstance(payload, dict):
        raw_ts = payload.get("timestamp")
    ts = _parse_ts(raw_ts, fallback=state.get("last_ts"))
    if isinstance(raw_ts, str) and raw_ts.strip():
        state["last_ts"] = ts
    elif "last_ts" not in state:
        state["last_ts"] = ts

    agent_key = f"codex:{rollout_id}"
    rec_type = d.get("type")

    if rec_type == "session_meta" and state.get("created") is None:
        ctx = payload if isinstance(payload, dict) else d
        meta_ts = ctx.get("timestamp")
        state["created"] = _parse_ts(meta_ts, fallback=ts)
    state.setdefault("created", ts)

    if rec_type in ("session_meta", "turn_context"):
        ctx = payload if isinstance(payload, dict) else d
        _update_context_fields(ctx, state)
        if not state.get("started"):
            state["started"] = True
            return [
                Event(
                    ts=ts,
                    session_key=session_key,
                    agent_key=agent_key,
                    kind=EventKind.START,
                    model=state.get("model") or None,
                    effort=state.get("effort") or None,
                )
            ]
        return []

    if not isinstance(payload, dict):
        return []

    if rec_type == "response_item":
        return _handle_response_item(ts, session_key, agent_key, payload, state)

    if rec_type == "event_msg":
        return _handle_event_msg(ts, session_key, agent_key, payload, state)

    return []


def _rollout_id_from_path(path: Path) -> str:
    """Extract UUID from rollout-<ts>-<id>.jsonl filename, falling back to stem."""
    match = _ROLLOUT_STEM_RE.match(path.stem)
    return match.group(1) if match else path.stem


def _proc_matches_rollout(proc: Any, info: CodexConv) -> bool:
    """Return True if a live Codex process cmdline matches rollout prompt or id."""
    raw_cmdline = getattr(proc, "cmdline", None)
    if not isinstance(raw_cmdline, list):
        return False

    norm_prompt = " ".join(info.first_prompt.split())
    prompt_prefix = norm_prompt[:_PROMPT_PREFIX_LEN]

    for arg in raw_cmdline:
        if not isinstance(arg, str):
            continue
        if len(info.rollout_id) >= 8 and info.rollout_id in arg:
            return True
        if not norm_prompt:
            continue
        norm_arg = " ".join(arg.split())
        if not norm_arg:
            continue
        if norm_arg == norm_prompt:
            return True
        if len(prompt_prefix) >= 8 and prompt_prefix in norm_arg:
            return True
        if len(norm_arg) >= 15 and norm_prompt.startswith(norm_arg):
            return True
    return False


class CodexScanner:
    """Incrementally scans Codex CLI rollout logs for today and yesterday."""

    def __init__(
        self,
        codex_home: Path | None = None,
        window_hours: int = config.WINDOW_HOURS,
        proc_provider: Callable[[], list[ProcInfo]] | None = None,
    ) -> None:
        self.codex_home: Path = Path(codex_home) if codex_home is not None else config.codex_home()
        self.window_hours: int = window_hours
        self._proc_provider: Callable[[], list[ProcInfo]] | None = proc_provider
        self._rollouts: dict[Path, _RolloutState] = {}

    def _get_processes(self) -> list[ProcInfo]:
        """Return active Codex processes via injected provider or procs.codex_processes."""
        if self._proc_provider is not None:
            return self._proc_provider()
        try:
            from agent_watch import procs
        except ImportError:
            return []
        return procs.codex_processes()

    def _active_date_dirs(self, now_utc: datetime) -> list[Path]:
        """Return existing sessions/YYYY/MM/DD dirs for today and yesterday (UTC & local)."""
        now_local = now_utc.astimezone()
        dates = {
            now_utc.date(),
            now_utc.date() - timedelta(days=1),
            now_local.date(),
            now_local.date() - timedelta(days=1),
        }
        sessions_dir = self.codex_home / "sessions"
        dirs: list[Path] = []
        for day in sorted(dates):
            day_dir = sessions_dir / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
            if day_dir.is_dir():
                dirs.append(day_dir)
        return dirs

    def _ingest_rollout(self, path: Path, size: int) -> list[Event]:
        """Read newly appended JSONL lines for path and return normalized events."""
        rstate = self._rollouts.get(path)
        if rstate is None:
            rstate = _RolloutState(
                tail=JsonlTail(path, skip_substrings=()),
                rollout_id=_rollout_id_from_path(path),
            )
            self._rollouts[path] = rstate
        elif size < rstate.tail.offset:
            rstate.parse_state = {}
            rstate.emitted_any = False
            rstate.rollout_id = _rollout_id_from_path(path)

        if not rstate.tail.changed():
            return []

        events: list[Event] = []
        for rec in rstate.tail.read_new():
            try:
                if (
                    rec.get("type") == "session_meta"
                    and isinstance(rec.get("payload"), dict)
                    and rec["payload"].get("id")
                    and not rstate.emitted_any
                ):
                    rstate.rollout_id = str(rec["payload"]["id"])
                evs = parse_rollout_line(
                    rec, rstate.rollout_id, UNLINKED_SESSION_KEY, rstate.parse_state
                )
            except Exception:  # noqa: BLE001 - never raise on malformed records
                continue
            if evs:
                rstate.emitted_any = True
                events.extend(evs)
        return events

    def scan(self, now: datetime | None = None) -> CodexScanResult:
        """Scan in-window Codex rollouts and return events, convs, running keys, and limits."""
        if now is None:
            now_utc = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now_utc = now.replace(tzinfo=timezone.utc)
        else:
            now_utc = now.astimezone(timezone.utc)

        ref_ts = now_utc.timestamp()
        cutoff_ts = ref_ts - self.window_hours * 3600

        discovered: list[tuple[Path, Any]] = []
        for day_dir in self._active_date_dirs(now_utc):
            try:
                candidates = sorted(day_dir.glob("*.jsonl"))
            except OSError:
                continue
            for path in candidates:
                try:
                    st = path.stat()
                except OSError:
                    continue
                if st.st_mtime < cutoff_ts:
                    continue
                discovered.append((path, st))

        if not discovered:
            return CodexScanResult()

        discovered.sort(key=lambda item: (item[1].st_mtime, str(item[0])))
        procs_list = self._get_processes()

        result = CodexScanResult()
        best_limits_ts: datetime | None = None

        for path, st in discovered:
            result.events.extend(self._ingest_rollout(path, st.st_size))
            rstate = self._rollouts[path]
            pstate = rstate.parse_state
            mtime_dt = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
            created = pstate.get("created") or pstate.get("last_ts") or mtime_dt

            conv = CodexConv(
                rollout_id=rstate.rollout_id,
                path=path,
                created=created,
                first_prompt=str(pstate.get("first_prompt") or ""),
                cwd=str(pstate.get("cwd") or ""),
                model=str(pstate.get("model") or ""),
                effort=str(pstate.get("effort") or ""),
            )
            result.convs.append(conv)

            agent_key = f"codex:{rstate.rollout_id}"
            age_seconds = ref_ts - st.st_mtime
            is_running = bool(procs_list) and (
                age_seconds <= _RUNNING_MTIME_SECONDS
                or any(_proc_matches_rollout(proc, conv) for proc in procs_list)
            )
            if is_running:
                result.running.add(agent_key)
            elif rstate.emitted_any and not pstate.get("end_emitted"):
                status = Status.DONE if pstate.get("task_complete") else Status.FAILED
                pstate["end_emitted"] = True
                pstate["had_end"] = True
                pstate["end_status"] = status
                end_ts = pstate.get("last_ts") or mtime_dt
                result.events.append(
                    Event(
                        ts=end_ts,
                        session_key=UNLINKED_SESSION_KEY,
                        agent_key=agent_key,
                        kind=EventKind.END,
                        status=status,
                        model=conv.model or None,
                        effort=conv.effort or None,
                    )
                )

            if (lim := pstate.get("limits")) is not None:
                lim_ts = pstate.get("limits_ts") or mtime_dt
                if best_limits_ts is None or lim_ts >= best_limits_ts:
                    best_limits_ts = lim_ts
                    result.limits = lim

        return result
