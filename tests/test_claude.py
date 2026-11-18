"""Tests for the Claude transcript scanner (uses a fake CLAUDE_HOME in tmp_path)."""

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import psutil
import pytest

from agent_watch import config
from agent_watch.model import EventKind, Status, main_agent_key
from agent_watch.sources import claude as claude_mod
from agent_watch.sources.claude import ClaudeScanner, summarize_tool
from agent_watch.store import Store

FIX = Path(__file__).parent / "fixtures" / "claude"
SID = "9af43e82-5772-4625-a46a-7cffea98c243"
AGENTS = {
    "a5f4272a501937721": 1,
    "a2a3a95ad46e9c54d": 2,
    "afd80650d75789625": 3,
}
NOW = datetime(2026, 11, 17, 17, 0, tzinfo=timezone.utc)
SLUG = "C--Users-user"


def build_home(tmp_path: Path, live: bool = True) -> Path:
    home = tmp_path / "claude"
    proj = home / "projects" / SLUG
    sub = proj / SID / "subagents"
    sub.mkdir(parents=True)
    shutil.copy(FIX / "session_main.jsonl", proj / f"{SID}.jsonl")
    for aid, n in AGENTS.items():
        shutil.copy(FIX / f"subagent_{n}.jsonl", sub / f"agent-{aid}.jsonl")
        shutil.copy(FIX / f"subagent_{n}.meta.json", sub / f"agent-{aid}.meta.json")
    if live:
        data = json.loads((FIX / "sessions_pid.json").read_text(encoding="utf-8"))
        data["pid"] = os.getpid()
        data["startedAt"] = int(psutil.Process().create_time() * 1000)
        (home / "sessions").mkdir()
        (home / "sessions" / f"{os.getpid()}.json").write_text(json.dumps(data), encoding="utf-8")
    return home


def scan_all(tmp_path: Path, live: bool = True):
    home = build_home(tmp_path, live)
    scanner = ClaudeScanner(home, window_hours=24 * 365)
    return home, scanner, scanner.scan(NOW)


def by_agent(events):
    out: dict[str, list] = {}
    for ev in events:
        out.setdefault(ev.agent_key, []).append(ev)
    return out


def test_summarize_tool_cases() -> None:
    assert summarize_tool("Bash", {"description": "list files", "command": "ls"}) == "Bash list files"
    assert summarize_tool("PowerShell", {"command": "x" * 100}) == "PowerShell " + "x" * 40
    base = Path("/w/p")
    assert summarize_tool("Read", {"file_path": str(base / "a.py")}, str(base)) == "Read a.py"
    assert summarize_tool("Edit", {"file_path": "/elsewhere/b.py"}, "/w/p") == "Edit /elsewhere/b.py"
    assert summarize_tool("Grep", {"pattern": "foo"}) == "Grep foo"
    assert summarize_tool("Glob", {"pattern": "*.py"}) == "Glob *.py"
    assert summarize_tool("Agent", {"description": "do it"}) == "Agent do it"
    assert summarize_tool("WebFetch", {"url": "http://x"}) == "WebFetch http://x"
    assert summarize_tool("WebSearch", {"query": "q"}) == "WebSearch q"
    assert summarize_tool("Other", {"a": 1}) == "Other"
    assert len(summarize_tool("Grep", {"pattern": "p" * 300})) == 80


def test_agents_models_calls_and_status(tmp_path: Path) -> None:
    _, _, res = scan_all(tmp_path)
    agents = by_agent(res.events)
    for aid in AGENTS:
        assert f"claude:{aid}" in agents
    assert all(e.session_key == SID for evs in agents.values() for e in evs)

    def calls(aid: str) -> int:
        return sum(e.kind == EventKind.TOOL_START for e in agents[f"claude:{aid}"])

    assert calls("a5f4272a501937721") == 2
    assert calls("a2a3a95ad46e9c54d") == 9
    assert calls("afd80650d75789625") == 10

    start = agents["claude:a2a3a95ad46e9c54d"][0]
    assert start.kind == EventKind.START and start.model == "sonnet"
    assert start.title == "lorem ipsum task description 6"
    models = {e.model for e in agents["claude:a5f4272a501937721"] if e.model}
    assert "claude-haiku-5-5" in models
    last_tool = [e for e in agents["claude:a5f4272a501937721"] if e.kind == EventKind.TOOL_START][-1]
    assert last_tool.tool == "SubagentHandback"

    ends = {e.agent_key: e for e in res.events if e.kind == EventKind.END}
    assert ends["claude:a5f4272a501937721"].status == Status.DONE
    assert ends["claude:a5f4272a501937721"].model == "claude-haiku-5-5"
    assert ends["claude:a2a3a95ad46e9c54d"].status == Status.DONE  # via task-notification
    assert "claude:afd80650d75789625" not in ends  # still running
    assert "claude:aba1f90b163d55be6" in ends
    assert "claude:blw34zalr" not in ends
    assert len([e for e in res.events if e.kind == EventKind.END]) == len(ends)


