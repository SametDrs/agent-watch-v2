"""Pilot tests for the wired agent-watch application (fake homes only)."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from test_claude import AGENTS, SID, build_home
from test_poller import LINKED_CONV, _FakeProc, SUB_ID, add_agy_shell_call, place_agy
import pytest

from agent_watch import __version__, launcher
from agent_watch.app import AgentWatchApp, build_parser, main
from agent_watch.model import Event, EventKind, Status, main_agent_key
from agent_watch.poller import PollResult
from agent_watch.ui.dashboard import DashboardScreen
from agent_watch.ui.theme import ACCENT, BG, BORDER, STATUS_COLORS, SURFACE, SURFACE_HI, TEXT
from agent_watch.ui.widgets import CardGrid, QuotaPanel


def make_app(tmp_path: Path) -> tuple[AgentWatchApp, Path]:
    claude_home = build_home(tmp_path)
    add_agy_shell_call(claude_home)
    agy_home = tmp_path / "agy"
    place_agy(agy_home, LINKED_CONV, "transcript_tools.jsonl")
    app = AgentWatchApp(
        claude_home=claude_home,
        agy_home=agy_home,
        codex_home=tmp_path / "codex",
        no_quota=True,
        window_hours=24 * 365,
        proc_provider=lambda: [],
    )
    return app, claude_home


async def wait_until(pilot, predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause()
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


def card_text(app: AgentWatchApp, agent_key: str) -> str:
    card = app.screen.query_one(CardGrid).cards[agent_key]
    return card.content.plain


async def test_dashboard_populates_and_updates_live(tmp_path: Path) -> None:
    app, claude_home = make_app(tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        assert await wait_until(pilot, lambda: app.first_poll_done and app.screen.query_one(CardGrid).cards, 3.0)
        screen = app.screen
        assert isinstance(screen, DashboardScreen)
        assert app.sub_title == ""
        assert screen.tab_keys and screen.tab_keys[0] == SID
        cards = screen.query_one(CardGrid).cards
        assert all(f"claude:{aid}" in cards for aid in AGENTS)
        assert f"agy:{LINKED_CONV}" in cards

        key = f"claude:{SUB_ID}"
        assert "SubagentHandback" in card_text(app, key)
        assert "tok" in card_text(app, key) and " out" in card_text(app, key)

        path = next(claude_home.glob(f"projects/*/{SID}/subagents/agent-{SUB_ID}.jsonl"))
        line = {
            "type": "assistant",
            "timestamp": "2026-11-17T16:59:00Z",
            "message": {"role": "assistant", "model": "claude-haiku-5-5", "content": [{
                "type": "tool_use", "id": "toolu_live", "name": "Grep", "input": {"pattern": "needle"},
            }]},
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")

        assert await wait_until(pilot, lambda: "Grep needle" in card_text(app, key), 1.5)


def test_cli_has_no_chat_options():
    opts = {a for act in build_parser()._actions for a in act.option_strings}
    assert "--model" not in opts and "--cwd" not in opts


def test_dashboard_has_no_chat_binding():
    assert all(b.key != "c" for b in DashboardScreen.BINDINGS)


def test_main_version(capsys) -> None:
    try:
        main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    assert capsys.readouterr().out.strip() == f"agent-watch {__version__}"


def test_cli_codex_home_and_app_wires_codex_scanner(tmp_path: Path) -> None:
    codex_dir = tmp_path / "codex_cli"
    args = build_parser().parse_args(["--codex-home", str(codex_dir)])
    assert args.codex_home == str(codex_dir)
    app = AgentWatchApp(
        claude_home=tmp_path / "c",
        agy_home=tmp_path / "a",
        codex_home=codex_dir,
        no_quota=True,
        proc_provider=lambda: [],
    )
    assert app.codex_home == codex_dir
    assert app.poller.codex_scanner is not None
    assert app.poller.codex_scanner.codex_home == codex_dir


async def test_app_wires_codex_limits_to_quota_panel(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    rid = "00000000-0000-0000-0000-00000000c999"
    codex_home = tmp_path / "codex"
    rollout = (
        codex_home
        / "sessions"
        / f"{now:%Y}"
        / f"{now:%m}"
        / f"{now:%d}"
        / f"rollout-{now:%Y-%m-%dT%H-%M-%S}-{rid}.jsonl"
    )
    rollout.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {"timestamp": iso, "type": "session_meta", "payload": {"id": rid, "timestamp": iso, "cwd": "C:\\work"}},
        {"timestamp": iso, "type": "turn_context", "payload": {"cwd": "C:\\work", "model": "gpt-5.6-terra"}},
        {"timestamp": iso, "type": "event_msg", "payload": {"type": "user_message", "message": "<prompt>"}},
        {
            "timestamp": iso,
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": None,
                "rate_limits": {"primary": {"used_percent": 31.0, "window_minutes": 300, "resets_at": 1795000000}},
            },
        },
    ]
    rollout.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    app = AgentWatchApp(
        claude_home=tmp_path / "claude",
        agy_home=tmp_path / "agy",
        codex_home=codex_home,
        no_quota=True,
        window_hours=24,
        proc_provider=lambda: [],
    )
    async with app.run_test(size=(160, 50)) as pilot:
        assert await wait_until(
            pilot,
            lambda: "codex" in app.screen.query_one(QuotaPanel).render().plain,
            3.0,
        )
        plain = app.screen.query_one(QuotaPanel).render().plain
        assert "codex  31% used" in plain and "resets" in plain


async def test_on_mount_releases_claim_on_acquire_oserror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert launcher.try_claim_launch() is True
    claim = launcher.run_dir() / "launch.claim"
    assert claim.exists()

    def _raise_oserror(*args: object, **kwargs: object) -> int | None:
        raise OSError("lock unavailable")

    monkeypatch.setattr(launcher, "acquire_instance_lock_retrying", _raise_oserror)

    app = AgentWatchApp(
        claude_home=tmp_path / "c",
        agy_home=tmp_path / "a",
        codex_home=tmp_path / "x",
        no_quota=True,
        proc_provider=lambda: [],
    )
    with caplog.at_level("WARNING"):
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert not claim.exists()
            assert app._instance_lock is None
            assert isinstance(app.screen, DashboardScreen)
    assert "Failed to acquire instance lock" in caplog.text


async def test_poller_last_error_shown_and_cleared_in_sub_title(tmp_path: Path) -> None:
    app = AgentWatchApp(
        claude_home=tmp_path / "c",
        agy_home=tmp_path / "a",
        codex_home=tmp_path / "x",
        no_quota=True,
        poll_ms=50,
        proc_provider=lambda: [],
    )
    fail = True
    orig_poll = app.poller.poll_once

    def scripted_poll() -> PollResult:
        if fail:
            app.poller.last_error = "agy: RuntimeError('boom')"
            return PollResult()
        return orig_poll()

    app.poller.poll_once = scripted_poll  # type: ignore[method-assign]
    async with app.run_test(size=(100, 30)) as pilot:
        assert await wait_until(pilot, lambda: app.sub_title == "agy: RuntimeError('boom')", 2.0)
        fail = False
        assert await wait_until(pilot, lambda: app.sub_title == "", 2.0)


def test_window_hours_help_mentions_agy_and_codex() -> None:
    help_text = " ".join(build_parser().format_help().split())
    assert "--window-hours" in help_text
    assert "agy" in help_text and "Codex" in help_text and "only" in help_text


async def test_agent_watch_theme_and_scrollbars(tmp_path: Path) -> None:
    app = AgentWatchApp(
        claude_home=tmp_path / "c",
        agy_home=tmp_path / "a",
        codex_home=tmp_path / "x",
        no_quota=True,
        proc_provider=lambda: [],
    )
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        assert "agent-watch" in app.available_themes
        assert app.theme == "agent-watch"
        theme = app.current_theme
        assert theme.dark is True
        assert theme.primary == ACCENT
        assert theme.background == BG
        assert theme.surface == SURFACE
        assert theme.panel == SURFACE_HI
        assert theme.foreground == TEXT
        assert theme.success == STATUS_COLORS[Status.DONE]
        assert theme.warning == STATUS_COLORS[Status.RUNNING]
        assert theme.error == STATUS_COLORS[Status.FAILED]

        grid = app.screen.query_one(CardGrid)
        assert grid.styles.scrollbar_size_vertical == 1
        assert grid.styles.scrollbar_size_horizontal == 1
        assert grid.styles.scrollbar_color.hex.lower() == BORDER
        assert grid.styles.scrollbar_background.hex.lower() == SURFACE
        assert grid.styles.scrollbar_color_hover.hex.lower() == ACCENT
        assert grid.styles.scrollbar_color_active.hex.lower() == ACCENT


def _bare_app(tmp_path: Path) -> AgentWatchApp:
    return AgentWatchApp(
        claude_home=tmp_path / "claude",
        agy_home=tmp_path / "agy",
        codex_home=tmp_path / "codex",
        no_quota=True,
        proc_provider=lambda: [],
    )


def test_transcript_dir_codex_uses_rollout_folder(tmp_path: Path) -> None:
    rid = "00000000-0000-0000-0000-00000000c777"
    day = tmp_path / "codex" / "sessions" / "2026" / "01" / "02"
    day.mkdir(parents=True)
    (day / f"rollout-2026-01-02T03-04-05-{rid}.jsonl").write_text("{}\n", encoding="utf-8")
    app = _bare_app(tmp_path)
    assert app.transcript_dir(f"codex:{rid}") == day
    assert app.transcript_dir("codex:missing") is None


def test_transcript_dir_main_agent_uses_session_log_folder(tmp_path: Path) -> None:
    claude_home = build_home(tmp_path)
    app = AgentWatchApp(
        claude_home=claude_home,
        agy_home=tmp_path / "agy",
        codex_home=tmp_path / "codex",
        no_quota=True,
        proc_provider=lambda: [],
    )
    expected = next((claude_home / "projects").glob(f"*/{SID}.jsonl")).parent
    assert app.transcript_dir(main_agent_key(SID)) == expected


def test_transcript_dir_main_agent_falls_back_to_session_cwd(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    app = _bare_app(tmp_path)
    app.store.upsert_session("sess-x", name="x", cwd=str(work))
    app.store.apply([Event(
        ts=datetime.now(timezone.utc), session_key="sess-x", agent_key=main_agent_key("sess-x"),
        kind=EventKind.START,
    )])
    assert app.transcript_dir(main_agent_key("sess-x")) == work


async def test_open_key_on_codex_card_opens_rollout_folder(tmp_path: Path, monkeypatch) -> None:
    rid = "00000000-0000-0000-0000-00000000c888"
    now = datetime.now(timezone.utc)
    iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    day = tmp_path / "codex" / "sessions" / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}"
    day.mkdir(parents=True)
    records = [
        {"timestamp": iso, "type": "session_meta", "payload": {"id": rid, "timestamp": iso, "cwd": "/work"}},
        {"timestamp": iso, "type": "event_msg", "payload": {"type": "user_message", "message": "<prompt>"}},
    ]
    (day / f"rollout-{now:%Y-%m-%dT%H-%M-%S}-{rid}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    opened: list[Path] = []
    monkeypatch.setattr("agent_watch.app._open_folder", opened.append)
    app = AgentWatchApp(
        claude_home=tmp_path / "claude",
        agy_home=tmp_path / "agy",
        codex_home=tmp_path / "codex",
        no_quota=True,
        window_hours=24,
        proc_provider=lambda: [_FakeProc(cmdline=["codex.exe", "exec", "<prompt>"])],
    )
    async with app.run_test(size=(160, 50)) as pilot:
        assert await wait_until(pilot, lambda: f"codex:{rid}" in app.screen.query_one(CardGrid).cards, 3.0)
        await pilot.press("o")
        await pilot.pause()
        assert opened == [day]
