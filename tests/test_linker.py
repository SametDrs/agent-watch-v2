"""Unit tests for agent_watch.linker."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_watch import config
from agent_watch.linker import (
    SESSION_PATH_RE,
    AgyConv,
    Link,
    is_agy_command,
    is_codex_command,
    link,
    link_codex,
    normalize_path,
    parse_agy_command,
    parse_codex_command,
)
from agent_watch.model import STANDALONE_SESSION_KEY, ShellCall
from agent_watch.sources.codex import CodexConv


def _ts(offset_seconds: float = 0.0) -> datetime:
    base = datetime(2026, 11, 17, 16, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(seconds=offset_seconds)


def _call(
    tool_use_id: str,
    command: str,
    *,
    session_id: str = "11111111-1111-1111-1111-111111111111",
    agent_key: str | None = None,
    start_s: float = 0.0,
    end_s: float | None = 60.0,
    cwd: str = "C:/proj",
) -> ShellCall:
    return ShellCall(
        session_id=session_id,
        agent_key=agent_key,
        tool_use_id=tool_use_id,
        ts_start=_ts(start_s),
        ts_end=_ts(end_s) if end_s is not None else None,
        cwd=cwd,
        command=command,
    )


def test_normalize_path() -> None:
    assert (
        normalize_path("file:///C:/Users/user/Desktop/projects/agent-watch/")
        == "c:/users/user/desktop/projects/agent-watch"
    )
    assert (
        normalize_path(r"C:\Users\user\Desktop\projects\agent-watch\\")
        == "c:/users/user/desktop/projects/agent-watch"
    )
    assert (
        normalize_path("file:///C%3A/Users/user/My%20Project/sub%5Cdir/")
        == "c:/users/user/my project/sub/dir"
    )
    assert normalize_path("") == ""
    assert normalize_path("/") == "/"


def test_session_path_re() -> None:
    uuid = "9af43e82-5772-4625-a46a-7cffea98c243"
    win_path = rf"C:\Users\user\AppData\Local\Temp\claude\5f1bb22aedc12112\{uuid}\scratchpad"
    posix_path = f"file:///C:/Users/user/AppData/Local/TEMP/CLAUDE/5f1bb22aedc12112/{uuid.upper()}"

    m_win = SESSION_PATH_RE.search(win_path)
    assert m_win is not None
    assert m_win.group(1) == uuid

    m_posix = SESSION_PATH_RE.search(posix_path)
    assert m_posix is not None
    assert m_posix.group(1).lower() == uuid

    assert SESSION_PATH_RE.search(r"C:\Temp\claude\hash\not-a-uuid\scratchpad") is None
    assert SESSION_PATH_RE.search(rf"C:\other\claude\hash\{uuid}") is None


def test_is_agy_command() -> None:
    assert is_agy_command(
        r'MSYS_NO_PATHCONV=1 "C:/Users/user/AppData/Local/agy/bin/agy.exe" '
        r'-p "Read T6.md" --model gemini-4-hydrogen-xhigh --print-timeout 300s'
    )
    assert is_agy_command(r"& 'C:\bin\agy.exe' --print 'Implement linker'")
    assert is_agy_command("agy --prompt 'Quick check'")
    assert is_agy_command("agy --print=hello")

    assert not is_agy_command("powershell -File C:/scripts/agy-quota.ps1")
    assert not is_agy_command("C:/scripts/agy-quota.ps1 -p something")
    assert not is_agy_command("agy models")
    assert not is_agy_command("agy.exe models --print-timeout 10s")
    assert not is_agy_command("agy status")
    assert not is_agy_command("mkdir -p out && agy models")
    assert not is_agy_command("")


def test_parse_agy_command_quotes_slashes_and_fallback() -> None:
    cmd_double = (
        r'MSYS_NO_PATHCONV=1 "C:/Users/user/AppData/Local/agy/bin/agy.exe" '
        r'-p "Read the task brief at C:\briefs\T0.md and implement it" '
        r'--model gemini-4-hydrogen-xhigh --mode accept-edits '
        r'--add-dir "C:\Users\user\Desktop\projects\agent-watch" '
        r'--print-timeout 300s'
    )
    parsed = parse_agy_command(cmd_double)
    assert parsed["prompt"] == r"Read the task brief at C:\briefs\T0.md and implement it"
    assert parsed["model"] == "gemini-4-hydrogen-xhigh"
    assert parsed["mode"] == "accept-edits"
    assert parsed["add_dirs"] == [r"C:\Users\user\Desktop\projects\agent-watch"]
    assert parsed["conversation_id"] is None

    cmd_single = (
        "& 'C:/bin/agy' --prompt 'Review \"T6\" carefully' "
        "--add-dir 'C:/proj/a' --add-dir=C:/proj/b "
        "--conversation conv-42 --model=flash --mode=plan"
    )
    parsed_single = parse_agy_command(cmd_single)
    assert parsed_single["prompt"] == 'Review "T6" carefully'
    assert parsed_single["add_dirs"] == ["C:/proj/a", "C:/proj/b"]
    assert parsed_single["conversation_id"] == "conv-42"
    assert parsed_single["model"] == "flash"
    assert parsed_single["mode"] == "plan"

    chained = 'mkdir -p build && agy -p "Real task prompt" --model m1 && mkdir -p dist'
    assert parse_agy_command(chained)["prompt"] == "Real task prompt"

    malformed = "agy -p fix Bob's bug --model gemini-4 --add-dir C:/proj"
    parsed_fallback = parse_agy_command(malformed)
    assert parsed_fallback["prompt"] == "fix"  # unquoted -p takes one token
    assert parsed_fallback["model"] == "gemini-4"
    assert parsed_fallback["add_dirs"] == ["C:/proj"]

    assert parse_agy_command("")["prompt"] == ""


def test_link_rule1_path_and_url_encoded_uri_with_subagent() -> None:
    sess_a = "9af43e82-5772-4625-a46a-7cffea98c243"
    sess_b = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    conv_sub = AgyConv(
        conv_id="agy-1",
        first_input_ts=_ts(2.0),
        first_input_text="<USER_REQUEST>\nImplement linker module cleanly for task six\n</USER_REQUEST>",
        workspace_paths=[
            f"file:///C%3A/Users/user/AppData/Local/Temp/claude/5f1bb22aedc12112/{sess_a}/scratchpad"
        ],
    )
    conv_cwd = AgyConv(
        conv_id="agy-2",
        first_input_ts=None,
        first_input_text="Inspect logs",
        workspace_paths=[],
        cwd=rf"C:\Users\user\AppData\Local\Temp\claude\5f1bb22aedc12112\{sess_b}\scratchpad",
    )

    calls = [
        _call(
            "tu-1",
            'agy.exe -p "Implement linker module cleanly for task six"',
            session_id=sess_a,
            agent_key="claude:sub-linker",
            start_s=0.0,
            end_s=50.0,
        )
    ]

    result = link([conv_sub, conv_cwd], calls, {})
    assert result["agy-1"] == Link(
        session_key=sess_a,
        parent_agent_key="claude:sub-linker",
        method="path",
        score=0.0,
    )
    assert result["agy-2"] == Link(
        session_key=sess_b,
        parent_agent_key=None,
        method="path",
        score=0.0,
    )


def test_link_rule2_conversation_id() -> None:
    conv = AgyConv(
        conv_id="conv-resume-99",
        first_input_ts=_ts(1000.0),
        first_input_text="Follow up",
        workspace_paths=["file:///C:/proj"],
    )
    calls = [
        _call(
            "tu-conv",
            'agy -p "Follow up" --conversation conv-resume-99',
            session_id="sess-conv",
            agent_key="claude:sub-resume",
            start_s=0.0,
            end_s=10.0,
        )
    ]
    result = link([conv], calls, {})
    assert result["conv-resume-99"] == Link(
        session_key="sess-conv",
        parent_agent_key="claude:sub-resume",
        method="conversation_id",
        score=0.0,
    )


def test_link_rule3_concurrent_calls_same_cwd_disambiguated_by_prompt() -> None:
    prompt_a = (
        "Read C:\\briefs\\common.md and then C:\\briefs\\T1.md to implement the "
        "Claude transcript parser with incremental tailing and unit tests."
    )
    prompt_b = (
        "Read C:\\briefs\\common.md and then C:\\briefs\\T2.md to implement the "
        "Antigravity transcript parser with usage extraction and unit tests."
    )

    conv_a = AgyConv(
        conv_id="c-a",
        first_input_ts=_ts(1.5),
        first_input_text=f"<USER_REQUEST>\n  {prompt_a}  \n</USER_REQUEST>",
        workspace_paths=["file:///C:/Users/user/Desktop/projects/agent-watch"],
    )
    conv_b = AgyConv(
        conv_id="c-b",
        first_input_ts=_ts(2.0),
        first_input_text=f"<USER_REQUEST>\n{prompt_b}\n</USER_REQUEST>",
        workspace_paths=[r"C:\Users\user\Desktop\projects\agent-watch"],
    )

    calls = [
        _call(
            "tu-a",
            rf'agy.exe -p "{prompt_a}" --add-dir "C:\Users\user\Desktop\projects\agent-watch"',
            session_id="sess-1",
            agent_key="claude:sub-a",
            start_s=0.0,
            end_s=120.0,
            cwd="C:/Users/user/Desktop/projects/agent-watch",
        ),
        _call(
            "tu-b",
            rf"agy -p '{prompt_b}' --add-dir 'C:/Users/user/Desktop/projects/agent-watch'",
            session_id="sess-1",
            agent_key="claude:sub-b",
            start_s=0.5,
            end_s=120.0,
            cwd="C:/Users/user/Desktop/projects/agent-watch",
        ),
    ]

    result = link([conv_a, conv_b], calls, {})
    assert result["c-a"].session_key == "sess-1"
    assert result["c-a"].parent_agent_key == "claude:sub-a"
    assert result["c-a"].method == "score"
    assert result["c-a"].score > 100.0

    assert result["c-b"].session_key == "sess-1"
    assert result["c-b"].parent_agent_key == "claude:sub-b"
    assert result["c-b"].method == "score"
    assert result["c-b"].score > 100.0


def test_link_rule3_single_candidate_fallback_and_loop_reuse() -> None:
    # Single candidate in window without >=20-char text match links by window uniqueness.
    conv_short = AgyConv(
        conv_id="c-short",
        first_input_ts=_ts(1.0),
        first_input_text="hi",
        workspace_paths=[],
    )
    call_single = _call(
        "tu-single",
        'agy -p "hi"',
        session_id="sess-single",
        agent_key=None,
        start_s=0.0,
        end_s=20.0,
    )

    # Two loop iterations sharing one ShellCall via text match, plus an unlinked short conv.
    loop_prompt = "Run verification sweep over all parser modules and report issues."
    conv_loop1 = AgyConv("c-loop1", _ts(101.0), loop_prompt, [])
    conv_loop2 = AgyConv("c-loop2", _ts(110.0), loop_prompt, [])
    conv_no_text = AgyConv("c-notext", _ts(115.0), "short", [])
    call_loop = _call(
        "tu-loop",
        f'agy -p "{loop_prompt}"',
        session_id="sess-loop",
        agent_key="claude:looper",
        start_s=100.0,
        end_s=200.0,
    )

    result = link(
        [conv_short, conv_loop1, conv_loop2, conv_no_text],
        [call_single, call_loop],
        {},
    )
    assert result["c-short"].method == "score"
    assert result["c-short"].session_key == "sess-single"

    assert result["c-loop1"].method == "score"
    assert result["c-loop1"].session_key == "sess-loop"
    assert result["c-loop1"].parent_agent_key == "claude:looper"

    assert result["c-loop2"].method == "score"
    assert result["c-loop2"].session_key == "sess-loop"
    assert result["c-loop2"].parent_agent_key == "claude:looper"

    # Cannot reuse an already-used call without a text match.
    assert result["c-notext"] == Link(STANDALONE_SESSION_KEY, None, "standalone")


def test_link_standalone_non_agy_commands_and_existing_links() -> None:
    conv_existing = AgyConv("c-exist", _ts(0.0), "ignored", [])
    conv_stale_standalone = AgyConv(
        "c-upgrade",
        _ts(2.0),
        "Upgrade standalone link once shell call appears",
        [],
    )
    conv_unmatched = AgyConv("c-free", _ts(500.0), "Manual interactive session", [])
    conv_no_ts = AgyConv("c-nots", None, "No timestamp conversation", [])

    preserved = Link("sess-old", "claude:old", "path", 0.0)
    existing = {
        "c-exist": preserved,
        "c-upgrade": Link(STANDALONE_SESSION_KEY, None, "standalone", 0.0),
    }

    calls = [
        _call("tu-quota", "powershell -File agy-quota.ps1", start_s=499.0, end_s=505.0),
        _call("tu-models", "agy models", start_s=499.0, end_s=505.0),
        _call(
            "tu-up",
            'agy -p "Upgrade standalone link once shell call appears"',
            session_id="sess-new",
            agent_key="claude:sub",
            start_s=0.0,
            end_s=30.0,
        ),
    ]

    result = link(
        [conv_existing, conv_stale_standalone, conv_unmatched, conv_no_ts],
        calls,
        existing,
    )
    assert result["c-exist"] == preserved
    assert result["c-upgrade"].session_key == "sess-new"
    assert result["c-upgrade"].parent_agent_key == "claude:sub"
    assert result["c-upgrade"].method == "score"
    assert result["c-free"] == Link(STANDALONE_SESSION_KEY, None, "standalone")
    assert result["c-nots"] == Link(STANDALONE_SESSION_KEY, None, "standalone")


def test_link_populates_effort_from_model_flag() -> None:
    sess_uuid = "9af43e82-5772-4625-a46a-7cffea98c243"
    conv_path = AgyConv(
        conv_id="c-path",
        first_input_ts=_ts(1.0),
        first_input_text="Path matched task",
        workspace_paths=[f"file:///C:/Temp/claude/hash/{sess_uuid}"],
    )
    conv_id = AgyConv(
        conv_id="conv-77",
        first_input_ts=_ts(100.0),
        first_input_text="Resumed task",
        workspace_paths=[],
    )
    conv_score = AgyConv(
        conv_id="c-score",
        first_input_ts=_ts(50.0),
        first_input_text="Score matched task",
        workspace_paths=[],
    )
    conv_no_effort = AgyConv(
        conv_id="c-no-effort",
        first_input_ts=_ts(200.0),
        first_input_text="Model without effort suffix",
        workspace_paths=[],
    )

    calls = [
        _call(
            "tu-path",
            'agy -p "Path matched task" --model gemini-4-hydrogen-medium',
            session_id=sess_uuid,
            agent_key="claude:sub1",
            start_s=0.0,
            end_s=10.0,
        ),
        _call(
            "tu-id",
            'agy -p "Resumed task" --conversation conv-77 --model=gemini-4-hydrogen-xhigh',
            session_id="sess-id",
            agent_key="claude:sub2",
            start_s=95.0,
            end_s=120.0,
        ),
        _call(
            "tu-score",
            'agy -p "Score matched task" --model gemini-4-hydrogen-high',
            session_id="sess-score",
            agent_key="claude:sub3",
            start_s=49.0,
            end_s=70.0,
        ),
        _call(
            "tu-plain",
            'agy -p "Model without effort suffix" --model claude-opus-4-6',
            session_id="sess-plain",
            agent_key=None,
            start_s=199.0,
            end_s=210.0,
        ),
    ]

    result = link([conv_path, conv_id, conv_score, conv_no_effort], calls, {})
    assert result["c-path"].method == "path" and result["c-path"].effort == "medium"
    assert result["conv-77"].method == "conversation_id" and result["conv-77"].effort == "xhigh"
    assert result["c-score"].method == "score" and result["c-score"].effort == "high"
    assert result["c-no-effort"].method == "score" and result["c-no-effort"].effort == ""


def _codex_conv(
    rollout_id: str,
    offset_s: float = 0.0,
    first_prompt: str = "",
    cwd: str = "C:/proj",
    model: str = "gpt-5.6-terra",
    effort: str = "",
) -> CodexConv:
    return CodexConv(
        rollout_id=rollout_id,
        path=Path(f"rollout-{rollout_id}.jsonl"),
        created=_ts(offset_s),
        first_prompt=first_prompt,
        cwd=cwd,
        model=model,
        effort=effort,
    )


def test_parse_codex_command() -> None:
    p = parse_codex_command(
        'codex exec -m gpt-5.6-terra -c model_reasoning_effort="high" -C D:\\x "fix the bug in foo"'
    )
    assert p == {
        "prompt": "fix the bug in foo",
        "model": "gpt-5.6-terra",
        "effort": "high",
        "cwd": "D:\\x",
    }
    assert not is_codex_command("echo codex")
    assert not is_codex_command("echo codex exec hi")
    assert not is_codex_command("codex")
    assert not is_codex_command("codex --version")
    assert not is_codex_command("codex login")
    assert not is_codex_command("codex -m gpt-5.6-terra")
    assert not is_codex_command("python codex.py")
    assert not is_codex_command("")

    assert is_codex_command('codex.exe e "quick task"')
    assert is_codex_command('npx codex exec "via npx"')
    assert is_codex_command("npx -y @openai/codex exec - < task.md")
    assert is_codex_command('codex -m gpt-5.6-terra "positional prompt"')
    assert is_codex_command('mkdir out && codex exec "chained task"')

    assert parse_codex_command("codex exec - < task.md")["prompt"] == ""
    assert parse_codex_command("codex exec -c model_reasoning_effort='low' \"t\"")["effort"] == "low"
    assert parse_codex_command("codex exec -c 'model_reasoning_effort=\"xhigh\"' \"t\"")["effort"] == "xhigh"
    assert parse_codex_command("codex exec --config=model_reasoning_effort=medium --model=m1 --cd=C:/w \"t\"") == {
        "prompt": "t",
        "model": "m1",
        "effort": "medium",
        "cwd": "C:/w",
    }
    assert parse_codex_command('codex exec -s workspace-write -m m2 "run sweep"') == {
        "prompt": "run sweep",
        "model": "m2",
        "effort": None,
        "cwd": None,
    }
    malformed = 'codex exec -m gpt-5 -c model_reasoning_effort="high" "fix Bob\'s bug'
    assert parse_codex_command(malformed) == {
        "prompt": "fix Bob's bug",
        "model": "gpt-5",
        "effort": "high",
        "cwd": None,
    }
    assert is_codex_command(malformed)
    assert not is_codex_command('codex login "unclosed')
    assert not is_codex_command('codex -m gpt-5 status "unclosed')
    assert not is_codex_command('codex --help "unclosed')
    assert not is_codex_command('codex --version "unclosed')
    assert not is_codex_command('codex exec --help "unclosed')
    assert is_codex_command('codex exec "fix --help \'bug')


def test_parallel_same_prompt_links_one_to_one() -> None:
    prompt = "Implement Task 6 codex linking module cleanly for agent watch"
    convs = [
        _codex_conv("r1", 1.0, first_prompt=prompt),
        _codex_conv("r2", 2.0, first_prompt=prompt),
    ]
    calls = [
        _call(
            "t1",
            f'codex exec -m gpt-5.6-terra -c model_reasoning_effort="high" "{prompt}"',
            session_id="s1",
            agent_key="A",
            start_s=0.0,
            end_s=60.0,
        ),
        _call(
            "t2",
            f'codex exec -m gpt-5.6-terra -c model_reasoning_effort="medium" "{prompt}"',
            session_id="s1",
            agent_key="B",
            start_s=1.0,
            end_s=60.0,
        ),
    ]
    links = link_codex(convs, calls)
    assert set(links) == {"r1", "r2"}
    assert {links["r1"].parent_agent_key, links["r2"].parent_agent_key} == {"A", "B"}
    assert all(lnk.method == "score" and lnk.session_key == "s1" for lnk in links.values())
    assert {links["r1"].effort, links["r2"].effort} == {"high", "medium"}


def test_time_window_fallback_when_prompt_from_file() -> None:
    links = link_codex(
        [_codex_conv("r1", 4.0, first_prompt="<other>")],
        [
            _call(
                "t1",
                'codex exec -c model_reasoning_effort="high" - < task.md',
                session_id="s1",
                agent_key="claude:sub1",
                start_s=0.0,
                end_s=20.0,
            )
        ],
    )
    assert links["r1"].method == "window"
    assert links["r1"].session_key == "s1"
    assert links["r1"].parent_agent_key == "claude:sub1"
    assert links["r1"].effort == "high"

    win = float(config.CODEX_LINK_WINDOW_SECONDS)
    edge = link_codex(
        [_codex_conv("r-edge", win, first_prompt="<other>")],
        [_call("t1", "codex exec - < task.md", start_s=0.0, end_s=1.0)],
    )
    assert "r-edge" in edge and edge["r-edge"].method == "window"

    too_late = link_codex(
        [_codex_conv("r-late", win + 1.0, first_prompt="<other>")],
        [_call("t1", "codex exec - < task.md", start_s=0.0, end_s=1.0)],
    )
    assert too_late == {}

    too_early = link_codex(
        [_codex_conv("r-early", -1.0, first_prompt="<other>")],
        [_call("t1", "codex exec - < task.md", start_s=0.0, end_s=1.0)],
    )
    assert too_early == {}


def test_link_codex_existing_strict_one_to_one_and_unmatched() -> None:
    prompt = "Implement Task 6 codex linking module cleanly for agent watch"
    r1 = _codex_conv("r1", 1.0, first_prompt=prompt)
    r2 = _codex_conv("r2", 2.0, first_prompt=prompt)
    t1 = _call(
        "t1",
        f'codex exec -c model_reasoning_effort="high" "{prompt}"',
        session_id="s1",
        agent_key="A",
        start_s=0.0,
        end_s=60.0,
    )

    first = link_codex([r1], [t1])
    assert set(first) == {"r1"}

    second = link_codex([r1, r2], [t1], existing=first)
    assert set(second) == {"r1"}
    assert second["r1"] == first["r1"]

    manual_existing = {"r1": Link(session_key="s1", parent_agent_key="A", method="score", score=99.0)}
    third = link_codex([r1, r2], [t1], existing=manual_existing)
    assert set(third) == {"r1"}

    assert link_codex([r1], [_call("t-agy", 'agy -p "hi"', start_s=0.0)]) == {}


def test_link_codex_stale_call_id_does_not_reserve_unrelated_call() -> None:
    prompt = "Implement Task 6 codex linking module cleanly for agent watch"
    r1 = _codex_conv("r1", 1.0, first_prompt=prompt)
    r2 = _codex_conv("r2", 11.0, first_prompt=prompt)
    t2 = _call(
        "t2",
        f'codex exec -c model_reasoning_effort="high" "{prompt}"',
        session_id="s1",
        agent_key="A",
        start_s=10.0,
        end_s=60.0,
    )
    existing = {
        "r1": Link(
            session_key="s1",
            parent_agent_key="A",
            method="score",
            score=99.0,
            call_id="t-gone",
        )
    }

    links = link_codex([r1, r2], [t2], existing=existing)
    assert set(links) == {"r1", "r2"}
    assert links["r1"] == existing["r1"]
    assert links["r2"].session_key == "s1"
    assert links["r2"].parent_agent_key == "A"
    assert links["r2"].call_id == "t2"
