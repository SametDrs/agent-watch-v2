"""Tests for the single-instance launcher, lock, claim, auto-open, and CLI."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psutil
import pytest

from agent_watch import launcher
from agent_watch.app import AgentWatchApp, build_parser, main


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Keep all launcher state inside tmp_path and forbid real subprocess spawns."""
    run = tmp_path / "run"
    monkeypatch.setenv("AGENT_WATCH_RUN_DIR", str(run))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.delenv("AGENT_WATCH_AUTO_OPEN", raising=False)

    def _forbid_popen(*args: object, **kwargs: object) -> object:
        raise AssertionError("unexpected real subprocess.Popen call in launcher test")

    monkeypatch.setattr(subprocess, "Popen", _forbid_popen)
    return run


def test_run_dir_env_override_and_localappdata_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    override = tmp_path / "custom-run"
    monkeypatch.setenv("AGENT_WATCH_RUN_DIR", str(override))
    assert launcher.run_dir() == override
    assert override.is_dir()

    monkeypatch.delenv("AGENT_WATCH_RUN_DIR")
    local_app = tmp_path / "fake-localappdata"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app))
    expected = local_app / "agent-watch"
    assert launcher.run_dir() == expected
    assert expected.is_dir()


def test_write_lock_instance_running_and_release() -> None:
    assert not launcher.instance_running()
    launcher.write_lock()
    lock = launcher.lock_path()
    assert lock.exists()
    data = json.loads(lock.read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()
    assert isinstance(data["started_at_ms"], int)
    # Informational JSON lock alone does not claim instance ownership (prevents PID-reuse false positives).
    assert not launcher.instance_running()

    handle = launcher.acquire_instance_lock()
    assert handle is not None
    try:
        assert launcher.instance_running()
        assert launcher.acquire_instance_lock() is None
    finally:
        launcher.release_lock()
        assert not lock.exists()
        launcher.release_instance_lock(handle)
    assert not launcher.instance_running()


def test_stale_lock_reclaimed_on_dead_pid_mismatch_and_corrupt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lock = launcher.lock_path()
    lock.write_text(json.dumps({"pid": 2**22 + 99999, "started_at_ms": 12345}), encoding="utf-8")
    assert not launcher.instance_running()
    assert not lock.exists()

    ct_ms = int(psutil.Process().create_time() * 1000)
    lock.write_text(json.dumps({"pid": os.getpid(), "started_at_ms": ct_ms - 60_000}), encoding="utf-8")
    assert not launcher.instance_running()
    assert not lock.exists()

    lock.write_text("{not-valid-json", encoding="utf-8")
    assert not launcher.instance_running()
    assert not lock.exists()

    opened: list[bool] = []
    monkeypatch.setattr(launcher, "open_new_window", lambda: opened.append(True))
    lock.write_text(json.dumps({"pid": 2**22 + 99999, "started_at_ms": 1}), encoding="utf-8")
    assert main(["--new-window"]) == 0
    assert opened == [True]
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines == ["Agent Watch opened"]


def test_release_lock_keeps_foreign_and_corrupt_locks() -> None:
    lock = launcher.lock_path()
    foreign = json.dumps({"pid": os.getpid() + 1000, "started_at_ms": 999})
    lock.write_text(foreign, encoding="utf-8")
    launcher.release_lock()
    assert lock.exists()
    assert lock.read_text(encoding="utf-8") == foreign

    lock.write_text("corrupt", encoding="utf-8")
    launcher.release_lock()
    assert lock.exists()


def test_second_launch_while_running_opens_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[bool] = []
    monkeypatch.setattr(launcher, "open_new_window", lambda: opened.append(True))
    handle = launcher.acquire_instance_lock()
    assert handle is not None
    try:
        launcher.write_lock()
        assert main(["--new-window"]) == 0
        assert opened == []
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines == ["Agent Watch already open"]
    finally:
        launcher.release_lock()
        launcher.release_instance_lock(handle)


def test_two_simultaneous_launchers_open_one_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_700_000_000.0
    assert [launcher.try_claim_launch(now), launcher.try_claim_launch(now)] == [True, False]

    # Reset claim and race four concurrent CLI launches across a barrier.
    (launcher.run_dir() / "launch.claim").unlink()
    opened: list[int] = []
    guard = threading.Lock()
    barrier = threading.Barrier(4)
    buffers: dict[int, io.StringIO] = {}
    orig_stdout = sys.stdout

    class _ThreadStdout:
        def write(self, text: str) -> int:
            buf = buffers.get(threading.get_ident())
            if buf is None:
                return orig_stdout.write(text)
            return buf.write(text)

        def flush(self) -> None:
            pass

    def _record_open() -> None:
        with guard:
            opened.append(1)

    monkeypatch.setattr(sys, "stdout", _ThreadStdout())
    monkeypatch.setattr(launcher, "open_new_window", _record_open)

    def _worker() -> tuple[int, int]:
        tid = threading.get_ident()
        buffers[tid] = io.StringIO()
        barrier.wait()
        return tid, main(["--new-window"])

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: _worker(), range(4)))

    assert [rc for _, rc in results] == [0, 0, 0, 0]
    assert len(opened) == 1
    lines: list[str] = []
    for tid, _ in results:
        thread_lines = buffers[tid].getvalue().strip().splitlines()
        assert len(thread_lines) == 1
        lines.extend(thread_lines)
    assert lines.count("Agent Watch opened") == 1
    assert lines.count("Agent Watch already open") == 3


