"""Tests for quota parsing, pool summarization, and QuotaService."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest

from agent_watch import config
from agent_watch.quota import (
    POOLS,
    QuotaBucket,
    QuotaService,
    parse_usage,
    pool_for_model,
    pool_pair,
    summarize,
)
from agent_watch.runner import run_oneshot


class FakeClock:
    """Deterministic monotonic clock for cache TTL tests."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_parse_usage_fixture(fixtures_dir: Path) -> None:
    text = (fixtures_dir / "agy" / "usage.json").read_text(encoding="utf-8")
    buckets = parse_usage(text)

    assert len(buckets) == 5
    for bucket in buckets:
        assert isinstance(bucket, QuotaBucket)
        assert 0.0 <= bucket.remaining <= 1.0
        assert bucket.reset is not None
        assert bucket.reset.tzinfo is not None

    by_id = {b.id: b for b in buckets}
    assert set(by_id) == {
        "gemini-weekly",
        "gemini-5h",
        "3p-weekly",
        "3p-5h",
        "gemini-argon-wtu-weekly",
    }
    assert by_id["gemini-5h"].group == "Gemini Models"
    assert by_id["gemini-5h"].window == "5h"
    assert by_id["gemini-5h"].remaining == pytest.approx(0.7592098712921143)
    assert by_id["gemini-5h"].reset == datetime(2026, 11, 17, 19, 4, 24, tzinfo=timezone.utc)


def test_parse_usage_error_and_malformed(fixtures_dir: Path) -> None:
    error_text = (fixtures_dir / "agy" / "usage_error.txt").read_text(encoding="utf-8")
    assert parse_usage(error_text) == []
    assert parse_usage("") == []
    assert parse_usage("not json at all") == []
    assert parse_usage('{"status": "SUCCESS"}') == []
    assert parse_usage('{"command": {"data": {"groups": "bad"}}}') == []


def test_parse_usage_with_noise_prefix(fixtures_dir: Path) -> None:
    text = (fixtures_dir / "agy" / "usage.json").read_text(encoding="utf-8")
    noisy = (
        "Warning: checking auth token\n"
        "[debug] {not valid json}\n"
        '{"log": "starting"}\n'
        + text
        + "\ntrailing log line\n"
    )
    buckets = parse_usage(noisy)
    assert len(buckets) == 5


def test_parse_usage_skips_malformed_buckets() -> None:
    payload = (
        '{"command":{"data":{"groups":[{"name":"G","buckets":['
        '{"id":"bad1","remaining_fraction":"nope"},'
        '{"id":"bad2","remaining_fraction":true},'
        '{"remaining_fraction":0.5},'
        '{"id":"ok","window":"5h","remaining_fraction":0.42,"reset_time":"invalid-ts"}'
        "]}]}}}"
    )
    buckets = parse_usage(payload)
    assert len(buckets) == 1
    assert buckets[0].id == "ok"
    assert buckets[0].remaining == pytest.approx(0.42)
    assert buckets[0].reset is None


def test_summarize_and_pool_pair(fixtures_dir: Path) -> None:
    text = (fixtures_dir / "agy" / "usage.json").read_text(encoding="utf-8")
    buckets = parse_usage(text)

    summary = summarize(buckets)
    assert list(summary.keys()) == [
        "Gemini 5h",
        "Gemini 7d",
        "Claude/GPT 5h",
        "Claude/GPT 7d",
        "Argon 7d",
    ]
    assert summary["Gemini 5h"][0] == pytest.approx(0.7592098712921143)
    assert summary["Gemini 7d"][0] == pytest.approx(0.673369288444519)
    assert summary["Claude/GPT 5h"][0] == pytest.approx(1.0)
    assert summary["Claude/GPT 7d"][0] == pytest.approx(1.0)
    assert summary["Argon 7d"][0] == pytest.approx(0.8692445158958435)

    empty_summary = summarize([])
    assert list(empty_summary.keys()) == list(summary.keys())
    assert all(val == (None, None) for val in empty_summary.values())

    assert pool_for_model("gemini-4-hydrogen-high") == "gemini"
    assert pool_for_model("claude-opus-5-5-high") == "3p"
    assert pool_for_model("gpt-oss-120b-medium") == "3p"
    assert pool_for_model("gemini-4-argon-high") == "argon"
    assert set(POOLS) == {"gemini", "3p", "argon"}

    h_5h, h_7d = pool_pair(buckets, "gemini-4-hydrogen-high")
    assert h_5h == summary["Gemini 5h"]
    assert h_7d == summary["Gemini 7d"]

    c_5h, c_7d = pool_pair(buckets, "claude-sonnet-5-5-high")
    assert c_5h == summary["Claude/GPT 5h"]
    assert c_7d == summary["Claude/GPT 7d"]

    a_5h, a_7d = pool_pair(buckets, "gemini-4-argon-high")
    assert a_5h == (None, None)
    assert a_7d == summary["Argon 7d"]


