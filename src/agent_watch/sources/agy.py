"""Antigravity (agy) conversation transcript source."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable

from agent_watch import config
from agent_watch.model import (
    Event,
    EventKind,
    STANDALONE_SESSION_KEY,
    Status,
    effort_from_model,
    truncate_body,
    utc,
)
from agent_watch.tail import JsonlTail
from agent_watch.titles import short_title

_RUNNING_MTIME_SECONDS = 15.0
_MAX_TITLE_LEN = 60
_MAX_SUMMARY_LEN = 80
_PATH_ARG_KEYS = ("Path", "AbsolutePath", "TargetFile", "File", "DirectoryPath")
_TOOL_ARG_KEYS = (*_PATH_ARG_KEYS, "Query", "Pattern", "CommandLine")
_PATH_KEY_SET = frozenset(_PATH_ARG_KEYS)

_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.DOTALL)
_TAG_BLOCK_RE = re.compile(r"<([A-Za-z_][A-Za-z0-9_:-]*)(?:\s[^>]*)?>.*?</\1>", re.DOTALL)


def strip_user_request(content: str) -> str:
    """Extract user prompt from <USER_REQUEST> or strip <...> tag blocks."""
    if not isinstance(content, str):
        return ""
    if match := _USER_REQUEST_RE.search(content):
        return match.group(1).strip()
    cleaned = _TAG_BLOCK_RE.sub("", content)
    return cleaned.strip()


def _decode_arg_value(raw: Any) -> str:
    """Decode JSON-encoded-in-string agy tool args, falling back to raw text."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (ValueError, TypeError, RecursionError):
            return raw
        if decoded is None:
            return ""
        return decoded if isinstance(decoded, str) else str(decoded)
    return str(raw)


def _path_basename(path_str: str) -> str:
    """Return final path component across both Windows and POSIX separators."""
    stripped = path_str.rstrip("/\\")
    if not stripped:
        return path_str
    sep_idx = max(stripped.rfind("/"), stripped.rfind("\\"))
    return stripped[sep_idx + 1 :] if sep_idx >= 0 else stripped


def summarize_agy_tool(name: str, args: dict[str, Any]) -> str:
    """Format a concise <=80-char summary for an agy tool invocation."""
    tool_name = name if isinstance(name, str) else str(name or "")
    if not isinstance(args, dict):
        return tool_name[:_MAX_SUMMARY_LEN]

    for key in _TOOL_ARG_KEYS:
        if key not in args:
            continue
        decoded = " ".join(_decode_arg_value(args[key]).split())
        if not decoded:
            continue
        candidate = f"{tool_name} {decoded}" if tool_name else decoded
        if key in _PATH_KEY_SET and len(candidate) > _MAX_SUMMARY_LEN:
            base = _path_basename(decoded)
            candidate = f"{tool_name} {base}" if tool_name else base
        return candidate[:_MAX_SUMMARY_LEN]

    return tool_name[:_MAX_SUMMARY_LEN]


def _clip(text: str, n: int = _MAX_SUMMARY_LEN) -> str:
    """Collapse whitespace to one line and clip to n chars."""
    return " ".join(text.split())[:n]


def _parse_ts(raw: Any, fallback: datetime | None = None) -> datetime:
    """Parse an ISO timestamp string to UTC datetime, falling back safely."""
    if isinstance(raw, str) and raw.strip():
        try:
            return utc(raw)
        except (ValueError, TypeError):
            pass
    return fallback if fallback is not None else datetime.now(timezone.utc)


def _as_int(raw: Any) -> int:
    """Convert numeric JSON values to int, ignoring bools and malformed values."""
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    return 0


def _make_usage_event(
    ts: datetime,
    session_key: str,
    agent_key: str,
    usage_src: dict[str, Any],
    model: str | None,
    effort: str | None = None,
) -> Event:
    """Build a normalized USAGE event from agy token counters."""
    tokens_in = _as_int(usage_src.get("input_tokens")) + _as_int(usage_src.get("cache_read_tokens"))
    tokens_out = _as_int(usage_src.get("output_tokens"))
    return Event(
        ts=ts,
        session_key=session_key,
        agent_key=agent_key,
        kind=EventKind.USAGE,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        ctx_tokens=tokens_in,
        model=model,
        effort=effort,
    )


