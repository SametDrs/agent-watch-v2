"""Quota discovery via the agy CLI."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
import json
import math
import time

from agent_watch import config
from agent_watch.model import utc
from agent_watch.runner import run_oneshot

Runner = Callable[[list[str]], Awaitable[tuple[int, str, str]]]

USAGE_ARGS: list[str] = [
    "-p",
    "/usage",
    "--output-format",
    "json",
    "--print-timeout",
    "30s",
]

POOLS: dict[str, tuple[str | None, str | None]] = {
    "gemini": ("gemini-5h", "gemini-weekly"),
    "3p": ("3p-5h", "3p-weekly"),
    "argon": (None, "gemini-argon-wtu-weekly"),
}

_SUMMARY_ITEMS: tuple[tuple[str, str], ...] = (
    ("Gemini 5h", "gemini-5h"),
    ("Gemini 7d", "gemini-weekly"),
    ("Claude/GPT 5h", "3p-5h"),
    ("Claude/GPT 7d", "3p-weekly"),
    ("Argon 7d", "gemini-argon-wtu-weekly"),
)


@dataclass
class QuotaBucket:
    """Remaining fraction and reset time for a single agy quota bucket."""

    id: str
    group: str
    window: str
    remaining: float
    reset: datetime | None


def _extract_json_object(text: str) -> dict[str, object] | None:
    """Decode the usage JSON object from text, skipping any leading noise lines."""
    if not text:
        return None
    decoder = json.JSONDecoder()
    idx = text.find("{")
    attempts = 0
    while idx != -1 and attempts < 64:
        attempts += 1
        try:
            obj, end = decoder.raw_decode(text, idx)
            if isinstance(obj, dict) and "command" in obj:
                return obj
            idx = text.find("{", end)
            continue
        except json.JSONDecodeError:
            pass
        idx = text.find("{", idx + 1)
    return None


def _parse_reset(raw: object) -> datetime | None:
    """Parse an ISO-8601 reset timestamp into a UTC datetime, or None."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return utc(raw)
    except ValueError:
        return None


def _parse_bucket(item: object, group_name: str) -> QuotaBucket | None:
    """Parse a single quota bucket mapping, returning None when malformed."""
    if not isinstance(item, dict):
        return None
    bucket_id = item.get("id")
    if not isinstance(bucket_id, str) or not bucket_id:
        return None
    raw_remaining = item.get("remaining_fraction")
    if isinstance(raw_remaining, bool) or not isinstance(raw_remaining, (int, float)):
        return None
    remaining = float(raw_remaining)
    if not math.isfinite(remaining):
        return None
    remaining = max(0.0, min(1.0, remaining))
    raw_window = item.get("window")
    window = raw_window if isinstance(raw_window, str) else ""
    reset = _parse_reset(item.get("reset_time"))
    return QuotaBucket(
        id=bucket_id,
        group=group_name,
        window=window,
        remaining=remaining,
        reset=reset,
    )


def parse_usage(text: str) -> list[QuotaBucket]:
    """Parse `agy -p /usage --output-format json` output into QuotaBucket items."""
    if not isinstance(text, str):
        return []
    data = _extract_json_object(text)
    if data is None:
        return []
    command = data.get("command")
    if not isinstance(command, dict):
        return []
    cmd_data = command.get("data")
    if not isinstance(cmd_data, dict):
        return []
    groups = cmd_data.get("groups")
    if not isinstance(groups, list):
        return []

    buckets: list[QuotaBucket] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        raw_group_name = group.get("name")
        group_name = raw_group_name if isinstance(raw_group_name, str) else ""
        raw_buckets = group.get("buckets")
        if not isinstance(raw_buckets, list):
            continue
        for item in raw_buckets:
            bucket = _parse_bucket(item, group_name)
            if bucket is not None:
                buckets.append(bucket)
    return buckets


def pool_for_model(model: str) -> str:
    """Return the quota pool ('argon', '3p', or 'gemini') for a model identifier."""
    lowered = (model or "").lower()
    if "argon" in lowered:
        return "argon"
    if any(tok in lowered for tok in ("claude", "gpt", "opus", "sonnet", "haiku")):
        return "3p"
    return "gemini"


def _bucket_entry(
    by_id: dict[str, QuotaBucket],
    bucket_id: str | None,
) -> tuple[float | None, datetime | None]:
    """Return (remaining, reset) for bucket_id, or (None, None) if missing."""
    if bucket_id is None:
        return (None, None)
    bucket = by_id.get(bucket_id)
    if bucket is None:
        return (None, None)
    return (bucket.remaining, bucket.reset)


def summarize(
    buckets: Iterable[QuotaBucket],
) -> dict[str, tuple[float | None, datetime | None]]:
    """Summarize quota buckets into ordered display labels mapped to (remaining, reset)."""
    by_id = {b.id: b for b in buckets}
    return {
        label: _bucket_entry(by_id, bucket_id)
        for label, bucket_id in _SUMMARY_ITEMS
    }


def pool_pair(
    buckets: Iterable[QuotaBucket],
    model: str,
) -> tuple[tuple[float | None, datetime | None], tuple[float | None, datetime | None]]:
    """Return ((5h_remaining, 5h_reset), (7d_remaining, 7d_reset)) for model's pool."""
    by_id = {b.id: b for b in buckets}
    id_5h, id_7d = POOLS[pool_for_model(model)]
    return (_bucket_entry(by_id, id_5h), _bucket_entry(by_id, id_7d))


class QuotaService:
    """Cached, single-flight fetcher for agy quota buckets."""

    def __init__(
        self,
        runner: Runner | None = None,
        ttl: float = config.QUOTA_CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runner = runner
        self._ttl = ttl
        self._clock = clock
        self._lock = asyncio.Lock()
        self._buckets: list[QuotaBucket] = []
        self._fetched_at: float | None = None
        self._generation = 0

    def _is_fresh(self) -> bool:
        """Return True when cached quota buckets are within TTL."""
        if self._fetched_at is None:
            return False
        return (self._clock() - self._fetched_at) < self._ttl

    def _active_runner(self) -> Runner:
        """Return the configured runner or default run_oneshot."""
        return self._runner if self._runner is not None else run_oneshot

    async def get(self, force: bool = False) -> list[QuotaBucket]:
        """Return cached quota buckets, refreshing when expired or forced."""
        if not force and self._is_fresh():
            return list(self._buckets)

        seen_gen = self._generation
        async with self._lock:
            if self._generation != seen_gen:
                return list(self._buckets)
            if not force and self._is_fresh():
                return list(self._buckets)

            try:
                rc, stdout, _ = await self._active_runner()(list(USAGE_ARGS))
            except Exception:
                self._generation += 1
                return list(self._buckets)

            if rc == 0:
                parsed = parse_usage(stdout)
                if parsed:
                    self._buckets = parsed
                    self._fetched_at = self._clock()
            self._generation += 1
            return list(self._buckets)
