"""Tests for the Antigravity (agy) conversation source."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import time

import pytest

from agent_watch import config
from agent_watch.model import EventKind, STANDALONE_SESSION_KEY, Status
from agent_watch.sources.agy import (
    AgyScanner,
    parse_transcript_line,
    strip_user_request,
    summarize_agy_tool,
)
from agent_watch.store import Store


@dataclass
class _FakeProc:
    cmdline: list[str]
    cwd: str | None = None


def _place_transcript(agy_home: Path, conv_id: str, src: Path, mtime: float | None = None) -> Path:
    dest = agy_home / "brain" / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    if mtime is not None:
        os.utime(dest, (mtime, mtime))
    return dest


def test_strip_user_request() -> None:
    """Extracts <USER_REQUEST> text or strips XML-like tag blocks."""
    wrapped = (
        "<USER_REQUEST>\n  Create three files.\n</USER_REQUEST>\n"
        "<ADDITIONAL_METADATA>\nlocal time\n</ADDITIONAL_METADATA>"
    )
    assert strip_user_request(wrapped) == "Create three files."

    tagged_only = "<ADDITIONAL_METADATA>\ninfo\n</ADDITIONAL_METADATA>\n  Direct prompt  "
    assert strip_user_request(tagged_only) == "Direct prompt"
    assert strip_user_request("  plain text  ") == "plain text"


def test_summarize_agy_tool() -> None:
    """Decodes JSON-in-string args, shortens long paths, and caps at 80 chars."""
    long_path_json = (
        '"C:\\\\Users\\\\user\\\\AppData\\\\Local\\\\Temp\\\\claude\\\\'
        'very_long_dir_name_here\\\\ws1\\\\a.txt"'
    )
    assert summarize_agy_tool("write_file", {"Path": long_path_json}) == "write_file a.txt"

    raw_long_path = (
        "C:\\Users\\user\\AppData\\Local\\Temp\\claude\\"
        "very_long_dir_name_here\\ws1\\b.txt"
    )
    assert summarize_agy_tool("read_file", {"Path": raw_long_path}) == "read_file b.txt"
    assert summarize_agy_tool("read_file", {"Path": '"src/app.py"'}) == "read_file src/app.py"
    assert (
        summarize_agy_tool("grep_search", {"Query": '"AgyScanner"', "CommandLine": '"ignored"'})
        == "grep_search AgyScanner"
    )
    long_cmd = "x" * 120
    cmd_summary = summarize_agy_tool("run_command", {"CommandLine": json.dumps(long_cmd)})
    assert len(cmd_summary) == 80
    assert cmd_summary.startswith("run_command ")
    assert summarize_agy_tool("finish", {"Other": "1"}) == "finish"


def test_parse_transcript_line_counts_and_malformed(fixtures_dir: Path) -> None:
    """Parses transcript_tools.jsonl events and ignores unknown/malformed dicts."""
    lines = (fixtures_dir / "agy" / "transcript_tools.jsonl").read_text(encoding="utf-8").splitlines()
    events = []
    for raw in lines:
        events.extend(parse_transcript_line(json.loads(raw), "conv-1", "sess-1"))

    by_kind = {k: [e for e in events if e.kind == k] for k in EventKind}
    assert len(by_kind[EventKind.START]) == 1
    assert by_kind[EventKind.START][0].title == "Create 3 small text files a.txt b.txt c.txt in the…"
    assert by_kind[EventKind.START][0].agent_key == "agy:conv-1"
    assert by_kind[EventKind.START][0].session_key == "sess-1"

    assert len(by_kind[EventKind.PROMPT]) == 1
    assert by_kind[EventKind.PROMPT][0].body == (
        "Create 3 small text files a.txt b.txt c.txt in the workspace with one sentence each, "
        "then read them back and summarize. Do not run shell commands."
    )

    assert len(by_kind[EventKind.THINKING]) == 1
    assert by_kind[EventKind.THINKING][0].body == "I'm thinking through how to approach this.\n\n\n"
    assert by_kind[EventKind.THINKING][0].effort == "medium"

    assert len(by_kind[EventKind.USAGE]) == 3
    assert [(u.tokens_in, u.tokens_out, u.ctx_tokens, u.model, u.effort) for u in by_kind[EventKind.USAGE]] == [
        (11334, 577, 11334, "gemini-4-hydrogen-medium", "medium"),
        (12151, 293, 12151, "gemini-4-hydrogen-medium", "medium"),
        (12739, 347, 12739, "gemini-4-hydrogen-medium", "medium"),
    ]

    assert len(by_kind[EventKind.TOOL_START]) == 6
    assert [t.summary for t in by_kind[EventKind.TOOL_START]] == [
        "write_file a.txt",
        "write_file b.txt",
        "write_file c.txt",
        "read_file a.txt",
        "read_file b.txt",
        "read_file c.txt",
    ]
    assert all(json.loads(t.body) for t in by_kind[EventKind.TOOL_START])
    assert all(t.effort == "medium" for t in by_kind[EventKind.TOOL_START])
    assert len(by_kind[EventKind.TOOL_END]) == 6
    assert all("Created At:" in t.body for t in by_kind[EventKind.TOOL_END])
    assert len(by_kind[EventKind.TEXT]) == 1
    assert len(by_kind[EventKind.TEXT][0].summary) <= 80
    assert by_kind[EventKind.TEXT][0].body.startswith("Created and read back three files in workspace.")
    assert by_kind[EventKind.TEXT][0].effort == "medium"

    # Follow-up USER_INPUT (step_index != 0) emits PROMPT without START; empty/malformed produce nothing.
    followup = parse_transcript_line({"type": "USER_INPUT", "step_index": 2, "content": "next"}, "c", "s")
    assert [e.kind for e in followup] == [EventKind.PROMPT]
    assert followup[0].body == "next"
    assert parse_transcript_line({"type": "USER_INPUT", "step_index": 2, "content": ""}, "c", "s") == []
    assert parse_transcript_line({"type": "UNKNOWN_STEP"}, "c", "s") == []
    malformed_evs = parse_transcript_line({"type": "PLANNER_RESPONSE", "tool_calls": ["bad"]}, "c", "s")
    assert len(malformed_evs) == 1
    assert malformed_evs[0].kind == EventKind.USAGE
    assert parse_transcript_line({}, "c", "s") == []


def test_scanner_tools_done_with_old_mtime(tmp_path: Path, fixtures_dir: Path) -> None:
    """Completed transcript with old mtime and no live process ends as DONE once."""
    now_ts = time.time()
    old_mtime = now_ts - 60.0
    _place_transcript(tmp_path, "conv-done", fixtures_dir / "agy" / "transcript_tools.jsonl", mtime=old_mtime)

    scanner = AgyScanner(agy_home=tmp_path, proc_provider=lambda: [])
    res = scanner.scan(now=datetime.fromtimestamp(now_ts, tz=timezone.utc), mtime_now=now_ts)

    assert res.running == set()
    assert res.ended == {"conv-done": Status.DONE}
    assert len(res.convs) == 1
    assert res.convs[0].title == "Create 3 small text files a.txt b.txt c.txt in the…"
    assert res.convs[0].last_type == "PLANNER_RESPONSE"
    assert res.convs[0].last_has_tool_calls is False

    kinds = [e.kind for e in res.events]
    assert kinds.count(EventKind.START) == 1
    assert kinds.count(EventKind.PROMPT) == 1
    assert kinds.count(EventKind.THINKING) == 1
    assert kinds.count(EventKind.USAGE) == 3
    assert kinds.count(EventKind.TOOL_START) == 6
    assert kinds.count(EventKind.TOOL_END) == 6
    assert kinds.count(EventKind.TEXT) == 1
    assert kinds.count(EventKind.END) == 1
    end_ev = [e for e in res.events if e.kind == EventKind.END][0]
    assert end_ev.status == Status.DONE
    assert end_ev.session_key == STANDALONE_SESSION_KEY

    # Second scan emits no duplicate events or ended transition.
    res2 = scanner.scan(now=datetime.fromtimestamp(now_ts + 5, tz=timezone.utc), mtime_now=now_ts + 5)
    assert res2.events == []
    assert res2.ended == {}
    assert res2.running == set()


def test_scanner_cancelled_failed_with_old_mtime(tmp_path: Path, fixtures_dir: Path) -> None:
    """Cancelled transcript with old mtime and no live process transitions to FAILED."""
    now_ts = time.time()
    old_mtime = now_ts - 60.0
    _place_transcript(
        tmp_path, "conv-cancel", fixtures_dir / "agy" / "transcript_cancelled.jsonl", mtime=old_mtime
    )

    # Also verify PLANNER_RESPONSE ending with pending tool_calls fails when stale.
    pending_tools_dir = tmp_path / "brain" / "conv-midtool" / ".system_generated" / "logs"
    pending_tools_dir.mkdir(parents=True)
    pending_path = pending_tools_dir / "transcript.jsonl"
    raw_lines = (fixtures_dir / "agy" / "transcript_cancelled.jsonl").read_text(encoding="utf-8").splitlines()
    pending_path.write_text("\n".join(raw_lines[:2]) + "\n", encoding="utf-8")
    os.utime(pending_path, (old_mtime, old_mtime))

    scanner = AgyScanner(agy_home=tmp_path, proc_provider=lambda: [])
    res = scanner.scan(now=datetime.fromtimestamp(now_ts, tz=timezone.utc), mtime_now=now_ts)

    assert res.running == set()
    assert res.ended == {"conv-cancel": Status.FAILED, "conv-midtool": Status.FAILED}
    end_events = [e for e in res.events if e.kind == EventKind.END]
    assert len(end_events) == 2
    assert all(e.status == Status.FAILED and e.summary == "cancelled" for e in end_events)


def test_scanner_running_by_process_and_mtime_fallback(tmp_path: Path, fixtures_dir: Path) -> None:
    """Matches live processes by --conversation or prompt text, else falls back to <15s mtime."""
    now_ts = time.time()
    old_mtime = now_ts - 120.0
    src = fixtures_dir / "agy" / "transcript_cancelled.jsonl"

    _place_transcript(tmp_path, "conv-flag", src, mtime=old_mtime)
    _place_transcript(tmp_path, "conv-prompt", src, mtime=old_mtime)
    _place_transcript(tmp_path, "conv-recent", src, mtime=now_ts - 5.0)

    prompt_text = (
        "Create 3 small text files a.txt b.txt c.txt in the workspace with one sentence each, "
        "then read them back and summarize. Do not run shell commands."
    )
    procs = [
        _FakeProc(cmdline=["agy.exe", "--conversation", "conv-flag"]),
        _FakeProc(cmdline=["agy.exe", "-p", prompt_text, "--model", "gemini-4-hydrogen-medium"]),
    ]

    scanner = AgyScanner(agy_home=tmp_path, proc_provider=lambda: procs)
    res1 = scanner.scan(now=datetime.fromtimestamp(now_ts, tz=timezone.utc), mtime_now=now_ts)
    assert res1.running == {"conv-flag", "conv-prompt", "conv-recent"}
    assert res1.ended == {}

    # Clear processes and advance mtime_now past 15s threshold: all 3 end cleanly.
    procs.clear()
    later = now_ts + 20.0
    res2 = scanner.scan(now=datetime.fromtimestamp(later, tz=timezone.utc), mtime_now=later)
    assert res2.running == set()
    assert res2.ended == {
        "conv-flag": Status.FAILED,
        "conv-prompt": Status.FAILED,
        "conv-recent": Status.FAILED,
    }


def test_scanner_incremental_append(tmp_path: Path, fixtures_dir: Path) -> None:
    """Incremental transcript appends yield only newly written events across scans."""
    all_lines = (fixtures_dir / "agy" / "transcript_tools.jsonl").read_text(encoding="utf-8").splitlines()
    log_path = tmp_path / "brain" / "conv-inc" / ".system_generated" / "logs" / "transcript.jsonl"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("\n".join(all_lines[:2]) + "\n", encoding="utf-8")

    now_ts = time.time()
    scanner = AgyScanner(agy_home=tmp_path, proc_provider=lambda: [])

    res1 = scanner.scan(now=datetime.fromtimestamp(now_ts, tz=timezone.utc), mtime_now=now_ts)
    assert res1.running == {"conv-inc"}
    assert [e.kind for e in res1.events] == [
        EventKind.START,
        EventKind.PROMPT,
        EventKind.USAGE,
        EventKind.THINKING,
        EventKind.TOOL_START,
        EventKind.TOOL_START,
        EventKind.TOOL_START,
    ]

    with log_path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(all_lines[2:]) + "\n")

    res2 = scanner.scan(now=datetime.fromtimestamp(now_ts, tz=timezone.utc), mtime_now=now_ts)
    assert [e.kind for e in res2.events].count(EventKind.START) == 0
    assert [e.kind for e in res2.events].count(EventKind.TOOL_END) == 6
    assert [e.kind for e in res2.events].count(EventKind.USAGE) == 2
    assert [e.kind for e in res2.events].count(EventKind.TOOL_START) == 3
    assert [e.kind for e in res2.events].count(EventKind.TEXT) == 1

    # Advance past 15s without proc -> single END event emitted.
    later = log_path.stat().st_mtime + 20.0
    res3 = scanner.scan(now=datetime.fromtimestamp(later, tz=timezone.utc), mtime_now=later)
    assert res3.ended == {"conv-inc": Status.DONE}
    assert [e.kind for e in res3.events] == [EventKind.END]


def test_scanner_ignores_conversation_metadata_json(
    tmp_path: Path, fixtures_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AgyScanner must never read conversation_metadata.json and uses transcript title."""
    now_ts = time.time()
    conv_id = "conv-meta-1"
    _place_transcript(
        tmp_path, conv_id, fixtures_dir / "agy" / "transcript_tools.jsonl", mtime=now_ts - 60.0
    )

    meta_path = tmp_path / "cache" / "conversation_metadata.json"
    meta_path.parent.mkdir(parents=True)
    meta_path.write_text(
        json.dumps({"conversations": {conv_id: {"summary": {"Title": "Should Not Be Read"}}}}),
        encoding="utf-8",
    )

    read_calls = 0
    orig_read_text = Path.read_text

    def spy_read_text(self: Path, *args: object, **kwargs: object) -> str:
        nonlocal read_calls
        if self == meta_path:
            read_calls += 1
        return orig_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", spy_read_text)  # type: ignore[attr-defined]

    scanner = AgyScanner(agy_home=tmp_path, proc_provider=lambda: [])
    res = scanner.scan(now=datetime.fromtimestamp(now_ts, tz=timezone.utc), mtime_now=now_ts)
    assert read_calls == 0
    assert res.convs[0].title == "Create 3 small text files a.txt b.txt c.txt in the…"