def parse_transcript_line(d: dict[str, Any], conv_id: str, session_key: str) -> list[Event]:
    """Parse one agy transcript.jsonl dict into normalized Event objects."""
    if not isinstance(d, dict):
        return []

    agent_key = f"agy:{conv_id}"
    ts = _parse_ts(d.get("created_at"))
    step_type = d.get("type")

    if step_type == "USER_INPUT":
        content = d.get("content")
        stripped = strip_user_request(content) if isinstance(content, str) else ""
        events: list[Event] = []
        if d.get("step_index") == 0:
            events.append(
                Event(
                    ts=ts,
                    session_key=session_key,
                    agent_key=agent_key,
                    kind=EventKind.START,
                    title=short_title(stripped, _MAX_TITLE_LEN),
                )
            )
        summary = _clip(stripped)
        if summary:
            events.append(
                Event(
                    ts=ts,
                    session_key=session_key,
                    agent_key=agent_key,
                    kind=EventKind.PROMPT,
                    summary=summary,
                    body=stripped,
                )
            )
        return events

    if step_type == "PLANNER_RESPONSE":
        events = []
        raw_model = d.get("model")
        model = raw_model if isinstance(raw_model, str) and raw_model else None
        effort = (effort_from_model(model) or None) if model else None
        events.append(_make_usage_event(ts, session_key, agent_key, d, model, effort))

        raw_thinking = d.get("thinking")
        if isinstance(raw_thinking, str) and _clip(raw_thinking):
            events.append(
                Event(
                    ts=ts,
                    session_key=session_key,
                    agent_key=agent_key,
                    kind=EventKind.THINKING,
                    summary=_clip(raw_thinking),
                    body=raw_thinking,
                    model=model,
                    effort=effort,
                )
            )

        tool_calls = d.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                tool_name = tc.get("name")
                if not isinstance(tool_name, str) or not tool_name:
                    continue
                raw_args = tc.get("args")
                args = raw_args if isinstance(raw_args, dict) else {}
                events.append(
                    Event(
                        ts=ts,
                        session_key=session_key,
                        agent_key=agent_key,
                        kind=EventKind.TOOL_START,
                        tool=tool_name,
                        summary=summarize_agy_tool(tool_name, args),
                        body=json.dumps(args, default=str),
                        model=model,
                        effort=effort,
                    )
                )

        content = d.get("content")
        if isinstance(content, str):
            text_summary = _clip(content)
            if text_summary:
                events.append(
                    Event(
                        ts=ts,
                        session_key=session_key,
                        agent_key=agent_key,
                        kind=EventKind.TEXT,
                        summary=text_summary,
                        body=content,
                        model=model,
                        effort=effort,
                    )
                )
        return events

    if step_type == "GENERIC":
        raw_content = d.get("content")
        body = truncate_body(raw_content) if isinstance(raw_content, str) else ""
        raw_status = d.get("status")
        is_err = isinstance(raw_status, str) and raw_status.strip().lower() in ("error", "failed")
        return [
            Event(
                ts=ts,
                session_key=session_key,
                agent_key=agent_key,
                kind=EventKind.TOOL_END,
                summary="error" if is_err else "",
                body=body,
            )
        ]

    return []


@dataclass
class AgyConvInfo:
    """Tracked state and metadata for a single agy conversation."""

    conv_id: str
    first_input_ts: datetime | None = None
    first_input_text: str = ""
    title: str = ""
    last_mtime: float = 0.0
    last_type: str = ""
    last_has_tool_calls: bool = False


@dataclass
class AgyScanResult:
    """Result emitted by a single AgyScanner.scan() pass."""

    events: list[Event] = field(default_factory=list)
    convs: list[AgyConvInfo] = field(default_factory=list)
    running: set[str] = field(default_factory=set)
    ended: dict[str, Status] = field(default_factory=dict)


def _norm_path(path_str: str) -> str:
    """Normalize path separators, trailing slashes, and Windows drive casing."""
    normalized = path_str.replace("\\", "/").rstrip("/")
    if os.name == "nt" or re.match(r"^[A-Za-z]:(?:/|$)", normalized):
        return normalized.lower()
    return normalized


def _extract_conv_flag(cmdline: list[str]) -> str | None:
    """Return --conversation argument value if present in cmdline."""
    for idx, arg in enumerate(cmdline):
        if not isinstance(arg, str):
            continue
        if arg == "--conversation" and idx + 1 < len(cmdline) and isinstance(cmdline[idx + 1], str):
            return cmdline[idx + 1]
        if arg.startswith("--conversation="):
            return arg.split("=", 1)[1]
    return None


