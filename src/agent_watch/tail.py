"""Incremental JSONL file tailer with offset tracking and filtering."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, IO

DEFAULT_SKIP: tuple[bytes, ...] = (
    b'"type":"api-request',
    b'"type":"file-history-snapshot"',
)


class JsonlTail:
    """Incrementally tail a JSONL file without holding file handles open."""

    def __init__(
        self,
        path: Path,
        skip_substrings: tuple[bytes, ...] = DEFAULT_SKIP,
        max_line_bytes: int = 256 * 1024,
        initial_tail_bytes: int | None = None,
    ) -> None:
        self.path: Path = Path(path)
        self.skip_substrings: tuple[bytes, ...] = skip_substrings
        self.max_line_bytes: int = max_line_bytes
        self.initial_tail_bytes: int | None = initial_tail_bytes

        self.offset: int = 0
        self.bad_lines: int = 0
        self.lines_read: int = 0

        self._buffer: bytes = b""
        self._last_stat: tuple[int, int] | None = None
        self._first_read: bool = True
        self._discard_first_line: bool = False

    def changed(self) -> bool:
        """Return True if file (st_mtime_ns, st_size) differs from last seen stat."""
        try:
            st = self.path.stat()
        except OSError:
            return False
        return (st.st_mtime_ns, st.st_size) != self._last_stat

    def read_new(self) -> list[dict[str, Any]]:
        """Read and return newly appended complete JSON object lines."""
        try:
            with self.path.open("rb") as fh:
                st = os.fstat(fh.fileno())
                size = st.st_size

                if self._first_read:
                    self._first_read = False
                    self._apply_initial_tail(fh, size)
                elif size < self.offset:
                    self.offset = 0
                    self._buffer = b""
                    self._discard_first_line = False

                fh.seek(self.offset)
                data = fh.read()
        except OSError:
            return []

        self.offset += len(data)
        self._last_stat = (st.st_mtime_ns, st.st_size)
        return self._consume_bytes(data)

    def _apply_initial_tail(self, fh: IO[bytes], size: int) -> None:
        """Seek near EOF on first read when initial_tail_bytes is exceeded."""
        if (
            self.initial_tail_bytes is None
            or self.initial_tail_bytes < 0
            or size <= self.initial_tail_bytes
        ):
            return
        start = size - self.initial_tail_bytes
        if start > 0:
            fh.seek(start - 1)
            self._discard_first_line = fh.read(1) != b"\n"
        self.offset = start

    def _consume_bytes(self, data: bytes) -> list[dict[str, Any]]:
        """Append new bytes to internal buffer and parse complete lines."""
        chunk = self._buffer + data
        if self._discard_first_line:
            if b"\n" not in chunk:
                self._buffer = b""
                return []
            _, chunk = chunk.split(b"\n", 1)
            self._discard_first_line = False

        if not chunk:
            self._buffer = b""
            return []

        parts = chunk.split(b"\n")
        self._buffer = parts.pop()

        records: list[dict[str, Any]] = []
        for raw_line in parts:
            parsed = self._parse_line(raw_line)
            if parsed is not None:
                records.append(parsed)
        return records

    def _parse_line(self, raw_line: bytes) -> dict[str, Any] | None:
        """Filter and JSON-decode one complete raw line into a dict."""
        line = raw_line.rstrip(b"\r")
        if not line:
            return None
        self.lines_read += 1
        if self.skip_substrings and any(sub in line for sub in self.skip_substrings):
            return None
        if len(line) > self.max_line_bytes:
            return None
        try:
            obj = json.loads(line.decode("utf-8", errors="replace"))
        except (ValueError, RecursionError):
            self.bad_lines += 1
            return None
        return obj if isinstance(obj, dict) else None