PLANNER_WITH_THINKING = {
    "step_index": 1,
    "type": "PLANNER_RESPONSE",
    "created_at": "2026-11-17T16:05:21Z",
    "model": "gemini-4-hydrogen-high",
    "thinking": "<thinking>",
    "content": "<text>",
    "tool_calls": [{"name": "read_file", "args": {"Path": "a.py"}}],
}

USER_REQUEST = {
    "step_index": 0,
    "type": "USER_INPUT",
    "created_at": "2026-11-17T16:05:20Z",
    "content": "<USER_REQUEST>\n<prompt>\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nmeta\n</ADDITIONAL_METADATA>",
}


def test_planner_thinking_and_prompt() -> None:
    evs = parse_transcript_line(PLANNER_WITH_THINKING, "c1", "s1") + parse_transcript_line(
        USER_REQUEST, "c1", "s1"
    )
    assert {e.kind for e in evs} >= {EventKind.THINKING, EventKind.TEXT, EventKind.PROMPT}
    thinking = next(e for e in evs if e.kind is EventKind.THINKING)
    assert thinking.body == "<thinking>"
    assert thinking.effort == "high"
    assert thinking.model == "gemini-4-hydrogen-high"

    text_ev = next(e for e in evs if e.kind is EventKind.TEXT)
    assert text_ev.body == "<text>"
    assert text_ev.effort == "high"

    tool_ev = next(e for e in evs if e.kind is EventKind.TOOL_START)
    assert json.loads(tool_ev.body) == {"Path": "a.py"}
    assert tool_ev.effort == "high"

    prompt_ev = next(e for e in evs if e.kind is EventKind.PROMPT)
    assert prompt_ev.body == "<prompt>"


