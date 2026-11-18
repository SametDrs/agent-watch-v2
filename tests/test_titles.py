"""Tests for short_title and its use by the Claude/Codex sources."""

import json
from pathlib import Path

from agent_watch.model import EventKind
from agent_watch.sources.claude import ClaudeScanner
from agent_watch.sources.codex import CodexScanner
from agent_watch.titles import TITLE_MAX, short_title

from test_claude import NOW, SID, SLUG, build_home
from test_codex import _write_rollout


def test_empty_inputs() -> None:
    assert short_title(None) == ""
    assert short_title("") == ""
    assert short_title("   \n ") == ""


def test_strips_slash_command() -> None:
    assert short_title("/plan fix the login bug") == "fix the login bug"
    assert short_title("/superpowers:brainstorming add a cache") == "add a cache"


def test_slash_command_does_not_eat_paths() -> None:
    assert short_title("/etc/hosts needs a look") == "/etc/hosts needs a look"


def test_strips_label_prefixes() -> None:
    assert short_title("GOAL: Produce an interface map") == "Produce an interface map"
    assert short_title("task: do it") == "do it"
    assert short_title("/plan GOAL: Map the project") == "Map the project"
    assert short_title("GOAL:\nFix the bug in foo.py") == "Fix the bug in foo.py"


def test_strips_brief_pointer() -> None:
    assert short_title("Read and follow the brief in /tmp/b.md. Then do Y") == "Then do Y"
    assert short_title("Read and follow the brief in docs/briefs/task3.md") == "task3.md"
    assert short_title(r"Read and follow the brief in C:\x\briefs\t3.md") == "t3.md"


def test_first_sentence_and_line() -> None:
    assert short_title("Fix the bug. Then run the tests.") == "Fix the bug"
    assert short_title("## Heading\nbody text") == "Heading"
    assert short_title("see e.g. foo bar") == "see e.g. foo bar"
    assert short_title("Bump v1.2 now") == "Bump v1.2 now"


def test_collapses_whitespace() -> None:
    assert short_title("a   b\t\tc") == "a b c"


def test_cap_at_word_boundary() -> None:
    text = "Implement Task 3 (all code+test steps; skip shell tests) and report back in detail"
    out = short_title(text)
    assert len(out) <= TITLE_MAX
    assert out.endswith("…")
    assert out == "Implement Task 3 (all code+test steps; skip shell tests)…"
    assert text.startswith(out[:-1].rstrip())


def test_cap_hard_cut_for_single_long_word() -> None:
    out = short_title("x" * 100)
    assert len(out) == TITLE_MAX
    assert out.endswith("…")


def test_short_text_unchanged() -> None:
    assert short_title("x" * TITLE_MAX) == "x" * TITLE_MAX


def _sub_title(tmp_path: Path, meta: dict | None, prompt: str) -> str | None:
    home = build_home(tmp_path)
    sub_dir = home / "projects" / SLUG / SID / "subagents"
    aid = "afallback0001"
    (sub_dir / f"agent-{aid}.jsonl").write_text(
        json.dumps({
            "type": "user",
            "timestamp": "2026-11-17T16:00:03Z",
            "message": {"role": "user", "content": prompt},
        }) + "\n",
        encoding="utf-8",
    )
    if meta is not None:
        (sub_dir / f"agent-{aid}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    res = ClaudeScanner(home, window_hours=24 * 365).scan(NOW)
    starts = [e for e in res.events if e.kind == EventKind.START and e.agent_key == f"claude:{aid}"]
    assert len(starts) == 1
    return starts[0].title


def test_claude_sub_prefers_meta_description(tmp_path: Path) -> None:
    assert _sub_title(tmp_path, {"description": "Filter, notify, timeline"}, "/plan GOAL: long prompt") == (
        "Filter, notify, timeline"
    )


def test_claude_sub_falls_back_to_prompt(tmp_path: Path) -> None:
    title = _sub_title(tmp_path, {"model": "sonnet"}, "/plan GOAL: Produce an interface map. More text")
    assert title == "Produce an interface map"


def test_claude_sub_blank_description_falls_back(tmp_path: Path) -> None:
    assert _sub_title(tmp_path, {"description": "  "}, "Implement Task 3") == "Implement Task 3"


def test_claude_sub_keeps_full_prompt_event(tmp_path: Path) -> None:
    home = build_home(tmp_path)
    sub_dir = home / "projects" / SLUG / SID / "subagents"
    prompt = "/plan GOAL: " + "very long prompt text " * 20
    (sub_dir / "agent-afull.jsonl").write_text(
        json.dumps({"type": "user", "timestamp": "2026-11-17T16:00:03Z",
                    "message": {"role": "user", "content": prompt}}) + "\n",
        encoding="utf-8",
    )
    res = ClaudeScanner(home, window_hours=24 * 365).scan(NOW)
    prompts = [e for e in res.events if e.agent_key == "claude:afull" and e.kind == EventKind.PROMPT]
    assert prompts and prompts[0].body == prompt


def test_codex_title_from_prompt_and_full_prompt_kept(tmp_path: Path) -> None:
    prompt = "/plan GOAL: Produce an interface map of this project and its tests, then " + "x " * 40
    lines = [
        json.dumps({"timestamp": "2026-11-17T10:00:00Z", "type": "session_meta",
                    "payload": {"id": "rid-1", "cwd": "/w", "timestamp": "2026-11-17T10:00:00Z"}}),
        json.dumps({"timestamp": "2026-11-17T10:00:01Z", "type": "event_msg",
                    "payload": {"type": "user_message", "message": prompt}}),
    ]
    home = tmp_path / "codex_home"
    home.mkdir()
    _write_rollout(home, lines, filename="rollout-2026-11-17T10-00-00-rid-1.jsonl")
    res = CodexScanner(home, proc_provider=lambda: []).scan()
    start = [e for e in res.events if e.kind is EventKind.PROMPT]
    assert start
    assert start[0].title == "Produce an interface map of this project and its tests"
    assert start[0].body == prompt


def test_long_text_cuts_at_clause_outside_parens() -> None:
    text = "Produce an interface map of this project, then write a long report about everything"
    assert short_title(text) == "Produce an interface map of this project"
    # a ';' inside parentheses is not a clause boundary
    assert short_title("Implement Task 3 (all code+test steps; skip shell tests) and report back in detail").endswith("…")