def test_usage_tokens(tmp_path: Path) -> None:
    _, _, res = scan_all(tmp_path)
    usage = [e for e in res.events if e.kind == EventKind.USAGE]
    assert usage
    for e in usage:
        assert e.ctx_tokens == e.tokens_in


def test_shell_calls(tmp_path: Path) -> None:
    _, _, res = scan_all(tmp_path)
    calls = res.shell_calls
    assert calls
    main_calls = [c for c in calls if c.agent_key == main_agent_key(SID)]
    sub_calls = [c for c in calls if c.agent_key == "claude:a2a3a95ad46e9c54d"]
    assert len(sub_calls) == 9
    assert any("agy.exe" in c.command for c in main_calls)
    assert all(c.session_id == SID for c in calls)
    finished = [c for c in calls if c.ts_end is not None]
    assert all(c.ts_end >= c.ts_start for c in finished)
    bg = [c for c in main_calls if c.ts_start.isoformat().startswith("2026-11-17T16:04:13")]
    assert len(bg) == 1 and bg[0].ts_end is not None  # completed via task-notification


def test_sessions_live_and_ended(tmp_path: Path) -> None:
    _, _, res = scan_all(tmp_path)
    assert res.live_session_keys == {SID}
    (info,) = res.sessions
    assert info.key == SID and info.status == "busy"
    assert info.name == "lorem ipsum session title"
    assert info.cwd.startswith("C:\\Users\\user")
    _, _, res2 = scan_all(tmp_path / "x", live=False)
    assert res2.live_session_keys == set()
    assert res2.sessions == []
    assert res2.events == []


def test_incremental(tmp_path: Path) -> None:
    home, scanner, first = scan_all(tmp_path)
    assert first.events
    assert scanner.scan(NOW).events == []
    path = home / "projects" / SLUG / SID / "subagents" / "agent-afd80650d75789625.jsonl"
    line = {
        "type": "assistant", "timestamp": "2026-11-17T16:10:00.000Z", "cwd": "C:\\Users\\user",
        "message": {"model": "claude-sonnet-5-5", "usage": {"input_tokens": 1, "output_tokens": 2},
                    "content": [{"type": "tool_use", "id": "toolu_new", "name": "Grep",
                                 "input": {"pattern": "needle"}}]},
    }
    with path.open("ab") as fh:
        fh.write((json.dumps(line) + "\n").encode())
        fh.write(b"not json\n")
        fh.write(b'{"type": "assistant", "message": {}}\n')  # no timestamp
    res = scanner.scan(NOW)
    kinds = [(e.kind, e.summary) for e in res.events]
    assert (EventKind.TOOL_START, "Grep needle") in kinds
    assert any(e.kind == EventKind.USAGE and e.tokens_out == 2 for e in res.events)
    assert all(e.agent_key == "claude:afd80650d75789625" for e in res.events)


def test_meta_finished_emits_end_once(tmp_path: Path) -> None:
    home, scanner, _ = scan_all(tmp_path)
    meta = home / "projects" / SLUG / SID / "subagents" / "agent-afd80650d75789625.meta.json"
    data = json.loads(meta.read_text(encoding="utf-8"))
    data["finished"] = True
    meta.write_text(json.dumps(data), encoding="utf-8")
    st = meta.stat()
    os.utime(meta, (st.st_atime, st.st_mtime + 5))
    res = scanner.scan(NOW)
    ends = [e for e in res.events if e.kind == EventKind.END]
    assert [e.agent_key for e in ends] == ["claude:afd80650d75789625"]
    assert scanner.scan(NOW).events == []


def test_malformed_files_do_not_raise(tmp_path: Path) -> None:
    home = build_home(tmp_path)
    (home / "sessions" / "junk.json").write_text("{bad", encoding="utf-8")
    (home / "projects" / SLUG / "zzz.jsonl").write_bytes(b"\xff\xfe garbage\n[1,2]\n")
    res = ClaudeScanner(home, window_hours=24 * 365).scan(NOW)
    assert res.events