def test_transcript_bodies_truncation_and_effort_edges() -> None:
    oversized = "R" * (config.BODY_CAP + 25)
    end_evs = parse_transcript_line(
        {"type": "GENERIC", "status": "DONE", "content": oversized}, "c1", "s1"
    )
    assert len(end_evs) == 1
    assert end_evs[0].kind is EventKind.TOOL_END
    assert end_evs[0].summary == ""
    assert len(end_evs[0].body) == config.BODY_CAP and end_evs[0].body.endswith("…")

    err_evs = parse_transcript_line(
        {"type": "GENERIC", "status": "ERROR", "content": "<result>"}, "c1", "s1"
    )
    assert err_evs[0].summary == "error"
    assert err_evs[0].body == "<result>"

    no_effort = parse_transcript_line(
        {
            "type": "PLANNER_RESPONSE",
            "model": "claude-opus-4-6",
            "thinking": "   \n",
            "content": "<text>",
        },
        "c1",
        "s1",
    )
    assert [e.kind for e in no_effort] == [EventKind.USAGE, EventKind.TEXT]
    assert all(e.effort is None for e in no_effort)


def test_scanner_failed_reopens_to_running(tmp_path: Path, fixtures_dir: Path) -> None:
    """FAILED agy conversation reopens to RUNNING when new writes arrive."""
    all_lines = (fixtures_dir / "agy" / "transcript_tools.jsonl").read_text(encoding="utf-8").splitlines()
    conv_id = "conv-reopen"
    log_path = tmp_path / "brain" / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("\n".join(all_lines[:2]) + "\n", encoding="utf-8")

    now_ts = time.time()
    old_mtime = now_ts - 60.0
    os.utime(log_path, (old_mtime, old_mtime))

    scanner = AgyScanner(agy_home=tmp_path, proc_provider=lambda: [])
    store = Store()

    res1 = scanner.scan(now=datetime.fromtimestamp(now_ts, tz=timezone.utc), mtime_now=now_ts)
    store.apply(res1.events)
    assert res1.running == set()
    assert res1.ended == {conv_id: Status.FAILED}
    agent = store.find_agent(f"agy:{conv_id}")
    assert agent is not None
    assert agent.status is Status.FAILED
    assert agent.ended is not None

    with log_path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(all_lines[2:6]) + "\n")
    reopen_ts = now_ts + 5.0
    os.utime(log_path, (reopen_ts, reopen_ts))

    res2 = scanner.scan(now=datetime.fromtimestamp(reopen_ts, tz=timezone.utc), mtime_now=reopen_ts)
    store.apply(res2.events)
    assert res2.running == {conv_id}
    assert res2.ended == {}
    assert any(e.status is Status.RUNNING for e in res2.events)
    assert agent.status is Status.RUNNING
    assert agent.ended is None

    with log_path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(all_lines[6:]) + "\n")
    done_ts = reopen_ts + 20.0
    os.utime(log_path, (reopen_ts + 1.0, reopen_ts + 1.0))

    res3 = scanner.scan(now=datetime.fromtimestamp(done_ts, tz=timezone.utc), mtime_now=done_ts)
    store.apply(res3.events)
    assert res3.ended == {conv_id: Status.DONE}
    assert agent.status is Status.DONE
    assert agent.ended is not None
