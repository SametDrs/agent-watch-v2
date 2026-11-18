"""Tests for the OpenAI Codex CLI rollout scanner, config paths, and process matching."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import psutil
import pytest

from agent_watch import config, procs
from agent_watch.model import EventKind, Source, Status, UNLINKED_SESSION_KEY
from agent_watch.sources.codex import (
    CodexConv,
    CodexLimits,
    CodexScanResult,
    CodexScanner,
    parse_rollout_line,
    summarize_codex_tool,
)
from agent_watch.store import Store

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "codex" / "rollout-sample.jsonl"
FIXTURE_LINES = FIXTURE_PATH.read_text(encoding="utf-8").splitlines()
FIXTURE_RECORDS = [json.loads(line) for line in FIXTURE_LINES]


@dataclass
class _FakeProc:
    cmdline: list[str]
    cwd: str | None = None


def _fixture_summary() -> dict[str, object]:
    """Derive expected IDs, model, effort, token totals, and limits from the fixture."""
    rollout_id = ""
    cwd = ""
    model = ""
    effort = ""
    first_prompt = ""
    last_total: dict[str, int] = {}
    last_usage: dict[str, int] = {}
    last_limits: dict[str, object] = {}

    for rec in FIXTURE_RECORDS:
        rtype = rec.get("type")
        payload = rec.get("payload", {})
        if rtype == "session_meta":
            rollout_id = payload.get("id", rollout_id)
            cwd = payload.get("cwd", cwd)
        elif rtype == "turn_context":
            cwd = payload.get("cwd", cwd)
            model = payload.get("model", model)
            effort = payload.get("effort", effort)
        elif rtype == "event_msg":
            ptype = payload.get("type")
            if ptype == "user_message" and not first_prompt:
                first_prompt = payload.get("message", "")
            elif ptype == "token_count":
                info = payload.get("info")
                if isinstance(info, dict):
                    if isinstance(info.get("total_token_usage"), dict):
                        last_total = info["total_token_usage"]
                    if isinstance(info.get("last_token_usage"), dict):
                        last_usage = info["last_token_usage"]
                if isinstance(payload.get("rate_limits"), dict):
                    last_limits = payload["rate_limits"]

    return {
        "rollout_id": rollout_id,
        "cwd": cwd,
        "model": model,
        "effort": effort,
        "first_prompt": first_prompt,
        "last_total": last_total,
        "last_usage": last_usage,
        "last_limits": last_limits,
    }


def _write_rollout(
    codex_home: Path,
    lines: list[str],
    *,
    day: datetime | None = None,
    filename: str | None = None,
    trailing_newline: bool = True,
    mtime: float | None = None,
) -> Path:
    ref_day = day or datetime.now(timezone.utc)
    if filename is None:
        rid = _fixture_summary()["rollout_id"]
        filename = f"rollout-{ref_day.strftime('%Y-%m-%dT%H-%M-%S')}-{rid}.jsonl"
    dest = (
        codex_home
        / "sessions"
        / f"{ref_day.year:04d}"
        / f"{ref_day.month:02d}"
        / f"{ref_day.day:02d}"
        / filename
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(lines) + ("\n" if trailing_newline and lines else "")
    dest.write_bytes(content.encode("utf-8"))
    if mtime is not None:
        os.utime(dest, (mtime, mtime))
    return dest


def _append(path: Path, text: str) -> None:
    with path.open("ab") as fh:
        fh.write(text.encode("utf-8"))


@pytest.fixture
def tmp_codex_home(tmp_path: Path) -> Path:
    home = tmp_path / "codex_home"
    home.mkdir()
    return home


@pytest.fixture
def codex_scan(tmp_codex_home: Path) -> CodexScanResult:
    _write_rollout(tmp_codex_home, FIXTURE_LINES)
    return CodexScanner(tmp_codex_home, proc_provider=lambda: []).scan()


def test_fixture_parses(codex_scan: CodexScanResult) -> None:
    expected = _fixture_summary()
    kinds = {e.kind for e in codex_scan.events}
    assert {
        EventKind.START,
        EventKind.PROMPT,
        EventKind.THINKING,
        EventKind.TEXT,
        EventKind.TOOL_START,
        EventKind.TOOL_END,
        EventKind.USAGE,
        EventKind.END,
    } <= kinds

    assert len(codex_scan.convs) == 1
    conv = codex_scan.convs[0]
    assert isinstance(conv, CodexConv)
    assert conv.rollout_id == expected["rollout_id"]
    assert conv.model == expected["model"] == "gpt-5.6-terra"
    assert conv.effort == expected["effort"] == "medium"
    assert conv.cwd == expected["cwd"]
    assert conv.first_prompt == expected["first_prompt"]

    expected_key = f"codex:{expected['rollout_id']}"
    assert all(e.agent_key == expected_key for e in codex_scan.events)
    assert all(e.session_key == UNLINKED_SESSION_KEY for e in codex_scan.events)
    assert codex_scan.running == set()

    prompts = [e for e in codex_scan.events if e.kind is EventKind.PROMPT]
    assert len(prompts) == 1
    assert prompts[0].body == expected["first_prompt"]

    texts = [e for e in codex_scan.events if e.kind is EventKind.TEXT]
    assert len(texts) == 1
    assert texts[0].body == "<text>"

    thinking = [e for e in codex_scan.events if e.kind is EventKind.THINKING]
    expected_reasoning = [
        r for r in FIXTURE_RECORDS
        if r.get("type") == "response_item" and r.get("payload", {}).get("type") == "reasoning"
    ]
    assert len(thinking) == len(expected_reasoning)
    assert thinking[0].body == "<thinking>"
    assert thinking[1].body == ""

    tool_starts = [e for e in codex_scan.events if e.kind is EventKind.TOOL_START]
    tool_ends = [e for e in codex_scan.events if e.kind is EventKind.TOOL_END]
    expected_calls = [
        r["payload"] for r in FIXTURE_RECORDS
        if r.get("type") == "response_item"
        and r.get("payload", {}).get("type") in ("function_call", "custom_tool_call")
    ]
    assert [e.tool for e in tool_starts] == [c["name"] for c in expected_calls]
    assert [e.tool for e in tool_ends] == [c["name"] for c in expected_calls]
    assert json.loads(tool_starts[0].body) == json.loads(expected_calls[0]["arguments"])
    assert tool_starts[1].body == expected_calls[1]["input"]

    ends = [e for e in codex_scan.events if e.kind is EventKind.END]
    assert len(ends) == 1
    assert ends[0].status is Status.DONE


def test_usage_deltas_sum_to_last_total(codex_scan: CodexScanResult) -> None:
    expected = _fixture_summary()
    last_total = expected["last_total"]
    last_usage = expected["last_usage"]
    assert isinstance(last_total, dict) and isinstance(last_usage, dict)

    use = [e for e in codex_scan.events if e.kind is EventKind.USAGE]
    assert len(use) == 2  # duplicate total_token_usage row produces no zero-delta event
    assert sum(e.tokens_in for e in use) == last_total["input_tokens"]
    assert sum(e.tokens_cached for e in use) == last_total["cached_input_tokens"]
    assert sum(e.tokens_out for e in use) == last_total["output_tokens"]
    assert sum(e.tokens_reasoning for e in use) == last_total["reasoning_output_tokens"]
    assert use[-1].ctx_tokens == last_usage["input_tokens"]


def test_rate_limits(codex_scan: CodexScanResult) -> None:
    expected = _fixture_summary()
    raw_limits = expected["last_limits"]
    assert isinstance(raw_limits, dict)
    primary = raw_limits["primary"]
    secondary = raw_limits["secondary"]
    assert isinstance(primary, dict) and isinstance(secondary, dict)

    limits = codex_scan.limits
    assert isinstance(limits, CodexLimits)
    assert limits.window_minutes == primary["window_minutes"]
    assert limits.window_minutes > 0
    assert limits.used_percent == primary["used_percent"]
    assert 0 <= limits.used_percent <= 100
    assert limits.secondary_used_percent == secondary["used_percent"]
    assert limits.resets_at == datetime.fromtimestamp(primary["resets_at"], tz=timezone.utc)


def test_partial_line_waits_for_newline(tmp_codex_home: Path) -> None:
    fc_idx = next(
        i for i, rec in enumerate(FIXTURE_RECORDS)
        if rec.get("type") == "response_item" and rec.get("payload", {}).get("type") == "function_call"
    )
    partial = FIXTURE_LINES[fc_idx][:30]
    path = _write_rollout(
        tmp_codex_home,
        FIXTURE_LINES[:fc_idx] + [partial],
        trailing_newline=False,
    )

    sc = CodexScanner(tmp_codex_home, proc_provider=lambda: [_FakeProc(cmdline=["codex.exe"])])
    res1 = sc.scan()
    assert len(res1.events) > 0
    assert EventKind.TOOL_START not in {e.kind for e in res1.events}

    _append(path, FIXTURE_LINES[fc_idx][30:] + "\n")
    res2 = sc.scan()
    assert [e.kind for e in res2.events] == [EventKind.TOOL_START]
    assert res2.events[0].tool == FIXTURE_RECORDS[fc_idx]["payload"]["name"]


def test_process_gone_without_completion_is_failed(tmp_codex_home: Path) -> None:
    rollout_id = str(_fixture_summary()["rollout_id"])
    no_complete_lines = [
        line for line, rec in zip(FIXTURE_LINES, FIXTURE_RECORDS)
        if not (rec.get("type") == "event_msg" and rec.get("payload", {}).get("type") == "task_complete")
    ]
    _write_rollout(tmp_codex_home, no_complete_lines)

    alive = True
    sc = CodexScanner(
        tmp_codex_home,
        proc_provider=lambda: [_FakeProc(cmdline=["codex.exe"])] if alive else [],
    )
    res1 = sc.scan()
    assert res1.running == {f"codex:{rollout_id}"}
    assert not any(e.kind is EventKind.END for e in res1.events)

    alive = False
    res2 = sc.scan()
    assert res2.running == set()
    ends = [e for e in res2.events if e.kind is EventKind.END]
    assert len(ends) == 1
    assert ends[0].status is Status.FAILED
    assert ends[0].agent_key == f"codex:{rollout_id}"

    # Subsequent scan does not re-emit END.
    res3 = sc.scan()
    assert res3.events == []
    assert res3.running == set()


def test_running_by_mtime_vs_prompt_prefix(tmp_codex_home: Path) -> None:
    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    now_ts = now.timestamp()
    rollout_id = str(_fixture_summary()["rollout_id"])
    prompt_one = "First rollout task that already went stale without match"
    prompt_two = "Investigate the failing parser pipeline in codex module"

    def _with_prompt(rid: str, prompt: str) -> list[str]:
        rows = list(FIXTURE_LINES[:-1])
        rows[0] = rows[0].replace(rollout_id, rid)
        rows[4] = json.dumps({
            "timestamp": "2026-11-18T11:50:00.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": prompt},
        })
        rows[5] = json.dumps({
            "timestamp": "2026-11-18T11:50:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            },
        })
        return rows

    # Older than 15s, no prompt match -> NOT running.
    path = _write_rollout(
        tmp_codex_home,
        _with_prompt(rollout_id, prompt_one),
        day=now,
        mtime=now_ts - 60.0,
    )
    active_procs: list[_FakeProc] = [_FakeProc(cmdline=["codex.exe", "exec", "unrelated task"])]
    sc = CodexScanner(tmp_codex_home, proc_provider=lambda: active_procs)

    res1 = sc.scan(now)
    assert res1.running == set()
    assert any(e.kind is EventKind.END and e.status is Status.FAILED for e in res1.events)

    # New rollout with old mtime (>15s) BUT matching prompt prefix in node cmdline -> RUNNING.
    other_id = "00000000-0000-0000-0000-000000000002"
    _write_rollout(
        tmp_codex_home,
        _with_prompt(other_id, prompt_two),
        day=now,
        filename=f"rollout-2026-11-18T11-50-00-{other_id}.jsonl",
        mtime=now_ts - 120.0,
    )
    active_procs.append(
        _FakeProc(cmdline=["node.exe", "C:\\npm\\codex.js", "exec", prompt_two[:45]])
    )
    res2 = sc.scan(now)
    assert res2.running == {f"codex:{other_id}"}

    # Fresh mtime (<=15s) on first rollout with unrelated codex process -> RUNNING.
    os.utime(path, (now_ts - 5.0, now_ts - 5.0))
    res3 = sc.scan(now)
    assert res3.running == {f"codex:{rollout_id}", f"codex:{other_id}"}


def test_scans_only_today_and_yesterday_utc_and_local(tmp_codex_home: Path) -> None:
    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    yesterday = now - timedelta(days=1)
    old_day = now - timedelta(days=3)

    id_today = "00000000-0000-0000-0000-000000000011"
    id_yest = "00000000-0000-0000-0000-000000000012"
    id_old = "00000000-0000-0000-0000-000000000013"
    id_stale_yest = "00000000-0000-0000-0000-000000000014"

    base_id = str(_fixture_summary()["rollout_id"])
    _write_rollout(
        tmp_codex_home,
        [FIXTURE_LINES[0].replace(base_id, id_today), FIXTURE_LINES[2], FIXTURE_LINES[-1]],
        day=now,
        filename=f"rollout-a-{id_today}.jsonl",
        mtime=now.timestamp() - 60,
    )
    _write_rollout(
        tmp_codex_home,
        [FIXTURE_LINES[0].replace(base_id, id_yest), FIXTURE_LINES[2], FIXTURE_LINES[-1]],
        day=yesterday,
        filename=f"rollout-b-{id_yest}.jsonl",
        mtime=now.timestamp() - 6 * 3600,
    )
    # In yesterday's folder, but outside window_hours=12.
    _write_rollout(
        tmp_codex_home,
        [FIXTURE_LINES[0].replace(base_id, id_stale_yest), FIXTURE_LINES[2], FIXTURE_LINES[-1]],
        day=yesterday,
        filename=f"rollout-c-{id_stale_yest}.jsonl",
        mtime=now.timestamp() - 18 * 3600,
    )
    # Outside today/yesterday folders even with recent mtime.
    _write_rollout(
        tmp_codex_home,
        [FIXTURE_LINES[0].replace(base_id, id_old), FIXTURE_LINES[2], FIXTURE_LINES[-1]],
        day=old_day,
        filename=f"rollout-d-{id_old}.jsonl",
        mtime=now.timestamp() - 60,
    )

    res = CodexScanner(tmp_codex_home, window_hours=12, proc_provider=lambda: []).scan(now)
    found_ids = {c.rollout_id for c in res.convs}
    assert found_ids == {id_today, id_yest}


def test_config_codex_home_and_sessions_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert config.codex_home() == Path.home() / ".codex"
    assert config.codex_sessions_dir() == Path.home() / ".codex" / "sessions"

    custom = tmp_path / "my-codex"
    monkeypatch.setenv("CODEX_HOME", str(custom))
    assert config.codex_home() == custom
    assert config.codex_sessions_dir() == custom / "sessions"


def test_codex_processes_matching(monkeypatch: pytest.MonkeyPatch) -> None:
    class _MockProc:
        def __init__(
            self,
            pid: int,
            name: str,
            cmdline: list[str] | Exception,
            cwd: str | None = "C:\\work\\proj",
        ) -> None:
            self.info = {"pid": pid, "name": name, "create_time": 1234.0}
            self._cmdline = cmdline
            self._cwd = cwd

        def cmdline(self) -> list[str]:
            if isinstance(self._cmdline, Exception):
                raise self._cmdline
            return self._cmdline

        def cwd(self) -> str | None:
            return self._cwd

    fake_list = [
        _MockProc(1, "codex", ["codex", "exec", "hi"]),
        _MockProc(2, "codex.exe", psutil.AccessDenied(2)),
        _MockProc(3, "node", ["node", "/usr/local/lib/node_modules/@openai/codex/bin/codex.js"]),
        _MockProc(4, "node.exe", ["node.exe", "C:\\npm\\Codex\\cli.js", "exec"]),
        _MockProc(5, "node.exe", ["node.exe", "C:\\app\\server.js"]),
        _MockProc(6, "node", psutil.AccessDenied(6)),
        _MockProc(7, "python.exe", ["python.exe", "codex.py"]),
    ]
    monkeypatch.setattr(procs.psutil, "process_iter", lambda attrs=None: fake_list)

    matched = procs.codex_processes()
    assert [p.pid for p in matched] == [1, 2, 3, 4]
    assert matched[0].cmdline == ["codex", "exec", "hi"]
    assert matched[1].cmdline == []


def test_parse_rollout_line_edges_truncation_and_store(codex_scan: CodexScanResult) -> None:
    # CodexScanResult constructs empty with defaults.
    empty = CodexScanResult()
    assert empty.events == []
    assert empty.convs == []
    assert empty.running == set()
    assert empty.limits is None

    state: dict[str, object] = {}
    assert parse_rollout_line({}, "r1", "s1", state) == []
    assert parse_rollout_line({"type": "event_msg", "payload": {"type": "token_count", "info": None}}, "r1", "s1", state) == []

    # Tool body truncation and non-zero exit_code error summary.
    oversized = "X" * (config.BODY_CAP + 40)
    assert parse_rollout_line(
        {
            "timestamp": "2026-11-18T01:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell_command",
                "arguments": '{"command": "pytest"}',
                "call_id": "c_err",
            },
        },
        "r1",
        "s1",
        state,
    )[0].summary == "shell_command pytest"

    end_evs = parse_rollout_line(
        {
            "timestamp": "2026-11-18T01:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c_err",
                "output": {"output": oversized, "metadata": {"exit_code": 1}},
            },
        },
        "r1",
        "s1",
        state,
    )
    assert len(end_evs) == 1
    assert end_evs[0].kind is EventKind.TOOL_END
    assert end_evs[0].tool == "shell_command"
    assert end_evs[0].summary == "error: shell_command"
    assert len(end_evs[0].body) == config.BODY_CAP and end_evs[0].body.endswith("…")

    str_err_evs = parse_rollout_line(
        {
            "timestamp": "2026-11-18T01:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c_err",
                "output": '{"output":"fail","metadata":{"exit_code":2}}',
            },
        },
        "r1",
        "s1",
        state,
    )
    assert str_err_evs[0].summary == "error: shell_command"

    assert summarize_codex_tool("apply_patch", "*** Begin Patch\n*** Update File: C:\\work\\proj\\a.py", "C:\\work\\proj") == "apply_patch a.py"

    # Store integration from fixture scan.
    expected = _fixture_summary()
    last_total = expected["last_total"]
    assert isinstance(last_total, dict)
    store = Store()
    store.apply(codex_scan.events)
    agent = store.find_agent(f"codex:{expected['rollout_id']}")
    assert agent is not None
    assert agent.source is Source.CODEX
    assert agent.status is Status.DONE
    assert agent.model == expected["model"]
    assert agent.effort == expected["effort"]
    assert agent.in_tokens == last_total["input_tokens"]
    assert agent.cached_tokens == last_total["cached_input_tokens"]
    assert agent.out_tokens == last_total["output_tokens"]
    assert agent.reasoning_tokens == last_total["reasoning_output_tokens"]


def test_timeout_failed_reopens_on_later_activity_and_task_complete(tmp_codex_home: Path) -> None:
    now0 = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
    rollout_id = "00000000-0000-0000-0000-000000000099"
    agent_key = f"codex:{rollout_id}"

    initial = [
        json.dumps({
            "timestamp": "2026-11-18T12:00:00Z",
            "type": "session_meta",
            "payload": {"id": rollout_id, "cwd": "C:\\work\\proj"},
        }),
        json.dumps({
            "timestamp": "2026-11-18T12:00:01Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-terra", "effort": "medium"},
        }),
        json.dumps({
            "timestamp": "2026-11-18T12:00:02Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "<prompt>"},
        }),
        json.dumps({
            "timestamp": "2026-11-18T12:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell_command",
                "arguments": '{"command":"<cmd>"}',
                "call_id": "c1",
            },
        }),
    ]
    path = _write_rollout(
        tmp_codex_home,
        initial,
        day=now0,
        filename=f"rollout-2026-11-18T12-00-00-{rollout_id}.jsonl",
        mtime=now0.timestamp(),
    )

    alive = True
    sc = CodexScanner(
        tmp_codex_home,
        proc_provider=lambda: [_FakeProc(cmdline=["codex.exe"])] if alive else [],
    )
    store = Store()

    res1 = sc.scan(now0)
    store.apply(res1.events)
    assert res1.running == {agent_key}
    assert not any(e.kind is EventKind.END for e in res1.events)
    agent = store.find_agent(agent_key)
    assert agent is not None and agent.status is Status.RUNNING

    def _pause_and_expect_timeout_failed(at: datetime) -> None:
        res_fail = sc.scan(at)
        store.apply(res_fail.events)
        assert res_fail.running == set()
        assert [(e.kind, e.status) for e in res_fail.events] == [(EventKind.END, Status.FAILED)]
        assert agent.status is Status.FAILED

    def _append_and_scan(at: datetime, rec: dict[str, object]) -> CodexScanResult:
        _append(path, json.dumps(rec) + "\n")
        os.utime(path, (at.timestamp(), at.timestamp()))
        res = sc.scan(at)
        store.apply(res.events)
        return res

    # >15s pause with no log writes and no prompt in cmdline -> premature FAILED,
    # reopened to RUNNING by TOOL_END, THINKING, TOOL_START, TEXT, and PROMPT.
    t = now0 + timedelta(seconds=20)
    _pause_and_expect_timeout_failed(t)
    t += timedelta(seconds=2)
    res_tool_end = _append_and_scan(t, {
        "timestamp": t.isoformat(),
        "type": "response_item",
        "payload": {"type": "function_call_output", "call_id": "c1", "output": "<result>"},
    })
    assert [(e.kind, e.status) for e in res_tool_end.events] == [(EventKind.TOOL_END, Status.RUNNING)]
    assert res_tool_end.running == {agent_key}
    assert agent.status is Status.RUNNING

    t += timedelta(seconds=20)
    _pause_and_expect_timeout_failed(t)
    t += timedelta(seconds=2)
    res_think = _append_and_scan(t, {
        "timestamp": t.isoformat(),
        "type": "response_item",
        "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "<thinking>"}]},
    })
    assert [(e.kind, e.status) for e in res_think.events] == [(EventKind.THINKING, Status.RUNNING)]
    assert agent.status is Status.RUNNING

    t += timedelta(seconds=20)
    _pause_and_expect_timeout_failed(t)
    t += timedelta(seconds=2)
    res_tool_start = _append_and_scan(t, {
        "timestamp": t.isoformat(),
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "shell_command",
            "arguments": '{"command":"<cmd2>"}',
            "call_id": "c2",
        },
    })
    assert [(e.kind, e.status) for e in res_tool_start.events] == [(EventKind.TOOL_START, Status.RUNNING)]
    assert agent.status is Status.RUNNING

    t += timedelta(seconds=20)
    _pause_and_expect_timeout_failed(t)
    t += timedelta(seconds=2)
    res_text = _append_and_scan(t, {
        "timestamp": t.isoformat(),
        "type": "event_msg",
        "payload": {"type": "agent_message", "message": "<text>"},
    })
    assert [(e.kind, e.status) for e in res_text.events] == [(EventKind.TEXT, Status.RUNNING)]
    assert agent.status is Status.RUNNING

    t += timedelta(seconds=20)
    _pause_and_expect_timeout_failed(t)
    t += timedelta(seconds=2)
    res_prompt = _append_and_scan(t, {
        "timestamp": t.isoformat(),
        "type": "event_msg",
        "payload": {"type": "user_message", "message": "<prompt2>"},
    })
    assert [(e.kind, e.status) for e in res_prompt.events] == [(EventKind.PROMPT, Status.RUNNING)]
    assert agent.status is Status.RUNNING

    # Direct task_complete after a premature timeout FAILED still emits END(DONE).
    t += timedelta(seconds=20)
    _pause_and_expect_timeout_failed(t)
    t += timedelta(seconds=2)
    alive = False
    res_done = _append_and_scan(t, {
        "timestamp": t.isoformat(),
        "type": "event_msg",
        "payload": {"type": "task_complete", "turn_id": "turn_1"},
    })
    assert [(e.kind, e.status) for e in res_done.events] == [(EventKind.END, Status.DONE)]
    assert res_done.running == set()
    assert agent.status is Status.DONE

    # Duplicate turn_complete after task_complete END(DONE) is suppressed.
    t += timedelta(seconds=1)
    res_dup = _append_and_scan(t, {
        "timestamp": t.isoformat(),
        "type": "event_msg",
        "payload": {"type": "turn_complete"},
    })
    assert res_dup.events == []


def test_multiturn_reopens_on_new_prompt(tmp_codex_home: Path) -> None:
    now0 = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
    rollout_id = str(_fixture_summary()["rollout_id"])
    agent_key = f"codex:{rollout_id}"

    path = _write_rollout(
        tmp_codex_home,
        FIXTURE_LINES,
        day=now0,
        mtime=now0.timestamp(),
    )
    sc = CodexScanner(tmp_codex_home, proc_provider=lambda: [_FakeProc(cmdline=["codex.exe"])])
    store = Store()

    res1 = sc.scan(now0)
    store.apply(res1.events)
    agent = store.find_agent(agent_key)
    assert agent is not None and agent.status is Status.DONE
    assert sum(1 for e in res1.events if e.kind is EventKind.END and e.status is Status.DONE) == 1

    # Turn 2 starts after END(DONE) -> PROMPT carries RUNNING, then END(DONE).
    t2 = now0 + timedelta(seconds=5)
    _append(
        path,
        "\n".join([
            json.dumps({"timestamp": "2026-11-18T12:00:05Z", "type": "event_msg", "payload": {"type": "task_started"}}),
            json.dumps({"timestamp": "2026-11-18T12:00:05Z", "type": "event_msg", "payload": {"type": "user_message", "message": "<prompt turn 2>"}}),
            json.dumps({"timestamp": "2026-11-18T12:00:05Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<prompt turn 2>"}]}}),
        ]) + "\n",
    )
    os.utime(path, (t2.timestamp(), t2.timestamp()))
    res2 = sc.scan(t2)
    store.apply(res2.events)
    assert [(e.kind, e.status) for e in res2.events] == [(EventKind.PROMPT, Status.RUNNING)]
    assert agent.status is Status.RUNNING

    t3 = now0 + timedelta(seconds=7)
    _append(
        path,
        "\n".join([
            json.dumps({"timestamp": "2026-11-18T12:00:07Z", "type": "event_msg", "payload": {"type": "agent_message", "message": "<text turn 2>"}}),
            json.dumps({"timestamp": "2026-11-18T12:00:07Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn_2"}}),
        ]) + "\n",
    )
    os.utime(path, (t3.timestamp(), t3.timestamp()))
    res3 = sc.scan(t3)
    store.apply(res3.events)
    assert [(e.kind, e.status) for e in res3.events] == [
        (EventKind.TEXT, None),
        (EventKind.END, Status.DONE),
    ]
    assert agent.status is Status.DONE


def test_created_does_not_advance_without_session_meta(tmp_codex_home: Path) -> None:
    now0 = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
    rollout_id = "00000000-0000-0000-0000-000000000077"
    initial = [
        json.dumps({
            "timestamp": "2026-11-18T11:55:00Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-terra", "effort": "medium"},
        }),
        json.dumps({
            "timestamp": "2026-11-18T11:55:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "<prompt>"},
        }),
    ]
    path = _write_rollout(
        tmp_codex_home,
        initial,
        day=now0,
        filename=f"rollout-2026-11-18T11-55-00-{rollout_id}.jsonl",
        mtime=now0.timestamp(),
    )
    sc = CodexScanner(tmp_codex_home, proc_provider=lambda: [_FakeProc(cmdline=["codex.exe"])])
    res1 = sc.scan(now0)
    expected_created = datetime(2026, 11, 18, 11, 55, 0, tzinfo=timezone.utc)
    assert res1.convs[0].created == expected_created

    t2 = now0 + timedelta(minutes=5)
    _append(
        path,
        json.dumps({
            "timestamp": "2026-11-18T12:05:00Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "<later text>"},
        }) + "\n",
    )
    os.utime(path, (t2.timestamp(), t2.timestamp()))
    res2 = sc.scan(t2)
    assert res2.convs[0].created == expected_created


def test_dedup_cleared_across_turns_and_effort_reinferred_on_model_change() -> None:
    state: dict[str, object] = {}

    # Initial turn_context infers effort from model; later turn_context re-infers on model change.
    s1 = parse_rollout_line(
        {"timestamp": "2026-11-18T01:00:00Z", "type": "turn_context", "payload": {"model": "gpt-5-high"}},
        "r_ctx",
        "s1",
        state,
    )
    assert s1[0].kind is EventKind.START and s1[0].effort == "high"
    assert state["effort"] == "high"

    assert parse_rollout_line(
        {"timestamp": "2026-11-18T01:00:01Z", "type": "turn_context", "payload": {"model": "gpt-5-low"}},
        "r_ctx",
        "s1",
        state,
    ) == []
    assert state["effort"] == "low"

    assert parse_rollout_line(
        {"timestamp": "2026-11-18T01:00:02Z", "type": "turn_context", "payload": {"model": "gpt-5.6-terra"}},
        "r_ctx",
        "s1",
        state,
    ) == []
    assert state["effort"] == ""

    assert parse_rollout_line(
        {
            "timestamp": "2026-11-18T01:00:03Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-terra", "effort": "medium"},
        },
        "r_ctx",
        "s1",
        state,
    ) == []
    assert state["effort"] == "medium"

    assert parse_rollout_line(
        {"timestamp": "2026-11-18T01:00:04Z", "type": "turn_context", "payload": {"model": "gpt-5.7"}},
        "r_ctx",
        "s1",
        state,
    ) == []
    assert state["effort"] == "medium"

    # Identical unpaired prompt/text across turns is not skipped after task_complete/task_started
    # or turn_complete/turn_started.
    p1 = parse_rollout_line(
        {"timestamp": "2026-11-18T01:00:10Z", "type": "event_msg", "payload": {"type": "user_message", "message": "same"}},
        "r_ctx",
        "s1",
        state,
    )
    t1 = parse_rollout_line(
        {
            "timestamp": "2026-11-18T01:00:11Z",
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "same reply"}]},
        },
        "r_ctx",
        "s1",
        state,
    )
    assert len(p1) == 1 and len(t1) == 1
    assert len(parse_rollout_line(
        {"timestamp": "2026-11-18T01:00:12Z", "type": "event_msg", "payload": {"type": "task_complete"}},
        "r_ctx",
        "s1",
        state,
    )) == 1

    p2 = parse_rollout_line(
        {
            "timestamp": "2026-11-18T01:00:13Z",
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "same"}]},
        },
        "r_ctx",
        "s1",
        state,
    )
    t2 = parse_rollout_line(
        {"timestamp": "2026-11-18T01:00:14Z", "type": "event_msg", "payload": {"type": "agent_message", "message": "same reply"}},
        "r_ctx",
        "s1",
        state,
    )
    assert len(p2) == 1 and p2[0].status is Status.RUNNING
    assert len(t2) == 1

    # Also cleared on turn_started even if previous turn had no completion event.
    assert parse_rollout_line(
        {"timestamp": "2026-11-18T01:00:15Z", "type": "event_msg", "payload": {"type": "turn_started"}},
        "r_ctx",
        "s1",
        state,
    ) == []
    p3 = parse_rollout_line(
        {"timestamp": "2026-11-18T01:00:16Z", "type": "event_msg", "payload": {"type": "user_message", "message": "same"}},
        "r_ctx",
        "s1",
        state,
    )
    t3 = parse_rollout_line(
        {
            "timestamp": "2026-11-18T01:00:17Z",
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "same reply"}]},
        },
        "r_ctx",
        "s1",
        state,
    )
    assert len(p3) == 1 and len(t3) == 1

    assert len(parse_rollout_line(
        {"timestamp": "2026-11-18T01:00:18Z", "type": "event_msg", "payload": {"type": "turn_complete"}},
        "r_ctx",
        "s1",
        state,
    )) == 1
    p4 = parse_rollout_line(
        {
            "timestamp": "2026-11-18T01:00:19Z",
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "same"}]},
        },
        "r_ctx",
        "s1",
        state,
    )
    t4 = parse_rollout_line(
        {"timestamp": "2026-11-18T01:00:20Z", "type": "event_msg", "payload": {"type": "agent_message", "message": "same reply"}},
        "r_ctx",
        "s1",
        state,
    )
    assert len(p4) == 1 and p4[0].status is Status.RUNNING
    assert len(t4) == 1


def test_codex_tool_events_carry_call_id() -> None:
    state: dict[str, object] = {}
    start = parse_rollout_line(
        {
            "timestamp": "2026-11-18T01:00:00Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell_command",
                "arguments": '{"command": "ls"}',
                "call_id": "call_xyz",
            },
        },
        "r1",
        "s1",
        state,
    )
    end = parse_rollout_line(
        {
            "timestamp": "2026-11-18T01:00:01Z",
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "call_xyz", "output": "ok"},
        },
        "r1",
        "s1",
        state,
    )
    assert start[0].tool_id == "call_xyz" and end[0].tool_id == "call_xyz"

    no_id = parse_rollout_line(
        {
            "timestamp": "2026-11-18T01:00:02Z",
            "type": "response_item",
            "payload": {"type": "function_call", "name": "shell_command", "arguments": "{}"},
        },
        "r1",
        "s1",
        state,
    )
    assert no_id[0].tool_id is None