V2_SID = "11111111-2222-3333-4444-555555555555"
DEAD_SID = "dead0000-0000-0000-0000-000000000000"
V2_SUB_ID = "subv2001"
SUB_KEY = f"claude:{V2_SUB_ID}"


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _write_session_file(
    home: Path,
    sid: str,
    pid: int | None = None,
    started_at: int | None = None,
    status: str = "idle",
) -> Path:
    sessions_dir = home / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    actual_pid = os.getpid() if pid is None else pid
    actual_started = int(psutil.Process().create_time() * 1000) if started_at is None else started_at
    path = sessions_dir / f"{actual_pid}.json"
    path.write_text(
        json.dumps({
            "pid": actual_pid,
            "sessionId": sid,
            "startedAt": actual_started,
            "status": status,
        }),
        encoding="utf-8",
    )
    return path


def _build_v2_home(tmp_path: Path) -> Path:
    home = tmp_path / "v2_claude"
    proj = home / "projects" / SLUG
    _write_session_file(home, V2_SID, status="busy")

    _write_jsonl(proj / f"{V2_SID}.jsonl", [
        {
            "type": "user",
            "timestamp": "2026-11-17T16:00:00Z",
            "cwd": "C:\\Users\\user",
            "message": {"role": "user", "content": "<prompt>"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-11-17T16:00:01Z",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-5-5",
                "usage": {
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 5,
                    "cache_read_input_tokens": 20,
                    "output_tokens": 7,
                },
                "content": [
                    {"type": "thinking", "thinking": "<thinking>"},
                    {"type": "text", "text": "<text>"},
                    {
                        "type": "tool_use",
                        "id": "tu_main_1",
                        "name": "Bash",
                        "input": {"command": "<cmd>"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-11-17T16:00:02Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_main_1",
                        "content": "<result>",
                        "is_error": False,
                    }
                ],
            },
        },
    ])

    sub_dir = proj / V2_SID / "subagents"
    sub_dir.mkdir(parents=True, exist_ok=True)
    (sub_dir / f"agent-{V2_SUB_ID}.meta.json").write_text(
        json.dumps({
            "description": "<sub-desc>",
            "effort": "low",
            "model": "claude-opus-5-5",
            "finished": True,
        }),
        encoding="utf-8",
    )
    _write_jsonl(sub_dir / f"agent-{V2_SUB_ID}.jsonl", [
        {
            "type": "user",
            "timestamp": "2026-11-17T16:00:03Z",
            "message": {"role": "user", "content": "<sub-prompt>"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-11-17T16:00:04Z",
            "message": {
                "role": "assistant",
                "model": "claude-opus-5-5",
                "usage": {
                    "input_tokens": 3,
                    "cache_creation_input_tokens": 2,
                    "cache_read_input_tokens": 11,
                    "output_tokens": 4,
                },
                "content": [{"type": "text", "text": "<sub-text>"}],
            },
        },
    ])

    _write_jsonl(proj / f"{DEAD_SID}.jsonl", [
        {
            "type": "user",
            "timestamp": "2026-11-17T16:00:00Z",
            "message": {"role": "user", "content": "<prompt>"},
        },
    ])
    _write_jsonl(proj / DEAD_SID / "subagents" / "agent-deadsub.jsonl", [
        {
            "type": "user",
            "timestamp": "2026-11-17T16:00:01Z",
            "message": {"role": "user", "content": "<sub-prompt>"},
        },
    ])
    return home


@pytest.fixture
def scan(tmp_path: Path):
    home = _build_v2_home(tmp_path)
    return ClaudeScanner(home).scan(NOW)


def test_main_agent_events(scan) -> None:
    main_key = main_agent_key(V2_SID)
    main_events = [e for e in scan.events if e.agent_key == main_key]
    kinds = [e.kind for e in main_events if e.kind not in (EventKind.USAGE, EventKind.START)]
    assert kinds[:5] == [
        EventKind.PROMPT,
        EventKind.THINKING,
        EventKind.TEXT,
        EventKind.TOOL_START,
        EventKind.TOOL_END,
    ]

    start = next(e for e in main_events if e.kind is EventKind.START)
    assert start.title == "main"

    usage = next(e for e in main_events if e.kind is EventKind.USAGE)
    assert (usage.tokens_in, usage.tokens_cached, usage.tokens_out, usage.ctx_tokens) == (35, 20, 7, 35)
    assert usage.model == "claude-sonnet-5-5"

    store = Store()
    store.apply(scan.events)
    agent = store.find_agent(main_key)
    assert agent is not None
    assert agent.is_main is True
    assert agent.title == "main"
    assert agent.cached_tokens == 20


def test_tool_bodies(scan) -> None:
    start = next(e for e in scan.events if e.kind is EventKind.TOOL_START)
    assert json.loads(start.body) == {"command": "<cmd>"}
    end = next(e for e in scan.events if e.kind is EventKind.TOOL_END)
    assert end.summary == ""
    assert end.body == "<result>"


def test_subagent_effort_model_finished(scan) -> None:
    sub = [e for e in scan.events if e.agent_key == SUB_KEY]
    assert any(e.effort == "low" for e in sub)
    assert any(e.model == "claude-opus-5-5" for e in sub)
    assert any(e.kind is EventKind.PROMPT and e.body == "<sub-prompt>" for e in sub)
    assert sub[-1].kind is EventKind.END and sub[-1].status is Status.DONE


def test_non_live_session_not_scanned(scan) -> None:
    assert all(e.session_key != DEAD_SID for e in scan.events)
    assert all(e.agent_key != "claude:deadsub" for e in scan.events)
    assert all(s.key != DEAD_SID for s in scan.sessions)
    assert DEAD_SID not in scan.live_session_keys


def test_is_meta_multi_text_empty_thinking_and_tool_result_edges(tmp_path: Path) -> None:
    home = tmp_path / "edges"
    proj = home / "projects" / SLUG
    _write_session_file(home, V2_SID)

    long_text_1 = "A" * 120
    long_text_2 = "B" * 120
    oversized_result = "R" * (config.BODY_CAP + 50)

    _write_jsonl(proj / f"{V2_SID}.jsonl", [
        {
            "type": "user",
            "isMeta": True,
            "timestamp": "2026-11-17T16:00:00Z",
            "message": {"role": "user", "content": "<meta-ignored>"},
        },
        {
            "type": "user",
            "timestamp": "2026-11-17T16:00:01Z",
            "message": {"role": "user", "content": [{"type": "text", "text": "<block-prompt>"}]},
        },
        {
            "type": "assistant",
            "timestamp": "2026-11-17T16:00:02Z",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-5-5",
                "content": [
                    {"type": "thinking", "thinking": ""},
                    {"type": "text", "text": long_text_1},
                    {"type": "text", "text": long_text_2},
                    {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"file_path": "a.py"}},
                    {"type": "tool_use", "id": "tu_2", "name": "Bash", "input": {"command": "<cmd>"}},
                ],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-11-17T16:00:03Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": [{"type": "text", "text": "<r1>"}, {"type": "text", "text": "<r2>"}],
                        "is_error": False,
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_2",
                        "content": oversized_result,
                        "is_error": True,
                    },
                ],
            },
        },
    ])

    res = ClaudeScanner(home).scan(NOW)
    prompts = [e for e in res.events if e.kind is EventKind.PROMPT]
    assert [p.body for p in prompts] == ["<block-prompt>"]

    thinking = [e for e in res.events if e.kind is EventKind.THINKING]
    assert len(thinking) == 1 and thinking[0].body == ""

    texts = [e for e in res.events if e.kind is EventKind.TEXT]
    assert [t.body for t in texts] == [long_text_1, long_text_2]

    ends = [e for e in res.events if e.kind is EventKind.TOOL_END]
    assert len(ends) == 2
    assert ends[0].summary == "" and ends[0].body == "<r1>\n<r2>"
    assert ends[1].summary == "error: Bash"
    assert len(ends[1].body) == config.BODY_CAP and ends[1].body.endswith("…")