def _extract_explicit_prompt(cmdline: list[str]) -> str:
    """Return non-empty prompt passed via -p/--prompt in cmdline, if any."""
    for idx, arg in enumerate(cmdline):
        if not isinstance(arg, str):
            continue
        if arg in ("-p", "--prompt") and idx + 1 < len(cmdline) and isinstance(cmdline[idx + 1], str):
            return cmdline[idx + 1].strip()
        if arg.startswith("-p="):
            return arg[3:].strip()
        if arg.startswith("--prompt="):
            return arg[9:].strip()
    return ""


def _proc_matches_conv(
    proc: Any,
    info: AgyConvInfo,
) -> bool:
    """Return True if a live agy process matches the given conversation."""
    raw_cmdline = getattr(proc, "cmdline", None)
    cmdline = raw_cmdline if isinstance(raw_cmdline, list) else []

    conv_flag = _extract_conv_flag(cmdline)
    if conv_flag is not None:
        return conv_flag == info.conv_id

    prompt_norm = " ".join(info.first_input_text.split())
    if prompt_norm:
        for arg in cmdline:
            if not isinstance(arg, str):
                continue
            candidates = [arg]
            if arg.startswith("-p=") and len(arg) > 3:
                candidates.append(arg[3:])
            elif arg.startswith("--prompt=") and len(arg) > 9:
                candidates.append(arg[9:])
            for cand in candidates:
                cand_norm = " ".join(cand.split())
                if not cand_norm:
                    continue
                if prompt_norm == cand_norm or prompt_norm in cand_norm:
                    return True
                if len(cand_norm) >= 15 and prompt_norm.startswith(cand_norm):
                    return True

    # If the process carries a specific non-matching prompt, do not fall back to cwd.
    if _extract_explicit_prompt(cmdline):
        return False

    cwd = getattr(proc, "cwd", None)
    if isinstance(cwd, str) and cwd.strip():
        norm_cwd = _norm_path(cwd)
        if len(norm_cwd) > 3 and prompt_norm and norm_cwd in _norm_path(info.first_input_text):
            return True

    return False


