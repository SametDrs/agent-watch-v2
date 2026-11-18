"""Tests for process helpers."""

import os

import psutil

from agent_watch.procs import agy_processes, pid_alive


def test_pid_alive_self() -> None:
    assert pid_alive(os.getpid())


def test_pid_alive_bogus() -> None:
    assert not pid_alive(2**22 + 12345)


def test_pid_alive_started_at_guard() -> None:
    ct_ms = int(psutil.Process().create_time() * 1000)
    assert pid_alive(os.getpid(), ct_ms)
    assert not pid_alive(os.getpid(), ct_ms - 60_000)


def test_agy_processes_returns_list() -> None:
    assert isinstance(agy_processes(), list)