def test_try_claim_launch_defaults_now_and_stores_claim_time() -> None:
    before = time.time()
    assert launcher.try_claim_launch() is True
    after = time.time()
    claim = launcher.run_dir() / "launch.claim"
    assert claim.exists()
    data = json.loads(claim.read_text(encoding="utf-8"))
    assert before <= data["claimed_at"] <= after
    assert launcher.try_claim_launch() is False


def test_stale_claim_replaced() -> None:
    assert launcher.try_claim_launch(0) is True
    assert launcher.try_claim_launch(15) is False
    assert launcher.try_claim_launch(16) is True


def test_auto_off(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[bool] = []
    monkeypatch.setattr(launcher, "open_new_window", lambda: opened.append(True))

    monkeypatch.setenv("AGENT_WATCH_AUTO_OPEN", "0")
    assert launcher.auto_open_enabled() is False
    assert main(["--new-window", "--auto"]) == 0
    assert opened == []
    assert capsys.readouterr().out.strip().splitlines() == ["auto-open off"]

    # Manual launch without --auto ignores auto-open switch.
    assert main(["--new-window"]) == 0
    assert opened == [True]
    assert capsys.readouterr().out.strip().splitlines() == ["Agent Watch opened"]


def test_auto_open_config_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[bool] = []
    monkeypatch.setattr(launcher, "open_new_window", lambda: opened.append(True))

    assert launcher.auto_open_enabled() is True
    cfg = launcher.run_dir() / "config.json"
    cfg.write_text(json.dumps({"auto_open": False}), encoding="utf-8")
    assert launcher.auto_open_enabled() is False
    assert main(["--new-window", "--auto"]) == 0
    assert opened == []
    assert capsys.readouterr().out.strip().splitlines() == ["auto-open off"]

    cfg.write_text("{bad-json", encoding="utf-8")
    assert launcher.auto_open_enabled() is True


def test_open_new_window_spawns_powershell_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    class FakePopen:
        def __init__(self, cmd: list[str], **kwargs: object) -> None:
            calls.append((cmd, kwargs))

        def wait(self, *args: object, **kwargs: object) -> int:
            raise AssertionError("open_new_window must return immediately without wait()")

        def communicate(self, *args: object, **kwargs: object) -> tuple[str, str]:
            raise AssertionError("open_new_window must not call communicate()")

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    launcher.open_new_window()

    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd[:3] == ["powershell.exe", "-NoProfile", "-Command"]
    exe = sys.executable.replace("'", "''")
    assert f"& '{exe}' -m agent_watch" in cmd[3]
    assert "Agent Watch" in cmd[3]
    assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NEW_CONSOLE", 0)


def test_launch_error_prints_single_line_and_releases_claim(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom() -> None:
        raise RuntimeError("spawn failed\nwith details")

    monkeypatch.setattr(launcher, "open_new_window", _boom)
    assert main(["--new-window"]) == 1
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("Agent Watch failed to open:")
    assert not (launcher.run_dir() / "launch.claim").exists()

    opened: list[bool] = []
    monkeypatch.setattr(launcher, "open_new_window", lambda: opened.append(True))
    assert main(["--new-window"]) == 0
    assert opened == [True]
    assert capsys.readouterr().out.strip().splitlines() == ["Agent Watch opened"]


def test_cli_flags_and_plain_main_runs_tui(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    args = build_parser().parse_args(["--new-window", "--auto"])
    assert args.new_window is True and args.auto is True
    plain = build_parser().parse_args([])
    assert plain.new_window is False and plain.auto is False

    ran: list[bool] = []
    opened: list[bool] = []
    monkeypatch.setattr(AgentWatchApp, "run", lambda self: ran.append(True))
    monkeypatch.setattr(launcher, "open_new_window", lambda: opened.append(True))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("AGY_HOME", str(tmp_path / "a"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "x"))

    assert main(["--no-quota"]) == 0
    assert ran == [True]
    assert opened == []


def test_main_module_exports_main() -> None:
    import agent_watch.__main__ as entry

    assert entry.main is main


async def test_tui_writes_and_removes_lock_on_mount_unmount(tmp_path: Path) -> None:
    app = AgentWatchApp(
        claude_home=tmp_path / "claude",
        agy_home=tmp_path / "agy",
        codex_home=tmp_path / "codex",
        no_quota=True,
        proc_provider=lambda: [],
    )
    lock = launcher.lock_path()
    assert not lock.exists()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert lock.exists()
        assert launcher.instance_running()
        data = json.loads(lock.read_text(encoding="utf-8"))
        assert data["pid"] == os.getpid()
    assert not lock.exists()
    assert not launcher.instance_running()


async def test_second_tui_exits_with_already_open_when_instance_lock_held(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    handle = launcher.acquire_instance_lock()
    assert handle is not None
    launcher.write_lock()
    try:
        app = AgentWatchApp(
            claude_home=tmp_path / "claude",
            agy_home=tmp_path / "agy",
            codex_home=tmp_path / "codex",
            no_quota=True,
            proc_provider=lambda: [],
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert app._exit is True
            assert app._instance_lock is None
            assert app.screen is not app.dashboard
        captured = capsys.readouterr()
        assert captured.err.strip().splitlines() == ["Agent Watch already open"]
        assert launcher.lock_path().exists()
        assert launcher.instance_running()
    finally:
        launcher.release_lock()
        launcher.release_instance_lock(handle)


async def test_claim_deleted_after_mount_and_reopen_after_close_launches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[bool] = []
    monkeypatch.setattr(launcher, "open_new_window", lambda: opened.append(True))

    assert main(["--new-window"]) == 0
    assert opened == [True]
    assert capsys.readouterr().out.strip().splitlines() == ["Agent Watch opened"]
    claim = launcher.run_dir() / "launch.claim"
    assert claim.exists()

    app = AgentWatchApp(
        claude_home=tmp_path / "claude",
        agy_home=tmp_path / "agy",
        codex_home=tmp_path / "codex",
        no_quota=True,
        proc_provider=lambda: [],
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert not claim.exists()
        assert launcher.lock_path().exists()
        assert launcher.instance_running()

        # Textual captures print() while the app runs, so capture this call's stdout directly.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert main(["--new-window"]) == 0
        assert len(opened) == 1
        assert buf.getvalue().strip().splitlines() == ["Agent Watch already open"]

    assert not launcher.lock_path().exists()
    assert not launcher.instance_running()
    assert main(["--new-window"]) == 0
    assert len(opened) == 2
    assert capsys.readouterr().out.strip().splitlines() == ["Agent Watch opened"]


def test_permission_error_on_claim_create_returns_false(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real_open = os.open

    def _deny_claim_open(path: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        if Path(path).name == "launch.claim" and (flags & os.O_EXCL):
            raise PermissionError("delete-pending")
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", _deny_claim_open)
    assert launcher.try_claim_launch() is False
    assert main(["--new-window"]) == 0
    assert capsys.readouterr().out.strip().splitlines() == ["Agent Watch already open"]


def test_launch_with_unwritable_run_dir_prints_single_line_and_returns_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    occupied = tmp_path / "occupied_run_dir"
    occupied.write_text("file", encoding="utf-8")
    monkeypatch.setenv("AGENT_WATCH_RUN_DIR", str(occupied))

    assert launcher.launch() == 1
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("Agent Watch failed to open:")

    def _boom_dir() -> Path:
        raise PermissionError("access denied\nmultiline")

    monkeypatch.setattr(launcher, "run_dir", _boom_dir)
    assert launcher.launch(auto=True) == 1
    assert capsys.readouterr().out.strip().splitlines() == [
        "Agent Watch failed to open: access denied multiline"
    ]


def test_started_at_ms_none_missing_or_non_int_is_stale(tmp_path: Path) -> None:
    bad_payloads = [
        {"pid": os.getpid(), "started_at_ms": None},
        {"pid": os.getpid()},
        {"pid": os.getpid(), "started_at_ms": "12345"},
        {"pid": os.getpid(), "started_at_ms": True},
    ]
    for idx, payload in enumerate(bad_payloads):
        raw = json.dumps(payload)
        assert launcher._parse_lock(raw) is None

        lock = launcher.lock_path()
        lock.write_text(raw, encoding="utf-8")
        assert not launcher.instance_running()
        assert not lock.exists()

        custom = tmp_path / f"custom-{idx}.lock"
        custom.write_text(raw, encoding="utf-8")
        assert not launcher.instance_running(custom)
        assert not custom.exists()


def test_claim_child_pid_keeps_claim_alive_past_ttl_when_running(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(launcher, "open_new_window", lambda: os.getpid())
    t0 = time.time()
    assert launcher.launch() == 0
    assert capsys.readouterr().out.strip().splitlines() == ["Agent Watch opened"]

    claim = launcher.run_dir() / "launch.claim"
    data = json.loads(claim.read_text(encoding="utf-8"))
    assert data["child_pid"] == os.getpid()
    assert type(data["child_started_at_ms"]) is int
    assert launcher.try_claim_launch(now=t0 + 20.0) is False

    claim.unlink()
    monkeypatch.setattr(launcher, "open_new_window", lambda: 2**22 + 99999)
    assert launcher.launch() == 0
    assert capsys.readouterr().out.strip().splitlines() == ["Agent Watch opened"]
    dead_data = json.loads(claim.read_text(encoding="utf-8"))
    assert "child_pid" not in dead_data
    assert "child_started_at_ms" not in dead_data
    assert launcher.try_claim_launch(now=time.time() + 20.0) is True


def test_claim_does_not_record_child_pid_when_create_time_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(launcher, "open_new_window", lambda: os.getpid())

    def _fail_create_time(self: psutil.Process) -> float:
        raise psutil.AccessDenied(self.pid)

    with monkeypatch.context() as m:
        m.setattr(psutil.Process, "create_time", _fail_create_time)
        assert launcher.launch() == 0
    assert capsys.readouterr().out.strip().splitlines() == ["Agent Watch opened"]

    claim = launcher.run_dir() / "launch.claim"
    data = json.loads(claim.read_text(encoding="utf-8"))
    assert "child_pid" not in data
    assert "child_started_at_ms" not in data
    assert launcher.try_claim_launch(now=time.time() + 20.0) is True


def test_write_lock_retries_permission_error_and_sweeps_stale_tmps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = launcher.run_dir()
    stale_tmp = run / ".lock.stale.tmp"
    fresh_tmp = run / ".lock.fresh.tmp"
    stale_tmp.write_text("old", encoding="utf-8")
    fresh_tmp.write_text("new", encoding="utf-8")
    old_ts = time.time() - 60.0
    os.utime(stale_tmp, (old_ts, old_ts))

    real_replace = os.replace
    attempts = 0
    sleeps: list[float] = []

    def _flaky_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise PermissionError("sharing violation")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _flaky_replace)
    monkeypatch.setattr(launcher.time, "sleep", lambda secs: sleeps.append(secs))

    launcher.write_lock()
    assert attempts == 3
    assert sleeps == [launcher.REPLACE_RETRY_DELAY, launcher.REPLACE_RETRY_DELAY]
    assert launcher.lock_path().exists()
    assert not stale_tmp.exists()
    assert fresh_tmp.exists()


async def test_app_mount_logs_write_lock_failure_and_deletes_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    assert launcher.try_claim_launch() is True
    claim = launcher.run_dir() / "launch.claim"
    assert claim.exists()

    def _fail_write_lock(*args: object, **kwargs: object) -> None:
        raise PermissionError("locked by reader")

    monkeypatch.setattr(launcher, "write_lock", _fail_write_lock)

    app = AgentWatchApp(
        claude_home=tmp_path / "claude",
        agy_home=tmp_path / "agy",
        codex_home=tmp_path / "codex",
        no_quota=True,
        proc_provider=lambda: [],
    )
    with caplog.at_level("WARNING"):
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert launcher.instance_running()
            assert not claim.exists()
    assert "Failed to write lock file" in caplog.text


def test_launch_error_keeps_newer_reclaimed_claim(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    claim = launcher.run_dir() / "launch.claim"
    newer_payload = json.dumps({"pid": 999, "claimed_at": 9_999_999_999.0, "token": "newer"})

    def _steal_then_fail() -> None:
        claim.write_text(newer_payload, encoding="utf-8")
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(launcher, "open_new_window", _steal_then_fail)
    assert launcher.launch() == 1
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    assert lines[0] == "Agent Watch failed to open: spawn failed"
    assert claim.exists()
    assert claim.read_text(encoding="utf-8") == newer_payload


def test_acquire_retrying_survives_brief_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    results = [None, None, 7]
    monkeypatch.setattr(launcher, "acquire_instance_lock", lambda path=None: results.pop(0))
    monkeypatch.setattr(launcher.time, "sleep", lambda _s: None)
    assert launcher.acquire_instance_lock_retrying() == 7
    assert results == []


def test_acquire_retrying_gives_up_when_held(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        launcher, "acquire_instance_lock", lambda path=None: calls.append(1)
    )
    monkeypatch.setattr(launcher.time, "sleep", lambda _s: None)
    assert launcher.acquire_instance_lock_retrying(attempts=3) is None
    assert len(calls) == 3


def test_release_claim_retries_on_permission_error(monkeypatch: pytest.MonkeyPatch) -> None:
    claim = launcher.run_dir() / "launch.claim"
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.write_text("{}", encoding="utf-8")
    real_unlink = Path.unlink
    failures = [PermissionError("busy")]

    def flaky_unlink(self: Path, missing_ok: bool = False) -> None:
        if failures:
            raise failures.pop()
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    monkeypatch.setattr(launcher.time, "sleep", lambda _s: None)
    launcher.release_claim()
    assert not claim.exists()
