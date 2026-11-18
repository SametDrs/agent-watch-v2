"""Single-instance lock, launch claim, auto-open switch, and window launcher."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

import psutil

from agent_watch import procs

CLAIM_TTL_SECONDS = 15.0
REPLACE_RETRIES = 10
REPLACE_RETRY_DELAY = 0.05
WINDOW_TITLE = "Agent Watch"
_O_BINARY = getattr(os, "O_BINARY", 0)

_claim_tls = threading.local()


def run_dir() -> Path:
    """Return the runtime state directory (created on demand)."""
    if env := os.environ.get("AGENT_WATCH_RUN_DIR"):
        path = Path(env).expanduser()
    elif local := os.environ.get("LOCALAPPDATA"):
        path = Path(local) / "agent-watch"
    elif sys.platform == "win32":
        path = Path.home() / "AppData" / "Local" / "agent-watch"
    else:
        path = Path.home() / ".local" / "share" / "agent-watch"
    path.mkdir(parents=True, exist_ok=True)
    return path


def lock_path() -> Path:
    """Return the informational single-instance lock file path."""
    return run_dir() / "agent-watch.lock"


def instance_lock_path() -> Path:
    """Return the OS-locked single-instance guard file path."""
    return run_dir() / "agent-watch.instance"


def _claim_path() -> Path:
    """Return the launch claim file path."""
    return run_dir() / "launch.claim"


def acquire_instance_lock(path: Path | None = None) -> int | None:
    """Acquire an exclusive non-blocking OS lock for the TUI lifetime, or None if held."""
    target = path if path is not None else instance_lock_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_CREAT | os.O_RDWR | _O_BINARY, 0o600)
    try:
        if sys.platform == "win32":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        return None
    return fd


def acquire_instance_lock_retrying(
    attempts: int = 20, delay: float = 0.05, path: Path | None = None
) -> int | None:
    """Acquire the instance lock, retrying briefly so a launcher probe cannot evict a starting TUI."""
    for attempt in range(attempts):
        handle = acquire_instance_lock(path)
        if handle is not None:
            return handle
        if attempt + 1 < attempts:
            time.sleep(delay)
    return None


def release_instance_lock(handle: int | None) -> None:
    """Release and close an OS lock handle previously acquired by acquire_instance_lock."""
    if handle is None:
        return
    try:
        if sys.platform == "win32":
            os.lseek(handle, 0, os.SEEK_SET)
            msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        try:
            os.close(handle)
        except OSError:
            pass


def _parse_lock(raw: str) -> tuple[int, int] | None:
    """Parse lock JSON into (pid, started_at_ms), or None when malformed."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    pid = data.get("pid")
    if type(pid) is not int or pid <= 0:
        return None
    started = data.get("started_at_ms")
    if type(started) is not int:
        return None
    return pid, started


def _unlink_if_unchanged(target: Path, expected_raw: str) -> None:
    """Remove target only if its contents still match expected_raw."""
    try:
        if target.read_text(encoding="utf-8") == expected_raw:
            target.unlink(missing_ok=True)
    except OSError:
        pass


def _reclaim_stale_json_lock(target: Path) -> bool:
    """Return True if target holds a valid live lock; unlink stale or malformed locks."""
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return False
    parsed = _parse_lock(raw)
    if parsed is not None:
        pid, started_at_ms = parsed
        if procs.pid_alive(pid, started_at_ms):
            return True
    _unlink_if_unchanged(target, raw)
    return False


def instance_running(path: Path | None = None) -> bool:
    """Return True if a live agent-watch instance holds the OS lock (or given lock file)."""
    if path is not None:
        return _reclaim_stale_json_lock(path)
    handle = acquire_instance_lock()
    if handle is None:
        return True
    # Release before any slower work so a starting TUI's acquire rarely meets the probe.
    release_instance_lock(handle)
    _reclaim_stale_json_lock(lock_path())
    return False


def _sweep_stale_lock_tmps(directory: Path) -> None:
    """Remove abandoned .lock.*.tmp files older than CLAIM_TTL_SECONDS."""
    cutoff = time.time() - CLAIM_TTL_SECONDS
    try:
        entries = list(directory.glob(".lock.*.tmp"))
    except OSError:
        return
    for candidate in entries:
        try:
            if candidate.stat().st_mtime < cutoff:
                candidate.unlink(missing_ok=True)
        except OSError:
            pass


