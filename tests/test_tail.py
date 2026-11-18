"""Tests for incremental JSONL tailer."""

from __future__ import annotations

import json
import os
from pathlib import Path

from agent_watch.tail import DEFAULT_SKIP, JsonlTail


def test_appends_return_only_new_lines(tmp_path: Path) -> None:
    """Subsequent read_new() calls return only newly appended lines."""
    log = tmp_path / "agent.jsonl"
    log.write_bytes(b'{"n": 1}\n{"n": 2}\n')

    tail = JsonlTail(log)
    assert tail.changed() is True
    assert tail.read_new() == [{"n": 1}, {"n": 2}]
    assert tail.changed() is False
    assert tail.read_new() == []
    assert tail.offset == log.stat().st_size
    assert tail.lines_read == 2
    assert tail.bad_lines == 0

    with log.open("ab") as fh:
        fh.write(b'{"n": 3}\n{"n": 4}\n')

    assert tail.changed() is True
    assert tail.read_new() == [{"n": 3}, {"n": 4}]
    assert tail.changed() is False
    assert tail.offset == log.stat().st_size
    assert tail.lines_read == 4


def test_partial_last_line_held_until_newline(tmp_path: Path) -> None:
    """Incomplete trailing line stays buffered until newline arrives."""
    log = tmp_path / "partial.jsonl"
    log.write_bytes(b'{"a": 1}\n{"b": ')

    tail = JsonlTail(log)
    assert tail.read_new() == [{"a": 1}]
    assert tail.bad_lines == 0

    # Calling read_new again with no extra bytes keeps partial buffer intact.
    assert tail.read_new() == []

    with log.open("ab") as fh:
        fh.write(b"2")
    assert tail.read_new() == []
    assert tail.bad_lines == 0

    with log.open("ab") as fh:
        fh.write(b"}\n")
    assert tail.read_new() == [{"b": 2}]
    assert tail.bad_lines == 0


def test_truncation_resets_and_rereads(tmp_path: Path) -> None:
    """Rewriting a file shorter resets offset and buffer and reads from start."""
    log = tmp_path / "trunc.jsonl"
    log.write_bytes(
        b'{"step": 1, "pad": "aaaaaaaaaaaaaaaaaaaa"}\n'
        b'{"step": 2, "pad": "bbbbbbbbbbbbbbbbbbbb"}\n'
        b'{"dangling": '
    )

    tail = JsonlTail(log)
    assert tail.read_new() == [
        {"step": 1, "pad": "aaaaaaaaaaaaaaaaaaaa"},
        {"step": 2, "pad": "bbbbbbbbbbbbbbbbbbbb"},
    ]
    assert tail.offset > 50

    log.write_bytes(b'{"reset": true}\n')
    assert tail.changed() is True
    assert tail.read_new() == [{"reset": True}]
    assert tail.offset == log.stat().st_size
    assert tail.bad_lines == 0


def test_filters_and_bad_lines(tmp_path: Path) -> None:
    """Skip substrings, oversized lines, non-dicts, and bad JSON are handled."""
    log = tmp_path / "filter.jsonl"
    exact_limit_line = b'{"k":"' + b"x" * 22 + b'"}'  # len == 30
    oversized_line = b'{"k":"' + b"y" * 23 + b'"}'  # len == 31
    assert len(exact_limit_line) == 30
    assert len(oversized_line) == 31

    payload = b"\n".join(
        [
            b'{"type":"api-request","data":1}',
            b'{"type":"file-history-snapshot","files":[]}',
            b"",
            b'{"ok": 1}',
            exact_limit_line,
            oversized_line,
            b"{malformed json",
            b"not-json-at-all",
            b"[1, 2, 3]",
            b'"plain string"',
            b'{"utf8": "hi\xff"}',
        ]
    ) + b"\n"
    log.write_bytes(payload)

    tail = JsonlTail(log, skip_substrings=DEFAULT_SKIP, max_line_bytes=30)
    rows = tail.read_new()

    assert rows == [
        {"ok": 1},
        {"k": "x" * 22},
        {"utf8": "hi\ufffd"},
    ]
    assert tail.bad_lines == 2