class AgyScanner:
    """Polls agy brain transcripts for conversation events."""

    def __init__(
        self,
        agy_home: Path | None = None,
        window_hours: int = config.WINDOW_HOURS,
        proc_provider: Callable[[], list[Any]] | None = None,
    ) -> None:
        self.agy_home: Path = Path(agy_home) if agy_home is not None else config.agy_home()
        self.window_hours: int = window_hours
        self._proc_provider: Callable[[], list[Any]] | None = proc_provider

        self._tails: dict[str, JsonlTail] = {}
        self._convs: dict[str, AgyConvInfo] = {}
        self._transcript_titles: dict[str, str] = {}
        self._last_event_ts: dict[str, datetime] = {}
        self._seen_first_input: set[str] = set()
        self._statuses: dict[str, Status] = {}

    def _get_processes(self) -> list[Any]:
        """Return active agy process objects using injected provider or lazy procs import."""
        if self._proc_provider is not None:
            return self._proc_provider()
        try:
            from agent_watch import procs
        except ImportError:
            return []
        return procs.agy_processes()

    def _ingest_transcript(self, conv_id: str, transcript_path: Path, st: os.stat_result) -> list[Event]:
        """Read newly appended transcript lines and update AgyConvInfo state."""
        tail = self._tails.get(conv_id)
        if tail is None:
            tail = JsonlTail(transcript_path, skip_substrings=())
            self._tails[conv_id] = tail

        if conv_id not in self._convs or st.st_size < tail.offset:
            self._convs[conv_id] = AgyConvInfo(conv_id=conv_id, last_mtime=st.st_mtime)
            self._transcript_titles.pop(conv_id, None)
            self._last_event_ts.pop(conv_id, None)
            self._seen_first_input.discard(conv_id)

        info = self._convs[conv_id]
        info.last_mtime = st.st_mtime

        if not tail.changed():
            return []

        events: list[Event] = []
        for row in tail.read_new():
            row_type = row.get("type")
            if isinstance(row_type, str):
                info.last_type = row_type
                tool_calls = row.get("tool_calls")
                info.last_has_tool_calls = (
                    row_type == "PLANNER_RESPONSE"
                    and isinstance(tool_calls, list)
                    and len(tool_calls) > 0
                )
            else:
                info.last_type = ""
                info.last_has_tool_calls = False

            raw_ts = row.get("created_at")
            if isinstance(raw_ts, str) and raw_ts.strip():
                try:
                    self._last_event_ts[conv_id] = utc(raw_ts)
                except (ValueError, TypeError):
                    pass

            if row_type == "USER_INPUT" and (
                row.get("step_index") == 0 or conv_id not in self._seen_first_input
            ):
                self._seen_first_input.add(conv_id)
                if isinstance(raw_ts, str) and raw_ts.strip():
                    try:
                        info.first_input_ts = utc(raw_ts)
                    except (ValueError, TypeError):
                        pass
                content = row.get("content")
                if isinstance(content, str):
                    stripped = strip_user_request(content)
                    info.first_input_text = stripped
                    self._transcript_titles[conv_id] = short_title(stripped, _MAX_TITLE_LEN)

            events.extend(parse_transcript_line(row, conv_id, STANDALONE_SESSION_KEY))

        return events

    def scan(self, now: datetime | None = None, mtime_now: float | None = None) -> AgyScanResult:
        """Scan brain transcripts in window and return new events and live status."""
        ref_mtime = mtime_now if mtime_now is not None else time.time()
        if now is not None:
            window_end_ts = now.timestamp()
        elif mtime_now is not None:
            window_end_ts = mtime_now
        else:
            window_end_ts = ref_mtime
        cutoff_ts = window_end_ts - self.window_hours * 3600

        brain_dir = self.agy_home / "brain"
        if not brain_dir.is_dir():
            return AgyScanResult()

        try:
            entries = sorted(brain_dir.iterdir(), key=lambda p: p.name)
        except OSError:
            return AgyScanResult()

        discovered: list[tuple[str, Path, os.stat_result]] = []
        for conv_dir in entries:
            if not conv_dir.is_dir():
                continue
            transcript_path = conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
            try:
                st = transcript_path.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff_ts:
                continue
            discovered.append((conv_dir.name, transcript_path, st))

        if not discovered:
            return AgyScanResult()

        result = AgyScanResult()
        in_window: list[AgyConvInfo] = []

        for conv_id, transcript_path, st in discovered:
            result.events.extend(self._ingest_transcript(conv_id, transcript_path, st))
            info = self._convs[conv_id]
            info.title = self._transcript_titles.get(conv_id, "")
            in_window.append(info)

        procs_list = self._get_processes()

        for info in in_window:
            matched_proc = any(
                _proc_matches_conv(proc, info) for proc in procs_list
            )
            age_seconds = ref_mtime - info.last_mtime
            if matched_proc or age_seconds < _RUNNING_MTIME_SECONDS:
                status = Status.RUNNING
                result.running.add(info.conv_id)
            elif info.last_type == "PLANNER_RESPONSE" and not info.last_has_tool_calls:
                status = Status.DONE
            else:
                status = Status.FAILED

            prev_status = self._statuses.get(info.conv_id)
            self._statuses[info.conv_id] = status

            if prev_status in (Status.DONE, Status.FAILED) and status == Status.RUNNING:
                reopen_ts = self._last_event_ts.get(info.conv_id) or datetime.fromtimestamp(
                    info.last_mtime, tz=timezone.utc
                )
                result.events.append(
                    Event(
                        ts=reopen_ts,
                        session_key=STANDALONE_SESSION_KEY,
                        agent_key=f"agy:{info.conv_id}",
                        kind=EventKind.START,
                        status=Status.RUNNING,
                    )
                )
            elif status != prev_status and status in (Status.DONE, Status.FAILED):
                result.ended[info.conv_id] = status
                end_ts = self._last_event_ts.get(info.conv_id) or datetime.fromtimestamp(
                    info.last_mtime, tz=timezone.utc
                )
                result.events.append(
                    Event(
                        ts=end_ts,
                        session_key=STANDALONE_SESSION_KEY,
                        agent_key=f"agy:{info.conv_id}",
                        kind=EventKind.END,
                        status=status,
                        summary="cancelled" if status == Status.FAILED else "",
                    )
                )

        result.convs = in_window
        return result