def test_main_agent_effort_present_and_absent(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "effort"
    proj = home / "projects" / SLUG
    sid_with = "11111111-0000-0000-0000-000000000001"
    sid_without = "11111111-0000-0000-0000-000000000002"
    monkeypatch.setattr(claude_mod, "pid_alive", lambda pid, started=None: pid in (101, 102))

    _write_session_file(home, sid_with, pid=101, started_at=1000)
    _write_session_file(home, sid_without, pid=102, started_at=2000)
    _write_jsonl(proj / f"{sid_with}.jsonl", [
        {
            "type": "assistant",
            "timestamp": "2026-11-17T16:00:00Z",
            "effort": "high",
            "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [{"type": "text", "text": "<text>"}]},
        }
    ])
    _write_jsonl(proj / f"{sid_without}.jsonl", [
        {
            "type": "assistant",
            "timestamp": "2026-11-17T16:00:00Z",
            "message": {"role": "assistant", "model": "claude-sonnet-5-5", "content": [{"type": "text", "text": "<text>"}]},
        }
    ])

    res = ClaudeScanner(home).scan(NOW)
    store = Store()
    store.apply(res.events)
    with_agent = store.find_agent(main_agent_key(sid_with))
    without_agent = store.find_agent(main_agent_key(sid_without))
    assert with_agent is not None and with_agent.effort == "high"
    assert without_agent is not None and without_agent.effort == ""


def test_eviction_on_session_stop_and_resume(tmp_path: Path, monkeypatch) -> None:
    home = _build_v2_home(tmp_path)
    for p in (home / "sessions").glob("*.json"):
        p.unlink()

    alive_pids: set[int] = {1001}
    monkeypatch.setattr(claude_mod, "pid_alive", lambda pid, started=None: pid in alive_pids)
    sess_file = _write_session_file(home, V2_SID, pid=1001, started_at=1000)

    scanner = ClaudeScanner(home)
    first = scanner.scan(NOW)
    assert first.reset_session_keys == set()
    assert any(e.agent_key == main_agent_key(V2_SID) for e in first.events)
    assert any(e.agent_key == SUB_KEY and e.kind is EventKind.END for e in first.events)
    assert scanner.scan(NOW).events == []

    # Session stops being live -> cached tails and agent state are evicted.
    alive_pids.clear()
    sess_file.unlink()
    dead_res = scanner.scan(NOW)
    assert dead_res.live_session_keys == set()
    assert dead_res.sessions == []
    assert dead_res.events == []
    assert dead_res.reset_session_keys == set()

    # Session resumes with the same session id and a new pid -> re-read from start.
    alive_pids.add(2002)
    sess_file2 = _write_session_file(home, V2_SID, pid=2002, started_at=2000)
    resumed = scanner.scan(NOW)
    assert resumed.live_session_keys == {V2_SID}
    assert resumed.reset_session_keys == {V2_SID}
    assert any(e.agent_key == main_agent_key(V2_SID) and e.kind is EventKind.PROMPT for e in resumed.events)
    assert any(e.agent_key == SUB_KEY and e.kind is EventKind.START for e in resumed.events)
    assert any(e.agent_key == SUB_KEY and e.kind is EventKind.END for e in resumed.events)

    # Rapid pid replacement between ticks also triggers eviction and re-read.
    sess_file2.unlink()
    alive_pids.clear()
    alive_pids.add(3003)
    _write_session_file(home, V2_SID, pid=3003, started_at=3000)
    rapid = scanner.scan(NOW)
    assert rapid.reset_session_keys == {V2_SID}
    assert any(e.agent_key == main_agent_key(V2_SID) and e.kind is EventKind.PROMPT for e in rapid.events)
    assert any(e.agent_key == SUB_KEY and e.kind is EventKind.END for e in rapid.events)


def test_tool_ids_populated_on_start_and_end(tmp_path: Path) -> None:
    home = tmp_path / "ids"
    proj = home / "projects" / SLUG
    _write_session_file(home, V2_SID)
    _write_jsonl(proj / f"{V2_SID}.jsonl", [
        {
            "type": "assistant",
            "timestamp": "2026-11-17T16:00:01Z",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-5-5",
                "content": [
                    {"type": "tool_use", "id": "tu_a", "name": "Bash", "input": {"command": "a"}},
                    {"type": "tool_use", "id": "tu_b", "name": "Bash", "input": {"command": "b"}},
                ],
            },
        },
        {
            "type": "user",
            "timestamp": "2026-11-17T16:00:02Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_b", "content": "rb"},
                    {"type": "tool_result", "tool_use_id": "tu_a", "content": "ra"},
                ],
            },
        },
    ])
    res = ClaudeScanner(home).scan(NOW)
    starts = [e for e in res.events if e.kind is EventKind.TOOL_START]
    ends = [e for e in res.events if e.kind is EventKind.TOOL_END]
    assert [e.tool_id for e in starts] == ["tu_a", "tu_b"]
    assert [(e.tool_id, e.body) for e in ends] == [("tu_b", "rb"), ("tu_a", "ra")]