def write_lock(path: Path | None = None) -> None:
    """Atomically write the current process lock file."""
    target = path if path is not None else lock_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    _sweep_stale_lock_tmps(target.parent)
    pid = os.getpid()
    try:
        started_at_ms = int(psutil.Process(pid).create_time() * 1000)
    except Exception:  # noqa: BLE001
        started_at_ms = int(time.time() * 1000)
    payload = json.dumps({"pid": pid, "started_at_ms": started_at_ms})
    fd, tmp_name = tempfile.mkstemp(prefix=".lock.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        for attempt in range(REPLACE_RETRIES + 1):
            try:
                os.replace(tmp_name, target)
                break
            except PermissionError:
                if attempt == REPLACE_RETRIES:
                    raise
                time.sleep(REPLACE_RETRY_DELAY)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def release_lock(path: Path | None = None) -> None:
    """Remove the lock file only if it is held by the current process."""
    target = path if path is not None else lock_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return
    parsed = _parse_lock(raw)
    if parsed is None:
        return
    pid, _ = parsed
    if pid == os.getpid():
        _unlink_if_unchanged(target, raw)


def release_claim(path: Path | None = None) -> None:
    """Remove the launch claim file if present."""
    target = path if path is not None else _claim_path()
    for _ in range(10):
        try:
            target.unlink(missing_ok=True)
            return
        except PermissionError:
            # A launcher may briefly hold the claim open while recording its child pid.
            time.sleep(0.05)
        except OSError:
            return


def _create_claim_exclusive(target: Path, claim_time: float) -> str | None:
    """Atomically create target with O_CREAT | O_EXCL and write claim payload."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_BINARY
    try:
        fd = os.open(target, flags, 0o600)
    except (FileExistsError, PermissionError):
        return None
    payload = json.dumps({
        "pid": os.getpid(),
        "claimed_at": claim_time,
        "token": uuid.uuid4().hex,
    })
    try:
        os.write(fd, payload.encode("utf-8"))
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    os.close(fd)
    return payload


def _is_claim_stale(target: Path, claim_time: float) -> bool:
    """Return True if target is missing, or TTL expired and recorded child pid is not alive."""
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None

    claimed_at: float | None = None
    child_pid: int | None = None
    child_started_at_ms: int | None = None
    if isinstance(data, dict):
        raw_claimed = data.get("claimed_at")
        if isinstance(raw_claimed, (int, float)) and not isinstance(raw_claimed, bool):
            val = float(raw_claimed)
            if math.isfinite(val):
                claimed_at = val
        raw_child = data.get("child_pid")
        if type(raw_child) is int and raw_child > 0:
            child_pid = raw_child
        raw_child_started = data.get("child_started_at_ms")
        if type(raw_child_started) is int:
            child_started_at_ms = raw_child_started

    if claimed_at is not None:
        ttl_passed = (claim_time - claimed_at) > CLAIM_TTL_SECONDS
    else:
        # Unparseable or empty claim may belong to a writer that just opened the file;
        # fall back to wall-clock mtime so an active claim is never clobbered mid-write.
        try:
            ttl_passed = (time.time() - target.stat().st_mtime) > CLAIM_TTL_SECONDS
        except FileNotFoundError:
            return True
        except OSError:
            return False

    if not ttl_passed:
        return False
    if child_pid is not None and procs.pid_alive(child_pid, child_started_at_ms):
        return False
    return True


def _is_reclaim_stale(reclaim: Path) -> bool:
    """Return True if reclaim guard is older than CLAIM_TTL_SECONDS and its owner is gone."""
    try:
        age = time.time() - reclaim.stat().st_mtime
    except FileNotFoundError:
        return False
    except OSError:
        return False
    if age <= CLAIM_TTL_SECONDS:
        return False
    try:
        data = json.loads(reclaim.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    if not isinstance(data, dict):
        return True
    pid = data.get("pid")
    started = data.get("started_at_ms")
    if type(pid) is int and pid > 0 and type(started) is int:
        if procs.pid_alive(pid, started):
            return False
    return True


def _try_claim_launch_raw(
    now: float | None = None, path: Path | None = None
) -> tuple[Path, str] | None:
    """Claim a launch slot and return (claim_path, raw_payload), or None when held."""
    claim_time = time.time() if now is None else float(now)
    target = path if path is not None else _claim_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if (payload := _create_claim_exclusive(target, claim_time)) is not None:
        return target, payload
    if not _is_claim_stale(target, claim_time):
        return None

    reclaim = target.with_name(f"{target.name}.reclaim")
    try:
        if _is_reclaim_stale(reclaim):
            reclaim.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        reclaim_fd = os.open(reclaim, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _O_BINARY, 0o600)
    except (FileExistsError, PermissionError):
        return None
    try:
        try:
            owner_started = int(psutil.Process(os.getpid()).create_time() * 1000)
        except Exception:  # noqa: BLE001
            owner_started = None
        reclaim_meta = json.dumps({"pid": os.getpid(), "started_at_ms": owner_started})
        try:
            os.write(reclaim_fd, reclaim_meta.encode("utf-8"))
        finally:
            os.close(reclaim_fd)
        if target.exists() and not _is_claim_stale(target, claim_time):
            return None
        try:
            target.unlink(missing_ok=True)
        except OSError:
            return None
        payload = _create_claim_exclusive(target, claim_time)
        if payload is None:
            return None
        try:
            if target.read_text(encoding="utf-8") != payload:
                return None
        except OSError:
            return None
        return target, payload
    finally:
        try:
            reclaim.unlink(missing_ok=True)
        except OSError:
            pass


def try_claim_launch(now: float | None = None, path: Path | None = None) -> bool:
    """Claim a single launch slot using an atomic exclusive file create."""
    result = _try_claim_launch_raw(now=now, path=path)
    _claim_tls.last = result
    return result is not None


def _record_claim_child(target: Path, expected_raw: str, child_pid: int) -> None:
    """Record the spawned child pid in target if unchanged; never recreate deleted claim."""
    try:
        child_started_at_ms = int(psutil.Process(child_pid).create_time() * 1000)
    except Exception:  # noqa: BLE001
        return
    try:
        fd = os.open(target, os.O_RDWR | _O_BINARY)
    except OSError:
        return
    try:
        chunks: list[bytes] = []
        while chunk := os.read(fd, 4096):
            chunks.append(chunk)
        current = b"".join(chunks).decode("utf-8", errors="replace")
        if current != expected_raw:
            return
        try:
            data = json.loads(current)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return
        data["child_pid"] = child_pid
        data["child_started_at_ms"] = child_started_at_ms
        encoded = json.dumps(data).encode("utf-8")
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, encoded)
        os.ftruncate(fd, len(encoded))
    except OSError:
        return
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def auto_open_enabled() -> bool:
    """Return True unless AGENT_WATCH_AUTO_OPEN=0 or config.json disables auto_open."""
    if os.environ.get("AGENT_WATCH_AUTO_OPEN") == "0":
        return False
    cfg_path = run_dir() / "config.json"
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return True
    if not isinstance(data, dict):
        return True
    val = data.get("auto_open", True)
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return val != 0
    if isinstance(val, str):
        return val.strip().lower() not in ("0", "false", "no", "off")
    return True


def open_new_window() -> int | None:
    """Open the Agent Watch TUI in a detached PowerShell console window."""
    exe = sys.executable.replace("'", "''")
    ps_cmd = f"$Host.UI.RawUI.WindowTitle = '{WINDOW_TITLE}'; & '{exe}' -m agent_watch"
    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    proc = subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
        creationflags=creationflags,
    )
    pid = getattr(proc, "pid", None)
    return pid if type(pid) is int and pid > 0 else None


def launch(auto: bool = False) -> int:
    """Run the single-instance launcher flow and print exactly one status line."""
    claim_info: tuple[Path, str] | None = None
    try:
        if auto and not auto_open_enabled():
            print("auto-open off")
            return 0
        if instance_running():
            print("Agent Watch already open")
            return 0
        _claim_tls.last = None
        if not try_claim_launch():
            print("Agent Watch already open")
            return 0
        claim_info = getattr(_claim_tls, "last", None)
        _claim_tls.last = None
        spawned = open_new_window()
        child_pid: int | None = None
        if type(spawned) is int and spawned > 0:
            child_pid = spawned
        elif hasattr(spawned, "pid"):
            maybe_pid = getattr(spawned, "pid", None)
            if type(maybe_pid) is int and maybe_pid > 0:
                child_pid = maybe_pid
        if claim_info is not None and child_pid is not None:
            _record_claim_child(claim_info[0], claim_info[1], child_pid)
    except Exception as exc:  # noqa: BLE001
        if claim_info is not None:
            _unlink_if_unchanged(claim_info[0], claim_info[1])
        msg = " ".join(str(exc).splitlines()) or type(exc).__name__
        print(f"Agent Watch failed to open: {msg}")
        return 1
    print("Agent Watch opened")
    return 0
