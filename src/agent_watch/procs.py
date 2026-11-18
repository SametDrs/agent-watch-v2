"""Process helpers built on psutil. Functions never raise."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import psutil


@dataclass
class ProcInfo:
    """Snapshot of a running process."""

    pid: int
    create_time: float
    cmdline: list[str] = field(default_factory=list)
    cwd: str | None = None


def pid_alive(pid: int, started_at_ms: int | None = None) -> bool:
    """Return True if pid is running (and, if given, started near started_at_ms)."""
    try:
        proc = psutil.Process(pid)
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return False
        if started_at_ms is not None:
            return abs(proc.create_time() * 1000 - started_at_ms) < 5000
        return True
    except Exception:  # noqa: BLE001 - never raise
        return False


def _safe(fn: Callable[[], Any], default: Any) -> Any:
    """Call fn, returning default on any psutil error."""
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def _iter_matching_processes(
    names: tuple[str, ...],
    node_substring: str | None = None,
) -> list[ProcInfo]:
    """Return running processes matching names or node/node.exe carrying node_substring."""
    found: list[ProcInfo] = []
    for proc in psutil.process_iter(["pid", "name", "create_time"]):
        try:
            name = (proc.info.get("name") or "").lower()
            is_direct = name in names
            is_node = node_substring is not None and name in ("node", "node.exe")
            if not is_direct and not is_node:
                continue
            cmdline = _safe(proc.cmdline, [])
            if not isinstance(cmdline, list):
                cmdline = []
            if is_node and not any(
                node_substring in arg.lower() for arg in cmdline if isinstance(arg, str)
            ):
                continue
            found.append(ProcInfo(
                pid=proc.info["pid"],
                create_time=proc.info.get("create_time") or 0.0,
                cmdline=cmdline,
                cwd=_safe(proc.cwd, None),
            ))
        except Exception:  # noqa: BLE001
            continue
    return found


def agy_processes() -> list[ProcInfo]:
    """Return running processes named agy / agy.exe."""
    return _iter_matching_processes(("agy", "agy.exe"))


def codex_processes() -> list[ProcInfo]:
    """Return running Codex CLI processes (codex/codex.exe or node running codex)."""
    return _iter_matching_processes(("codex", "codex.exe"), node_substring="codex")
