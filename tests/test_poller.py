"""Tests for Poller orchestration on fake Claude and agy homes."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from test_claude import AGENTS, FIX as CLAUDE_FIX, SID, build_home

from agent_watch import config
from agent_watch.model import (
    STANDALONE_SESSION_KEY,
    UNLINKED_SESSION_KEY,
    EventKind,
    Status,
    main_agent_key,
)
from agent_watch.poller import Poller
from agent_watch.sources import claude as claude_mod
from agent_watch.sources.agy import AgyScanner
from agent_watch.sources.claude import ClaudeScanner
from agent_watch.sources.codex import CodexScanner
from agent_watch.store import Store

AGY_FIX = Path(__file__).parent / "fixtures" / "agy"
NOW = datetime(2026, 11, 17, 17, 0, tzinfo=timezone.utc)
LINKED_CONV = "11111111-1111-1111-1111-111111111111"
LOOSE_CONV = "22222222-2222-2222-2222-222222222222"
PROMPT = (
    "Create 3 small text files a.txt b.txt c.txt in the workspace with one sentence each, "
    "then read them back and summarize. Do not run shell commands."
)
SUB_ID = next(iter(AGENTS))


def place_agy(agy_home: Path, conv_id: str, fixture: str, shift_to: str | None = None) -> Path:
    """Copy an agy transcript fixture; optionally rewrite its day/hour to escape every shell call window."""
    dest = agy_home / "brain" / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    dest.parent.mkdir(parents=True, exist_ok=True)
    text = (AGY_FIX / fixture).read_text(encoding="utf-8")
    if shift_to:
        text = text.replace("2026-11-17T16:", shift_to)
    dest.write_text(text, encoding="utf-8")
    return dest


def add_agy_shell_call(
    claude_home: Path,
    command: str = f'agy -p "{PROMPT}"',
    ts_start: str = "2026-11-17T16:05:19Z",
    ts_end: str = "2026-11-17T16:05:30Z",
    tool_use_id: str = "toolu_agy1",
) -> None:
    """Append a Bash tool_use running agy (and its result) to the main transcript."""
    main = claude_home / "projects" / "C--Users-user" / f"{SID}.jsonl"
    use = {
        "type": "assistant",
        "timestamp": ts_start,
        "cwd": "C:/Users/user",
        "message": {"role": "assistant", "content": [{
            "type": "tool_use", "id": tool_use_id, "name": "Bash",
            "input": {"command": command},
        }]},
    }
    result = {
        "type": "user",
        "timestamp": ts_end,
        "message": {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": tool_use_id, "content": "done",
        }]},
    }
    with main.open("a", encoding="utf-8") as fh:
        for rec in (use, result):
            fh.write(json.dumps(rec) + "\n")


def make_poller(tmp_path: Path) -> tuple[Poller, Store, Path, Path]:
    claude_home = build_home(tmp_path)
    add_agy_shell_call(claude_home)
    agy_home = tmp_path / "agy"
    place_agy(agy_home, LINKED_CONV, "transcript_tools.jsonl")
    place_agy(agy_home, LOOSE_CONV, "transcript_cancelled.jsonl", shift_to="2026-11-17T03:")
    store = Store()
    poller = Poller(
        store,
        ClaudeScanner(claude_home, window_hours=24 * 365),
        AgyScanner(agy_home, window_hours=24 * 365, proc_provider=lambda: []),
    )
    return poller, store, claude_home, agy_home


def test_links_agy_into_claude_session_and_standalone(tmp_path: Path) -> None:
    poller, store, _, _ = make_poller(tmp_path)
    result = poller.poll_once(NOW)

    assert poller.last_error is None
    linked = store.find_agent(f"agy:{LINKED_CONV}")
    loose = store.find_agent(f"agy:{LOOSE_CONV}")
    assert linked is not None and loose is not None
    assert linked.session_key == SID
    assert linked.key in store.sessions[SID].agents
    assert loose.session_key == STANDALONE_SESSION_KEY
    assert store.sessions[STANDALONE_SESSION_KEY].name == "Unlinked"
    assert {SID, STANDALONE_SESSION_KEY} <= result.changed
    for aid in AGENTS:
        assert f"claude:{aid}" in store.sessions[SID].agents
    agy_events = [e for e in result.events if e.agent_key == f"agy:{LINKED_CONV}"]
    assert agy_events and all(e.session_key == SID for e in agy_events)
    assert store.sessions[SID].name


def test_new_subagent_line_marks_session_changed(tmp_path: Path) -> None:
    poller, store, claude_home, _ = make_poller(tmp_path)
    poller.poll_once(NOW)
    quiet = poller.poll_once(NOW)
    assert quiet.events == []

    path = next(claude_home.glob(f"projects/*/{SID}/subagents/agent-{SUB_ID}.jsonl"))
    before = store.find_agent(f"claude:{SUB_ID}").calls
    line = {
        "type": "assistant",
        "timestamp": "2026-11-17T16:59:00Z",
        "message": {"role": "assistant", "model": "claude-haiku-5-5", "content": [{
            "type": "tool_use", "id": "toolu_new", "name": "Grep", "input": {"pattern": "needle"},
        }]},
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")

    result = poller.poll_once(NOW)
    assert SID in result.changed
    assert any(e.kind == EventKind.TOOL_START and e.summary == "Grep needle" for e in result.events)
    assert store.find_agent(f"claude:{SUB_ID}").calls == before + 1


class Boom:
    def scan(self, now=None):
        raise RuntimeError("boom")


def test_scanner_exceptions_are_contained(tmp_path: Path) -> None:
    poller, store, _, _ = make_poller(tmp_path)
    poller.claude_scanner = Boom()
    poller.agy_scanner = Boom()
    result = poller.poll_once(NOW)
    assert result.events == []
    assert poller.last_error is not None and "boom" in poller.last_error

    poller2, _, _, _ = make_poller(tmp_path / "second")
    poller2.agy_scanner = Boom()
    result2 = poller2.poll_once(NOW)
    assert poller2.last_error is not None and "agy" in poller2.last_error
    assert any(e.agent_key.startswith("claude:") for e in result2.events)


def _write_agy_transcript(agy_home: Path, conv_id: str, records: list[dict]) -> Path:
    dest = agy_home / "brain" / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return dest


@pytest.fixture
def poller_with_agy_call(tmp_path: Path) -> Poller:
    claude_home = build_home(tmp_path)
    add_agy_shell_call(
        claude_home,
        command='agy -p "<prompt>" --model gemini-4-hydrogen-high',
        ts_start="2026-11-17T16:01:00Z",
        ts_end="2026-11-17T16:01:10Z",
        tool_use_id="toolu_agy_effort",
    )
    agy_home = tmp_path / "agy"
    _write_agy_transcript(
        agy_home,
        "c1",
        [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-11-17T16:01:01Z",
                "content": "<USER_REQUEST>\n<prompt>\n</USER_REQUEST>",
            }
        ],
    )
    return Poller(
        Store(),
        ClaudeScanner(claude_home, window_hours=24 * 365),
        AgyScanner(agy_home, window_hours=24 * 365, proc_provider=lambda: []),
    )


def test_linked_agy_gets_effort_from_command(poller_with_agy_call: Poller) -> None:
    poller_with_agy_call.poll_once()
    agent = poller_with_agy_call.store.find_agent("agy:c1")
    assert agent is not None
    assert agent.session_key == SID
    assert agent.effort == "high"

    # Once transcript emits its own model, source effort overrides link fallback.
    transcript = (
        poller_with_agy_call.agy_scanner.agy_home
        / "brain"
        / "c1"
        / ".system_generated"
        / "logs"
        / "transcript.jsonl"
    )
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-11-17T16:01:02Z",
                    "model": "gemini-4-hydrogen-low",
                    "content": "<text>",
                }
            )
            + "\n"
        )
    poller_with_agy_call.poll_once()
    assert agent.effort == "low"


def test_link_effort_does_not_overwrite_source_effort(tmp_path: Path) -> None:
    claude_home = build_home(tmp_path)
    add_agy_shell_call(claude_home, command=f'agy -p "{PROMPT}" --model gemini-4-hydrogen-high')
    agy_home = tmp_path / "agy"
    place_agy(agy_home, LINKED_CONV, "transcript_tools.jsonl")
    store = Store()
    poller = Poller(
        store,
        ClaudeScanner(claude_home, window_hours=24 * 365),
        AgyScanner(agy_home, window_hours=24 * 365, proc_provider=lambda: []),
    )
    poller.poll_once(NOW)
    agent = store.find_agent(f"agy:{LINKED_CONV}")
    assert agent is not None
    assert agent.session_key == SID
    assert agent.effort == "medium"


def test_delayed_link_applies_effort_and_unlinked_keeps_empty(tmp_path: Path) -> None:
    claude_home = build_home(tmp_path)
    agy_home = tmp_path / "agy"
    _write_agy_transcript(
        agy_home,
        "c-delay",
        [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "created_at": "2026-11-17T16:01:01Z",
                "content": "<USER_REQUEST>\n<prompt>\n</USER_REQUEST>",
            }
        ],
    )
    _write_agy_transcript(
        agy_home,
        "c-loose",
        [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "created_at": "2026-11-17T03:01:01Z",
                "content": "<USER_REQUEST>\n<prompt>\n</USER_REQUEST>",
            }
        ],
    )
    store = Store()
    poller = Poller(
        store,
        ClaudeScanner(claude_home, window_hours=24 * 365),
        AgyScanner(agy_home, window_hours=24 * 365, proc_provider=lambda: []),
    )
    poller.poll_once(NOW)
    delayed = store.find_agent("agy:c-delay")
    loose = store.find_agent("agy:c-loose")
    assert delayed is not None and delayed.session_key == STANDALONE_SESSION_KEY and delayed.effort == ""
    assert loose is not None and loose.session_key == STANDALONE_SESSION_KEY and loose.effort == ""

    add_agy_shell_call(
        claude_home,
        command='agy -p "<prompt>" --model gemini-4-hydrogen-xhigh',
        ts_start="2026-11-17T16:01:00Z",
        ts_end="2026-11-17T16:01:10Z",
        tool_use_id="toolu_delay",
    )
    res2 = poller.poll_once(NOW)
    assert {STANDALONE_SESSION_KEY, SID} <= res2.changed
    assert delayed.session_key == SID
    assert delayed.effort == "xhigh"
    assert loose.effort == ""

    quiet = poller.poll_once(NOW)
    assert quiet.changed == set()
    assert quiet.events == []


@dataclass
class _FakeProc:
    cmdline: list[str]
    cwd: str | None = None


def _write_codex_rollout(
    codex_home: Path,
    rid: str,
    records: list[dict],
    mtime: float | None = None,
) -> Path:
    dest = (
        codex_home
        / "sessions"
        / "2026"
        / "11"
        / "17"
        / f"rollout-2026-11-17T16-59-00-{rid}.jsonl"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(dest, (mtime, mtime))
    return dest


def _append_bash_call(
    transcript_path: Path,
    tool_use_id: str,
    command: str,
    ts_start: str,
    ts_end: str | None = None,
) -> None:
    recs = [
        {
            "type": "assistant",
            "timestamp": ts_start,
            "cwd": "C:/work/proj",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_use_id,
                        "name": "Bash",
                        "input": {"command": command},
                    }
                ],
            },
        }
    ]
    if ts_end is not None:
        recs.append(
            {
                "type": "user",
                "timestamp": ts_end,
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": "<result>",
                        }
                    ],
                },
            }
        )
    with transcript_path.open("a", encoding="utf-8") as fh:
        for rec in recs:
            fh.write(json.dumps(rec) + "\n")


def test_codex_card_under_calling_subagent(tmp_path: Path) -> None:
    claude_home = build_home(tmp_path)
    sub_path = next(claude_home.glob(f"projects/*/{SID}/subagents/agent-{SUB_ID}.jsonl"))
    main_path = next(claude_home.glob(f"projects/*/{SID}.jsonl"))

    sub_prompt = "<prompt> codex subagent task alpha"
    main_prompt = "<prompt> codex main task beta"
    _append_bash_call(
        sub_path,
        "tu_codex_sub",
        f'codex exec -m gpt-5.6-terra -c model_reasoning_effort="high" "{sub_prompt}"',
        "2026-11-17T16:59:00Z",
        "2026-11-17T16:59:20Z",
    )
    _append_bash_call(
        main_path,
        "tu_codex_main",
        f'codex exec -m gpt-5.6-terra -c model_reasoning_effort="xhigh" "{main_prompt}"',
        "2026-11-17T16:59:10Z",
        "2026-11-17T16:59:30Z",
    )

    codex_home = tmp_path / "codex"
    rid_sub = "00000000-0000-0000-0000-00000000c101"
    rid_main = "00000000-0000-0000-0000-00000000c102"
    _write_codex_rollout(
        codex_home,
        rid_sub,
        [
            {"timestamp": "2026-11-17T16:59:01Z", "type": "session_meta", "payload": {"id": rid_sub, "timestamp": "2026-11-17T16:59:01Z", "cwd": "C:\\work\\proj"}},
            {"timestamp": "2026-11-17T16:59:01Z", "type": "turn_context", "payload": {"cwd": "C:\\work\\proj", "model": "gpt-5.6-terra"}},
            {"timestamp": "2026-11-17T16:59:01Z", "type": "event_msg", "payload": {"type": "user_message", "message": sub_prompt}},
            {"timestamp": "2026-11-17T16:59:05Z", "type": "event_msg", "payload": {"type": "token_count", "info": None, "rate_limits": {"primary": {"used_percent": 14.5, "window_minutes": 300, "resets_at": 1795000000}}}},
            {"timestamp": "2026-11-17T16:59:15Z", "type": "event_msg", "payload": {"type": "task_complete"}},
        ],
        mtime=NOW.timestamp() - 30.0,
    )
    _write_codex_rollout(
        codex_home,
        rid_main,
        [
            {"timestamp": "2026-11-17T16:59:11Z", "type": "session_meta", "payload": {"id": rid_main, "timestamp": "2026-11-17T16:59:11Z", "cwd": "C:\\work\\proj"}},
            {"timestamp": "2026-11-17T16:59:11Z", "type": "turn_context", "payload": {"cwd": "C:\\work\\proj", "model": "gpt-5.6-terra", "effort": "low"}},
            {"timestamp": "2026-11-17T16:59:11Z", "type": "event_msg", "payload": {"type": "user_message", "message": main_prompt}},
            {"timestamp": "2026-11-17T16:59:25Z", "type": "event_msg", "payload": {"type": "task_complete"}},
        ],
        mtime=NOW.timestamp() - 25.0,
    )

    store = Store()
    poller = Poller(
        store,
        ClaudeScanner(claude_home, window_hours=24 * 365),
        AgyScanner(tmp_path / "agy", window_hours=24 * 365, proc_provider=lambda: []),
        CodexScanner(codex_home, window_hours=24 * 365, proc_provider=lambda: []),
    )
    res = poller.poll_once(NOW)
    assert poller.last_error is None

    c_sub = store.find_agent(f"codex:{rid_sub}")
    c_main = store.find_agent(f"codex:{rid_main}")
    assert c_sub is not None and c_main is not None
    assert c_sub.session_key == SID
    assert c_sub.parent_agent_key == f"claude:{SUB_ID}"
    assert c_sub.effort == "high"
    assert c_main.session_key == SID
    assert c_main.parent_agent_key == main_agent_key(SID)
    assert c_main.effort == "low"
    assert res.codex_limits is not None and res.codex_limits.used_percent == 14.5


def test_unlinked_codex_shown_while_running_then_dropped(tmp_path: Path) -> None:
    claude_home = build_home(tmp_path)
    codex_home = tmp_path / "codex"
    rid = "00000000-0000-0000-0000-00000000c201"
    _write_codex_rollout(
        codex_home,
        rid,
        [
            {"timestamp": "2026-11-17T16:59:50Z", "type": "session_meta", "payload": {"id": rid, "timestamp": "2026-11-17T16:59:50Z", "cwd": "C:\\work\\proj"}},
            {"timestamp": "2026-11-17T16:59:50Z", "type": "turn_context", "payload": {"cwd": "C:\\work\\proj", "model": "gpt-5.6-terra"}},
            {"timestamp": "2026-11-17T16:59:51Z", "type": "event_msg", "payload": {"type": "user_message", "message": "<prompt>"}},
        ],
        mtime=NOW.timestamp() - 2.0,
    )

    procs: list[_FakeProc] = [_FakeProc(cmdline=["codex.exe", "exec", "<prompt>"])]
    store = Store()
    poller = Poller(
        store,
        ClaudeScanner(claude_home, window_hours=24 * 365),
        AgyScanner(tmp_path / "agy", window_hours=24 * 365, proc_provider=lambda: []),
        CodexScanner(codex_home, window_hours=24 * 365, proc_provider=lambda: procs),
    )

    poller.poll_once(NOW)
    agent = store.find_agent(f"codex:{rid}")
    assert agent is not None
    assert agent.session_key == UNLINKED_SESSION_KEY
    assert store.sessions[UNLINKED_SESSION_KEY].name == "Unlinked"
    assert agent.status is Status.RUNNING

    procs.clear()
    t_end = NOW + timedelta(seconds=20)
    poller.poll_once(t_end)
    assert store.find_agent(f"codex:{rid}") is not None
    assert agent.status is Status.FAILED

    poller.poll_once(t_end + timedelta(seconds=config.UNLINKED_GRACE_SECONDS - 1))
    assert store.find_agent(f"codex:{rid}") is not None

    res_drop = poller.poll_once(t_end + timedelta(seconds=config.UNLINKED_GRACE_SECONDS))
    assert UNLINKED_SESSION_KEY in res_drop.changed
    assert store.find_agent(f"codex:{rid}") is None
    assert f"codex:{rid}" not in store.sessions[UNLINKED_SESSION_KEY].agents


def test_resumed_session_reappears(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    alive_pids: set[int] = {1001}
    monkeypatch.setattr(claude_mod, "pid_alive", lambda pid, started=None: pid in alive_pids)

    claude_home = build_home(tmp_path, live=False)
    sessions_dir = claude_home / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    sess1 = sessions_dir / "1.json"
    sess1.write_text(
        json.dumps({"pid": 1001, "sessionId": SID, "startedAt": 1000, "status": "busy"}),
        encoding="utf-8",
    )

    poller = Poller(
        Store(),
        ClaudeScanner(claude_home, window_hours=24 * 365),
        AgyScanner(tmp_path / "agy", window_hours=24 * 365, proc_provider=lambda: []),
        CodexScanner(tmp_path / "codex", window_hours=24 * 365, proc_provider=lambda: []),
    )
    poller.poll_once(NOW)
    assert SID in poller.store.sessions and poller.store.find_agent(main_agent_key(SID)) is not None

    sess1.unlink()
    alive_pids.clear()
    poller.poll_once(NOW + timedelta(seconds=1))
    assert SID in poller.store.sessions

    poller.poll_once(NOW + timedelta(seconds=config.SESSION_GRACE_SECONDS + 2))
    assert SID not in poller.store.sessions
    assert poller.store.find_agent(main_agent_key(SID)) is None

    alive_pids.add(2002)
    (sessions_dir / "2.json").write_text(
        json.dumps({"pid": 2002, "sessionId": SID, "startedAt": 2000, "status": "busy"}),
        encoding="utf-8",
    )
    poller.poll_once(NOW + timedelta(seconds=config.SESSION_GRACE_SECONDS + 5))
    assert SID in poller.store.sessions and poller.store.find_agent(main_agent_key(SID)) is not None


def test_codex_scanner_exception_contained(tmp_path: Path) -> None:
    poller, _, _, _ = make_poller(tmp_path)
    poller.codex_scanner = Boom()
    res = poller.poll_once(NOW)
    assert poller.last_error is not None and "codex" in poller.last_error
    assert any(e.agent_key.startswith("claude:") for e in res.events)


def test_old_finished_unlinked_ignored_while_running_unlinked_is_kept(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    old_mtime = now.timestamp() - 3600.0
    fresh_mtime = now.timestamp() - 2.0

    agy_home = tmp_path / "agy"
    old_agy = place_agy(agy_home, "old-agy", "transcript_tools.jsonl")
    os.utime(old_agy, (old_mtime, old_mtime))
    run_agy = _write_agy_transcript(
        agy_home,
        "run-agy",
        [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "created_at": now.isoformat(),
                "content": "<USER_REQUEST>\n<prompt>\n</USER_REQUEST>",
            }
        ],
    )
    os.utime(run_agy, (fresh_mtime, fresh_mtime))

    codex_home = tmp_path / "codex"
    day_dir = codex_home / "sessions" / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}"
    day_dir.mkdir(parents=True, exist_ok=True)
    rid_old = "00000000-0000-0000-0000-00000000d001"
    rid_run = "00000000-0000-0000-0000-00000000d002"

    old_codex = day_dir / f"rollout-{now:%Y-%m-%dT%H-%M-%S}-{rid_old}.jsonl"
    old_codex.write_text(
        "\n".join([
            json.dumps({"timestamp": now.isoformat(), "type": "session_meta", "payload": {"id": rid_old, "cwd": "C:\\work"}}),
            json.dumps({"timestamp": now.isoformat(), "type": "event_msg", "payload": {"type": "user_message", "message": "old task"}}),
            json.dumps({"timestamp": now.isoformat(), "type": "event_msg", "payload": {"type": "task_complete"}}),
        ]) + "\n",
        encoding="utf-8",
    )
    os.utime(old_codex, (old_mtime, old_mtime))

    run_codex = day_dir / f"rollout-{now:%Y-%m-%dT%H-%M-%S}-{rid_run}.jsonl"
    run_codex.write_text(
        "\n".join([
            json.dumps({"timestamp": now.isoformat(), "type": "session_meta", "payload": {"id": rid_run, "cwd": "C:\\work"}}),
            json.dumps({"timestamp": now.isoformat(), "type": "event_msg", "payload": {"type": "user_message", "message": "live task"}}),
        ]) + "\n",
        encoding="utf-8",
    )
    os.utime(run_codex, (fresh_mtime, fresh_mtime))

    procs: list[_FakeProc] = [_FakeProc(cmdline=["codex.exe", rid_run])]
    store = Store()
    poller = Poller(
        store,
        ClaudeScanner(tmp_path / "claude", window_hours=24 * 365),
        AgyScanner(agy_home, window_hours=24, proc_provider=lambda: []),
        CodexScanner(codex_home, window_hours=24, proc_provider=lambda: procs),
    )

    res = poller.poll_once(now)
    assert store.find_agent("agy:old-agy") is None
    assert store.find_agent(f"codex:{rid_old}") is None
    assert all(e.agent_key not in {"agy:old-agy", f"codex:{rid_old}"} for e in res.events)

    live_agy = store.find_agent("agy:run-agy")
    live_codex = store.find_agent(f"codex:{rid_run}")
    assert live_agy is not None and live_agy.session_key == UNLINKED_SESSION_KEY
    assert live_agy.status is Status.RUNNING
    assert live_codex is not None and live_codex.session_key == UNLINKED_SESSION_KEY
    assert live_codex.status is Status.RUNNING

    # Agents already in the store still receive their END event when they finish.
    procs.clear()
    os.utime(run_agy, (old_mtime, old_mtime))
    poller.poll_once(now + timedelta(seconds=20))
    assert store.find_agent("agy:run-agy") is not None and live_agy.status is Status.FAILED
    assert store.find_agent(f"codex:{rid_run}") is not None and live_codex.status is Status.FAILED


def test_claude_pid_change_same_sid_resets_without_doubling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alive_pids: set[int] = {1001}
    monkeypatch.setattr(claude_mod, "pid_alive", lambda pid, started=None: pid in alive_pids)

    claude_home = build_home(tmp_path, live=False)
    sessions_dir = claude_home / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    sess1 = sessions_dir / "1001.json"
    sess1.write_text(
        json.dumps({"pid": 1001, "sessionId": SID, "startedAt": 1000, "status": "busy"}),
        encoding="utf-8",
    )

    add_agy_shell_call(claude_home)
    agy_home = tmp_path / "agy"
    place_agy(agy_home, LINKED_CONV, "transcript_tools.jsonl")

    main_path = next(claude_home.glob(f"projects/*/{SID}.jsonl"))
    codex_prompt = "<prompt> linked codex task"
    _append_bash_call(
        main_path,
        "tu_codex_linked",
        f'codex exec -m gpt-5.6-terra "{codex_prompt}"',
        "2026-11-17T16:59:10Z",
        "2026-11-17T16:59:30Z",
    )
    codex_home = tmp_path / "codex"
    rid_codex = "00000000-0000-0000-0000-00000000e101"
    _write_codex_rollout(
        codex_home,
        rid_codex,
        [
            {"timestamp": "2026-11-17T16:59:11Z", "type": "session_meta", "payload": {"id": rid_codex, "cwd": "C:\\work\\proj"}},
            {"timestamp": "2026-11-17T16:59:11Z", "type": "turn_context", "payload": {"cwd": "C:\\work\\proj", "model": "gpt-5.6-terra"}},
            {"timestamp": "2026-11-17T16:59:11Z", "type": "event_msg", "payload": {"type": "user_message", "message": codex_prompt}},
            {
                "timestamp": "2026-11-17T16:59:15Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 120, "cached_input_tokens": 30, "output_tokens": 45, "reasoning_output_tokens": 10}},
                },
            },
            {"timestamp": "2026-11-17T16:59:20Z", "type": "event_msg", "payload": {"type": "task_complete"}},
        ],
        mtime=NOW.timestamp() - 30.0,
    )

    store = Store()
    poller = Poller(
        store,
        ClaudeScanner(claude_home, window_hours=24 * 365),
        AgyScanner(agy_home, window_hours=24 * 365, proc_provider=lambda: []),
        CodexScanner(codex_home, window_hours=24 * 365, proc_provider=lambda: []),
    )
    poller.poll_once(NOW)

    main_key = main_agent_key(SID)
    sub_key = f"claude:{SUB_ID}"
    agy_key = f"agy:{LINKED_CONV}"
    codex_key = f"codex:{rid_codex}"
    tracked_keys = (main_key, sub_key, agy_key, codex_key)

    def snapshot() -> tuple[dict[str, int], dict[str, tuple[object, ...]]]:
        per_agent = {}
        for k in tracked_keys:
            a = store.find_agent(k)
            assert a is not None and a.session_key == SID
            per_agent[k] = (
                a.in_tokens,
                a.cached_tokens,
                a.out_tokens,
                a.reasoning_tokens,
                a.calls,
                list(a.tool_times),
                store.event_count(k),
                len(store.events(k)),
            )
        return store.totals(SID), per_agent

    totals_before, agents_before = snapshot()
    assert totals_before["calls"] > 0 and totals_before["out_tokens"] > 0

    # Live pid 1 -> pid 2 with same sid resets Claude agents only and avoids doubling.
    sess1.unlink()
    alive_pids.clear()
    alive_pids.add(2002)
    sess2 = sessions_dir / "2002.json"
    sess2.write_text(
        json.dumps({"pid": 2002, "sessionId": SID, "startedAt": 2000, "status": "busy"}),
        encoding="utf-8",
    )
    poller.poll_once(NOW + timedelta(seconds=1))
    assert snapshot() == (totals_before, agents_before)

    # Brief disappearance within grace followed by resume also does not double-apply.
    sess2.unlink()
    alive_pids.clear()
    poller.poll_once(NOW + timedelta(seconds=2))
    alive_pids.add(3003)
    (sessions_dir / "3003.json").write_text(
        json.dumps({"pid": 3003, "sessionId": SID, "startedAt": 3000, "status": "busy"}),
        encoding="utf-8",
    )
    poller.poll_once(NOW + timedelta(seconds=3))
    assert snapshot() == (totals_before, agents_before)