def test_initial_tail_bytes_skips_old_and_partial_line(tmp_path: Path) -> None:
    """initial_tail_bytes seeks near EOF and discards the first partial line."""
    log = tmp_path / "big.jsonl"
    line1 = b'{"n": 1, "pad": "aaaaaaaaaa"}\n'
    line2 = b'{"n": 2, "pad": "bbbbbbbbbb"}\n'
    line3 = b'{"n": 3}\n'
    log.write_bytes(line1 + line2 + line3)

    # Cut right into the middle of line2.
    tail_bytes = len(line3) + 10
    tail = JsonlTail(log, initial_tail_bytes=tail_bytes)
    rows = tail.read_new()

    assert rows == [{"n": 3}]
    assert tail.bad_lines == 0
    assert tail.offset == log.stat().st_size

    with log.open("ab") as fh:
        fh.write(b'{"n": 4}\n')
    assert tail.read_new() == [{"n": 4}]


def test_initial_tail_bytes_partial_without_newline_discards_on_next_read(
    tmp_path: Path,
) -> None:
    """First partial line spanning chunks is still discarded cleanly."""
    log = tmp_path / "split_tail.jsonl"
    old = b'{"old": 1, "pad": "aaaaaaaaaaaa"}\n'
    half_mid = b'{"mid": "part1-'
    log.write_bytes(old + half_mid)

    tail = JsonlTail(log, initial_tail_bytes=10)
    assert tail.read_new() == []
    assert tail.bad_lines == 0

    with log.open("ab") as fh:
        fh.write(b'part2"}\n{"next": 2}\n')
    assert tail.read_new() == [{"next": 2}]
    assert tail.bad_lines == 0


def test_initial_tail_bytes_smaller_file_reads_all(tmp_path: Path) -> None:
    """When file size does not exceed initial_tail_bytes, read from start."""
    log = tmp_path / "small.jsonl"
    content = json.dumps({"all": 1}).encode("utf-8") + b"\n"
    log.write_bytes(content)

    tail = JsonlTail(log, initial_tail_bytes=1024)
    assert tail.read_new() == [{"all": 1}]


def test_missing_file_behavior(tmp_path: Path) -> None:
    """Missing file returns [] and changed() is False without altering offset."""
    log = tmp_path / "missing.jsonl"
    tail = JsonlTail(log)

    assert tail.changed() is False
    assert tail.read_new() == []
    assert tail.offset == 0

    log.write_bytes(b'{"appeared": 1}\n')
    assert tail.changed() is True
    assert tail.read_new() == [{"appeared": 1}]
    saved_offset = tail.offset
    assert saved_offset > 0

    log.unlink()
    assert tail.changed() is False
    assert tail.read_new() == []
    assert tail.offset == saved_offset


def test_no_open_handle_allows_replace_and_unlink(tmp_path: Path) -> None:
    """File can be replaced or deleted immediately after read_new()."""
    log = tmp_path / "lockcheck.jsonl"
    replaced = tmp_path / "replaced.jsonl"
    log.write_bytes(b'{"x": 1}\n')

    tail = JsonlTail(log)
    assert tail.read_new() == [{"x": 1}]

    os.replace(log, replaced)
    assert not log.exists()
    replaced.unlink()
    assert not replaced.exists()


def test_crlf_lines_parse(tmp_path: Path) -> None:
    """CRLF line endings and split CRLF boundaries parse cleanly."""
    log = tmp_path / "crlf.jsonl"
    log.write_bytes(b'{"a": 1}\r\n\r\n{"b": 2}\r')

    tail = JsonlTail(log)
    assert tail.read_new() == [{"a": 1}]

    with log.open("ab") as fh:
        fh.write(b'\n{"c": 3}\r\n')

    assert tail.read_new() == [{"b": 2}, {"c": 3}]
    assert tail.bad_lines == 0
