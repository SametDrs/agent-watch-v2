"""Claude Code transcript scanner: turns JSONL transcripts into normalized events."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import config
from ..model import Event, EventKind, ShellCall, Status, main_agent_key, truncate_body, utc
from ..procs import pid_alive
from ..tail import JsonlTail
from ..titles import short_title

SHELL_TOOLS = ("Bash", "PowerShell")
BG_RE = re.compile(r"background with ID:\s*([\w-]+)")
TASK_ID_RE = re.compile(r"<task-id>([^<]+)</task-id>")
STATUS_RE = re.compile(r"<status>([^<]+)</status>")
FAIL_WORDS = ("error", "fail", "kill", "cancel")


def _clip(text: str, n: int = 80) -> str:
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


def summarize_tool(name: str, inp: dict, cwd: str = "") -> str:
    """Return a short one-line summary of a tool call (max 80 chars)."""
    inp = inp if isinstance(inp, dict) else {}
    if name in ("Bash", "PowerShell"):
        detail = inp.get("description") or str(inp.get("command", ""))[:40]
    elif name in ("Read", "Edit", "Write", "NotebookEdit"):
        detail = _rel(str(inp.get("file_path") or inp.get("notebook_path") or ""), cwd)
    elif name in ("Grep", "Glob"):
        detail = inp.get("pattern", "")
    elif name == "Agent":
        detail = inp.get("description", "")
    elif name in ("WebFetch", "WebSearch"):
        detail = inp.get("url") or inp.get("query") or ""
    else:
        return _clip(name)
    return _clip(f"{name} {detail}".rstrip())


@dataclass
class SessionInfo:
    """A Claude session known to the scanner."""

    key: str
    name: str
    cwd: str
    status: str
    started: datetime | None = None


@dataclass
class ScanResult:
    """Output of one scan."""

    events: list[Event] = field(default_factory=list)
    shell_calls: list[ShellCall] = field(default_factory=list)
    sessions: list[SessionInfo] = field(default_factory=list)
    live_session_keys: set[str] = field(default_factory=set)
    reset_session_keys: set[str] = field(default_factory=set)


@dataclass
class _Sub:
    """State for one subagent transcript."""

    tail: JsonlTail
    session_id: str
    agent_id: str
    meta_path: Path
    meta_stat: tuple[int, int] | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    started: bool = False
    ended: bool = False
    last_ts: datetime | None = None


@dataclass
class _Main:
    """State for one main transcript."""

    tail: JsonlTail
    session_id: str
    title: str = ""
    cwd: str = ""
    started: datetime | None = None
    effort: str = ""


def _first_text(msg: Any) -> str:
    """Return the first plain text of a user message (str or text blocks)."""
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            return block["text"]
    return ""


def _text_of(content: Any) -> str:
    """Flatten a tool_result content value to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict):
                raw = b.get("text")
                if isinstance(raw, str) and raw:
                    parts.append(raw)
            elif isinstance(b, str) and b:
                parts.append(b)
        return "\n".join(parts)
    return ""


