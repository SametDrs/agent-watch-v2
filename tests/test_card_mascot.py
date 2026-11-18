"""Unit tests for agent_watch.ui.card_mascot."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from rich.cells import cell_len
from rich.style import Style

from agent_watch import config
from agent_watch.model import Agent, Source, Status
from agent_watch.ui.card_mascot import (
    CANVAS_H,
    CANVAS_W,
    PALETTE,
    compose,
    mascot_state,
    render,
    sprite,
)


@pytest.mark.parametrize("src", list(Source))
@pytest.mark.parametrize("state", ["idle", "working", "done", "error"])
def test_frames_valid(src: Source, state: str) -> None:
    """Every frame across 80 ticks is 14x10 palette-valid and renders to 5x14 Rich lines."""
    for t in range(80):
        c = compose(sprite(src, state, t), state, t)
        assert len(c) == CANVAS_H and all(len(r) == CANVAS_W for r in c)
        assert set("".join(c)) <= set(PALETTE) | {"."}
        rendered = render(c)
        assert not rendered.plain.endswith("\n")
        lines = rendered.split()
        assert len(lines) == CANVAS_H // 2
        assert all(cell_len(line.plain) == CANVAS_W for line in lines)
        assert set(rendered.plain) <= {" ", "▀", "▄", "\n"}


def test_sprite_sizes() -> None:
    """Verify exact sprite row counts for Clawd, mini Gravi, and Codex cloud head."""
    assert (
        len(sprite(Source.CLAUDE, "idle", 0)),
        len(sprite(Source.AGY, "idle", 0)),
        len(sprite(Source.CODEX, "idle", 0)),
    ) == (5, 7, 6)


def test_render_half_block_styles() -> None:
    """Verify transparent, top-only, bottom-only, and dual-color half-block cells."""
    canvas = ["." * CANVAS_W for _ in range(CANVAS_H)]
    canvas[0] = ".o.o" + "." * (CANVAS_W - 4)
    canvas[1] = "..bk" + "." * (CANVAS_W - 4)

    rendered = render(canvas)
    assert not rendered.plain.endswith("\n")
    lines = rendered.plain.split("\n")
    assert len(lines) == CANVAS_H // 2
    assert lines[0] == " ▀▄▀" + " " * (CANVAS_W - 4)
    assert [(s.start, s.end, s.style) for s in rendered.spans] == [
        (1, 2, Style(color=PALETTE["o"])),
        (2, 3, Style(color=PALETTE["b"])),
        (3, 4, Style(color=PALETTE["o"], bgcolor=PALETTE["k"])),
    ]


def test_compose_effects_and_invalid_source() -> None:
    """Verify state-level effects (idle z, done sparkles, error flash) and source guard."""
    assert compose(sprite(Source.CLAUDE, "idle", 30), "idle", 30)[0][-1] == "z"
    assert compose(sprite(Source.CLAUDE, "done", 0), "done", 0)[1][0] == "y"
    assert "r" in "".join(compose(sprite(Source.CLAUDE, "error", 0), "error", 0))

    with pytest.raises(ValueError):
        sprite("unknown", "idle", 0)  # type: ignore[arg-type]


def test_mascot_state_mapping() -> None:
    """Verify Agent status + last_write mapping to working/idle/done/error."""
    now = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)

    def make_agent(status: Status, last_write: datetime | None = None) -> Agent:
        return Agent(
            key="claude:a1",
            source=Source.CLAUDE,
            session_key="sess-1",
            status=status,
            last_write=last_write,
        )

    assert mascot_state(make_agent(Status.RUNNING, now - timedelta(seconds=10)), now) == "working"
    assert (
        mascot_state(
            make_agent(Status.RUNNING, now - timedelta(seconds=config.QUIET_SECONDS)),
            now,
        )
        == "working"
    )
    assert mascot_state(make_agent(Status.RUNNING, None), now) == "working"
    assert (
        mascot_state(
            make_agent(Status.RUNNING, now - timedelta(seconds=config.QUIET_SECONDS + 1)),
            now,
        )
        == "idle"
    )
    assert mascot_state(make_agent(Status.PENDING, now), now) == "idle"
    assert mascot_state(make_agent(Status.PENDING, None), now) == "idle"
    assert mascot_state(make_agent(Status.DONE, now - timedelta(hours=1)), now) == "done"
    assert mascot_state(make_agent(Status.FAILED, now), now) == "error"
    assert mascot_state(make_agent(Status.STALE, now), now) == "error"