async def test_quota_service_caching_and_force(fixtures_dir: Path) -> None:
    usage_text = (fixtures_dir / "agy" / "usage.json").read_text(encoding="utf-8")
    clock = FakeClock(0.0)
    calls: list[list[str]] = []

    async def fake_runner(args: list[str]) -> tuple[int, str, str]:
        calls.append(args)
        return (0, usage_text, "")

    svc = QuotaService(runner=fake_runner, ttl=60.0, clock=clock)

    first = await svc.get()
    assert len(first) == 5
    assert calls == [["-p", "/usage", "--output-format", "json", "--print-timeout", "30s"]]

    clock.now = 59.0
    second = await svc.get()
    assert len(second) == 5
    assert len(calls) == 1

    clock.now = 60.0
    third = await svc.get()
    assert len(third) == 5
    assert len(calls) == 2

    clock.now = 61.0
    forced = await svc.get(force=True)
    assert len(forced) == 5
    assert len(calls) == 3


async def test_quota_service_single_flight(fixtures_dir: Path) -> None:
    usage_text = (fixtures_dir / "agy" / "usage.json").read_text(encoding="utf-8")
    gate = asyncio.Event()
    started = asyncio.Event()
    call_count = 0

    async def slow_runner(args: list[str]) -> tuple[int, str, str]:
        nonlocal call_count
        call_count += 1
        started.set()
        await gate.wait()
        return (0, usage_text, "")

    svc = QuotaService(runner=slow_runner, ttl=60.0, clock=FakeClock(10.0))

    tasks = [asyncio.create_task(svc.get()) for _ in range(5)]
    await started.wait()
    gate.set()
    results = await asyncio.gather(*tasks)

    assert call_count == 1
    assert all(len(res) == 5 for res in results)


async def test_quota_service_error_fallback(fixtures_dir: Path) -> None:
    usage_text = (fixtures_dir / "agy" / "usage.json").read_text(encoding="utf-8")
    error_text = (fixtures_dir / "agy" / "usage_error.txt").read_text(encoding="utf-8")
    clock = FakeClock(0.0)
    mode = "fail_rc"

    async def fake_runner(args: list[str]) -> tuple[int, str, str]:
        if mode == "fail_rc":
            return (1, "", error_text)
        if mode == "bad_json":
            return (0, error_text, "")
        if mode == "raise":
            raise RuntimeError("boom")
        return (0, usage_text, "")

    svc = QuotaService(runner=fake_runner, ttl=60.0, clock=clock)

    assert await svc.get() == []

    mode = "ok"
    good = await svc.get()
    assert len(good) == 5

    clock.now = 100.0
    mode = "fail_rc"
    assert await svc.get() == good

    mode = "bad_json"
    assert await svc.get() == good

    mode = "raise"
    assert await svc.get() == good


async def test_run_oneshot_missing_binary_and_python_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "agy_bin", lambda: "nonexistent-agy-binary-xyz-999")
    rc, stdout, stderr = await run_oneshot(["--version"])
    assert rc == -1
    assert stdout == ""
    assert stderr

    monkeypatch.setattr(config, "agy_bin", lambda: sys.executable)
    rc, stdout, stderr = await run_oneshot(
        ["-c", "import sys; print('ok'); print('warn', file=sys.stderr)"]
    )
    assert rc == 0
    assert stdout.strip() == "ok"
    assert stderr.strip() == "warn"

    rc, stdout, stderr = await run_oneshot(
        ["-c", "import time; time.sleep(10)"],
        timeout=0.2,
    )
    assert (rc, stdout, stderr) == (-1, "", "timeout")