class ClaudeScanner:
    """Incrementally scans live Claude Code sessions."""

    def __init__(self, claude_home: Path | None = None, window_hours: int = config.WINDOW_HOURS) -> None:
        self.home = Path(claude_home) if claude_home else config.claude_home()
        self._mains: dict[str, _Main] = {}
        self._subs: dict[Path, _Sub] = {}
        self._shell: dict[str, ShellCall] = {}
        self._bg: dict[str, str] = {}  # background task id -> tool_use_id
        self._known_agents: set[str] = set()
        self._ended_agents: set[str] = set()
        self._session_agents: dict[str, set[str]] = {}
        self._live_procs: dict[str, tuple[int, int | None]] = {}
        self._tool_names: dict[str, str] = {}
        self._session_tids: dict[str, set[str]] = {}
        self._events: list[Event] = []
        self._evicted_sids: set[str] = set()
        self.reset_session_keys: set[str] = set()

    # ---- discovery ----
    def _live_sessions(self) -> dict[str, dict[str, Any]]:
        """Return live sessions by sessionId from sessions/*.json."""
        live: dict[str, dict[str, Any]] = {}
        for path in (self.home / "sessions").glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or not data.get("sessionId"):
                    continue
                pid, started_ms = self._proc_id(data)
                if pid_alive(pid, started_ms):
                    live[str(data["sessionId"])] = data
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return live

    @staticmethod
    def _proc_id(data: dict[str, Any]) -> tuple[int, int | None]:
        """Extract a process identity tuple (pid, startedAt) from session metadata."""
        started = data.get("startedAt")
        started_ms = started if isinstance(started, int) and not isinstance(started, bool) else None
        return (int(data["pid"]), started_ms)

    def _register_agent(self, sid: str, agent_id: str) -> None:
        """Track agent_id under session sid so dead-session eviction clears all state."""
        self._known_agents.add(agent_id)
        self._session_agents.setdefault(sid, set()).add(agent_id)

    def _evict_dead(self, live: dict[str, dict[str, Any]]) -> None:
        """Evict cached tails and agent state for sessions that stopped being live."""
        live_procs = {sid: self._proc_id(data) for sid, data in live.items()}
        cached_sids = (
            set(self._mains)
            | {sub.session_id for sub in self._subs.values()}
            | set(self._session_agents)
            | set(self._live_procs)
        )
        for sid in cached_sids:
            if sid in live_procs and self._live_procs.get(sid) == live_procs[sid]:
                continue
            had_tails = sid in self._mains or any(
                sub.session_id == sid for sub in self._subs.values()
            )
            if had_tails:
                self._evicted_sids.add(sid)
            self._mains.pop(sid, None)
            for path in [p for p, sub in self._subs.items() if sub.session_id == sid]:
                del self._subs[path]
            for aid in self._session_agents.pop(sid, ()):
                self._known_agents.discard(aid)
                self._ended_agents.discard(f"claude:{aid}")
            for tid in self._session_tids.pop(sid, ()):
                self._tool_names.pop(tid, None)
            removed_tids = {tid for tid, call in self._shell.items() if call.session_id == sid}
            for tid in removed_tids:
                self._shell.pop(tid, None)
            if removed_tids:
                for bg_id in [b for b, tid in self._bg.items() if tid in removed_tids]:
                    del self._bg[bg_id]
        self._live_procs = live_procs

    def _discover(self, live: dict[str, dict[str, Any]]) -> None:
        """Register tails for main and subagent files of live sessions."""
        if not live:
            return
        projects = self.home / "projects"
        for path in projects.glob("*/*.jsonl"):
            sid = path.stem
            if sid not in live or sid in self._mains:
                continue
            self._mains[sid] = _Main(JsonlTail(path), sid)
        for path in projects.glob("*/*/subagents/agent-*.jsonl"):
            sid = path.parent.parent.name
            if sid not in live or path in self._subs:
                continue
            agent_id = path.stem[len("agent-"):]
            meta = path.with_name(f"agent-{agent_id}.meta.json")
            self._subs[path] = _Sub(JsonlTail(path), sid, agent_id, meta)
            self._register_agent(sid, agent_id)

    # ---- scan ----
    def scan(self, now: datetime | None = None) -> ScanResult:
        """Scan transcripts of live sessions and return new events plus current state."""
        now = now or datetime.now(timezone.utc)
        live = self._live_sessions()
        self._events = []
        self.reset_session_keys = set()
        self._evict_dead(live)
        self._discover(live)
        rebuilt = {
            sid
            for sid in self._evicted_sids
            if sid in self._mains or any(sub.session_id == sid for sub in self._subs.values())
        }
        self._evicted_sids -= rebuilt
        self.reset_session_keys = rebuilt
        for sub in list(self._subs.values()):
            self._scan_sub(sub)
        for main in list(self._mains.values()):
            if main.tail.changed():
                for rec in main.tail.read_new():
                    self._handle_main(main, rec)
        for sub in list(self._subs.values()):
            self._finish_sub(sub, now)
        calls = list(self._shell.values())
        return ScanResult(
            self._events,
            calls,
            self._sessions(live),
            set(live),
            set(self.reset_session_keys),
        )

    def _sessions(self, live: dict[str, dict[str, Any]]) -> list[SessionInfo]:
        out = []
        for sid, main in self._mains.items():
            data = live[sid]
            status = "busy" if data.get("status") == "busy" else "idle"
            cwd = main.cwd or data.get("cwd", "")
            base = Path(cwd.replace("\\", "/")).name if cwd else ""
            name = main.title or data.get("name") or base or sid
            started = main.started
            if isinstance(data.get("startedAt"), int):
                started = datetime.fromtimestamp(data["startedAt"] / 1000, timezone.utc)
            out.append(SessionInfo(sid, name, cwd, status, started))
        return out

    # ---- subagents ----
    def _read_meta(self, sub: _Sub) -> None:
        try:
            st = sub.meta_path.stat()
        except OSError:
            return
        stat_key = (st.st_mtime_ns, st.st_size)
        if stat_key == sub.meta_stat:
            return
        sub.meta_stat = stat_key
        try:
            data = json.loads(sub.meta_path.read_text(encoding="utf-8"))
            sub.meta = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            pass

    def _scan_sub(self, sub: _Sub) -> None:
        key = f"claude:{sub.agent_id}"
        self._read_meta(sub)
        if sub.tail.changed():
            for rec in sub.tail.read_new():
                try:
                    self._handle_sub(sub, key, rec)
                except Exception:  # noqa: BLE001 - never raise on bad data
                    continue

    def _finish_sub(self, sub: _Sub, now: datetime) -> None:
        """Emit END once when meta.json says the subagent finished."""
        key = f"claude:{sub.agent_id}"
        if sub.meta.get("finished") and not sub.ended and sub.started:
            sub.ended = True
            if key not in self._ended_agents:
                self._ended_agents.add(key)
                self._register_agent(sub.session_id, sub.agent_id)
                raw_effort = sub.meta.get("effort")
                effort = raw_effort if isinstance(raw_effort, str) and raw_effort else None
                self._emit(
                    sub.last_ts or now, sub.session_id, key, EventKind.END,
                    status=Status.DONE, effort=effort,
                )

    def _emit(self, ts: datetime, sid: str, key: str, kind: EventKind, **kw: Any) -> None:
        self._events.append(Event(ts=ts, session_key=sid, agent_key=key, kind=kind, **kw))

    def _handle_sub(self, sub: _Sub, key: str, rec: dict[str, Any]) -> None:
        ts_raw = rec.get("timestamp")
        if not isinstance(ts_raw, str):
            return
        ts = utc(ts_raw)
        sub.last_ts = ts
        sid = sub.session_id
        if not sub.started:
            sub.started = True
            title = sub.meta.get("description")
            if not (isinstance(title, str) and title.strip()):
                title = short_title(_first_text(rec.get("message")))
            model = sub.meta.get("model")
            effort = sub.meta.get("effort")
            self._emit(
                ts, sid, key, EventKind.START,
                title=title if isinstance(title, str) and title else None,
                model=model if isinstance(model, str) else None,
                effort=effort if isinstance(effort, str) and effort else None,
            )
        cwd = rec.get("cwd") or ""
        msg = rec.get("message")
        if not isinstance(msg, dict):
            return
        if rec.get("type") == "assistant":
            self._assistant(sid, key, ts, msg, cwd, None)
        elif rec.get("type") == "user":
            self._user(sid, key, ts, msg, rec, None)

    def _assistant(
        self,
        sid: str,
        key: str,
        ts: datetime,
        msg: dict,
        cwd: str,
        effort: str | None,
    ) -> None:
        model = msg.get("model") if isinstance(msg.get("model"), str) else None
        usage = msg.get("usage")
        if isinstance(usage, dict):
            raw_in = int(usage.get("input_tokens") or 0)
            raw_cache_read = int(usage.get("cache_read_input_tokens") or 0)
            raw_cache_create = int(usage.get("cache_creation_input_tokens") or 0)
            tin = raw_in + raw_cache_read + raw_cache_create
            self._emit(
                ts, sid, key, EventKind.USAGE,
                tokens_in=tin,
                tokens_cached=raw_cache_read,
                tokens_out=int(usage.get("output_tokens") or 0),
                ctx_tokens=tin,
                model=model,
                effort=effort,
            )
        content = msg.get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                self._tool_use(sid, key, ts, block, cwd, model, effort)
            elif btype == "thinking":
                raw_thinking = block.get("thinking")
                body = raw_thinking if isinstance(raw_thinking, str) else ""
                self._emit(
                    ts, sid, key, EventKind.THINKING,
                    summary=_clip(body),
                    body=body,
                    model=model,
                    effort=effort,
                )
            elif btype == "text":
                raw_text = block.get("text")
                body = raw_text if isinstance(raw_text, str) else ""
                summary = _clip(body)
                if summary:
                    self._emit(
                        ts, sid, key, EventKind.TEXT,
                        summary=summary,
                        body=body,
                        model=model,
                        effort=effort,
                    )

    def _tool_use(
        self,
        sid: str,
        key: str,
        ts: datetime,
        block: dict,
        cwd: str,
        model: str | None,
        effort: str | None,
    ) -> None:
        name = str(block.get("name", ""))
        inp = block.get("input") if isinstance(block.get("input"), dict) else {}
        tid = str(block.get("id", ""))
        if tid:
            self._tool_names[tid] = name
            self._session_tids.setdefault(sid, set()).add(tid)
        self._emit(
            ts, sid, key, EventKind.TOOL_START,
            tool=name,
            summary=summarize_tool(name, inp, cwd),
            body=json.dumps(inp),
            model=model,
            effort=effort,
        )
        if name in SHELL_TOOLS and tid:
            self._shell[tid] = ShellCall(sid, key, tid, ts, None, cwd, str(inp.get("command", "")))

    def _emit_prompt(
        self,
        ts: datetime,
        sid: str,
        key: str,
        raw_text: Any,
        effort: str | None,
    ) -> None:
        if not isinstance(raw_text, str):
            return
        summary = _clip(raw_text)
        if summary:
            self._emit(ts, sid, key, EventKind.PROMPT, summary=summary, body=raw_text, effort=effort)

    def _user(
        self,
        sid: str,
        key: str,
        ts: datetime,
        msg: dict,
        rec: dict,
        effort: str | None,
    ) -> None:
        is_meta = bool(rec.get("isMeta"))
        content = msg.get("content")
        if isinstance(content, str):
            if not is_meta:
                self._emit_prompt(ts, sid, key, content, effort)
            return
        result = rec.get("toolUseResult")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_result":
                tid = str(block.get("tool_use_id", ""))
                self._shell_result(tid, ts, block, result)
                tool = self._tool_names.get(tid, "tool")
                summary = f"error: {tool}" if block.get("is_error") else ""
                body = truncate_body(_text_of(block.get("content")))
                self._emit(
                    ts, sid, key, EventKind.TOOL_END,
                    tool=tool,
                    summary=summary,
                    body=body,
                    effort=effort,
                )
            elif btype == "text" and not is_meta:
                self._emit_prompt(ts, sid, key, block.get("text"), effort)

    def _shell_result(self, tid: str, ts: datetime, block: dict, result: Any) -> None:
        call = self._shell.get(tid)
        if call is None:
            return
        bg = None
        if isinstance(result, dict) and result.get("backgroundTaskId"):
            bg = str(result["backgroundTaskId"])
        elif m := BG_RE.search(_text_of(block.get("content"))):
            bg = m.group(1)
        if bg:
            self._bg[bg] = tid
            call.ts_end = None
        else:
            call.ts_end = ts

    # ---- main transcript ----
    def _handle_main(self, main: _Main, rec: dict[str, Any]) -> None:
        try:
            self._handle_main_inner(main, rec)
        except Exception:  # noqa: BLE001
            return

    def _handle_main_inner(self, main: _Main, rec: dict[str, Any]) -> None:
        typ = rec.get("type")
        if typ == "ai-title":
            if isinstance(rec.get("aiTitle"), str):
                main.title = rec["aiTitle"]
            return
        ts_raw = rec.get("timestamp")
        if not isinstance(ts_raw, str):
            return
        ts = utc(ts_raw)
        if isinstance(rec.get("cwd"), str) and rec["cwd"]:
            main.cwd = rec["cwd"]
        raw_effort = rec.get("effort")
        if isinstance(raw_effort, str) and raw_effort:
            main.effort = raw_effort
        effort = main.effort or None
        sid = main.session_id
        key = main_agent_key(sid)
        if main.started is None:
            main.started = ts
            self._emit(ts, sid, key, EventKind.START, title="main", effort=effort)
        if typ == "queue-operation":
            self._notification(sid, ts, str(rec.get("content", "")))
            return
        msg = rec.get("message")
        if not isinstance(msg, dict):
            return
        if typ == "assistant":
            self._assistant(sid, key, ts, msg, main.cwd, effort)
        elif typ == "user":
            self._user(sid, key, ts, msg, rec, effort)
            self._agent_result(sid, ts, rec.get("toolUseResult"))

    def _agent_result(self, sid: str, ts: datetime, result: Any) -> None:
        if not isinstance(result, dict) or not result.get("agentId"):
            return
        agent_id = str(result["agentId"])
        self._register_agent(sid, agent_id)
        if result.get("isAsync") or "status" not in result:
            return
        model = result.get("resolvedModel")
        self._end_agent(sid, ts, agent_id, self._status_of(str(result.get("status", ""))),
                        model if isinstance(model, str) else None)

    @staticmethod
    def _status_of(text: str) -> Status:
        lowered = text.lower()
        return Status.FAILED if any(w in lowered for w in FAIL_WORDS) else Status.DONE

    def _end_agent(self, sid: str, ts: datetime, agent_id: str, status: Status, model: str | None) -> None:
        self._register_agent(sid, agent_id)
        key = f"claude:{agent_id}"
        if key in self._ended_agents:
            return
        self._ended_agents.add(key)
        self._emit(ts, sid, key, EventKind.END, status=status, model=model)

    def _notification(self, sid: str, ts: datetime, content: str) -> None:
        task = TASK_ID_RE.search(content)
        stat = STATUS_RE.search(content)
        if not task or not stat:
            return
        task_id = task.group(1).strip()
        if task_id in self._known_agents:
            self._end_agent(sid, ts, task_id, self._status_of(stat.group(1)), None)
        elif task_id in self._bg:
            call = self._shell.get(self._bg[task_id])
            if call is not None and call.ts_end is None:
                call.ts_end = ts
