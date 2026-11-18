"""Card pixel mascots (Clawd, Gravi mini, Codex cloud head) rendered as Rich Text."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from rich.style import Style
from rich.text import Text

from agent_watch import config
from agent_watch.model import Agent, Source, Status

CANVAS_W = 14
CANVAS_H = 10

PALETTE: dict[str, str] = {
    "o": "#d97757", "k": "#1a1a1a",                       # Clawd
    "a": "#f0883e", "b": "#6ea8fe", "B": "#7c9cff",       # Gravi
    "v": "#b388ff", "w": "#e6f7ff", "p": "#7c9cff",
    "u": "#3d7bf0", "U": "#2c5fcf", "s": "#101a33",       # Codex cloud bot
    "c": "#a8ecff",
    "r": "#f85149", "y": "#ffd866", "g": "#7ee787",       # effects
    "z": "#8b949e",
}

_CODEX_FACE: dict[str, list[str]] = {
    "normal": ["csssss", "scssss", "csscss"],
    "off":    ["csssss", "scssss", "csssss"],
    "blank":  ["ssssss", "ssssss", "ssssss"],
    "happy":  ["ssssss", "cssssc", "sccccs"],
    "error":  ["rsrrsr", "srssrs", "rsrrsr"],
}

_CODEX_TYPING: list[list[str]] = [
    ["csssss", "scssss", "cscsss"],
    ["csssss", "scssss", "csccss"],
    ["csssss", "scssss", "cscccs"],
    ["csssss", "scssss", "csssss"],
]


def _clawd(state: str, tick: int) -> list[str]:
    """Build 5x10 pixel rows for Clawd."""
    eyes = "..okooko.."
    legs = "..o.oo.o.."
    arms = "oooooooooo"
    if state == "idle":
        if tick % 24 in (22, 23):
            eyes = "..oooooo.."  # blink
        if 40 <= tick % 60 < 48:  # look around
            eyes = "..kooko..." if (tick // 4) % 2 else "...okooko."
    elif state == "working":
        legs = ("..o.oo.o..", ".o.o..o.o.", "..o.oo.o..", "...o..o...")[tick % 4]
        arms = ("oooooooooo", ".oooooooo.")[tick % 2]
    elif state == "done":
        eyes = "..kkookk.."
        if tick % 16 < 4:
            arms = "o.oooooo.o"  # arms up
    elif state == "error":
        eyes = "..kook.k.."[:10] if tick % 2 else "..okooko.."
    return ["..oooooo..", eyes, arms, "..oooooo..", legs]


def _gravi(state: str, tick: int) -> list[str]:
    """Build 7x10 pixel rows for mini Gravi."""
    ant = "....a....."
    eyes = ".bwkwwkwb."
    jet = ".........."
    if state == "idle":
        if tick % 28 in (26, 27):
            eyes = ".bwwwwwwb."
        ant = "....a....." if (tick // 6) % 2 else "....y....."
    elif state == "working":
        ant = ("....y.....", "....a.....")[tick % 2]
        jet = ("...p..p...", "..p....p..", "...p..p...", "....pp....")[tick % 4]
        eyes = (".bwkwwkwb.", ".bwwkwwkb.")[(tick // 3) % 2]  # eyes scan
    elif state == "done":
        eyes = ".bkwkkwkb."
    elif state == "error":
        eyes = ".bwkwwkwb." if tick % 2 else ".bkwkwkwb."
    return [ant, "..bbbbbb..", ".bwwwwwwb.", eyes, ".bBBBBBBv.", "..vvvvvv..", jet]


def _codex(state: str, tick: int) -> list[str]:
    """Build 6x10 pixel rows for the Codex cloud head."""
    face = _CODEX_FACE["normal"] if (tick // 4) % 2 else _CODEX_FACE["off"]  # cursor blink
    top = ".uu.uu.uu."
    if state == "idle":
        if tick % 30 in (28, 29):
            face = _CODEX_FACE["blank"]  # screen flicker
        if 50 <= tick % 70 < 58:
            top = ("..uu.uu.u.", ".u.uu.uu..")[(tick // 2) % 2]  # cloud puffs drift
    elif state == "working":
        face = _CODEX_TYPING[(tick // 2) % 4]
    elif state == "done":
        face = _CODEX_FACE["happy"]
    elif state == "error":
        face = _CODEX_FACE["error"]
    return [
        top,
        "uuuuuuuuuu",
        "uu" + face[0] + "uu",
        "uu" + face[1] + "uu",
        "uu" + face[2] + "uu",
        ".uuuuuuuu.",
    ]


_SPRITE_BUILDERS: dict[Source, Callable[[str, int], list[str]]] = {
    Source.CLAUDE: _clawd,
    Source.AGY: _gravi,
    Source.CODEX: _codex,
}


def sprite(source: Source, state: str, tick: int) -> list[str]:
    """Return uncomposed sprite rows for a source, mascot state, and animation tick."""
    builder = _SPRITE_BUILDERS.get(source)
    if builder is None:
        raise ValueError(f"Unsupported mascot source: {source!r}")
    return builder(state, tick)


def compose(rows: list[str], state: str, tick: int) -> list[str]:
    """Place a sprite on the 14x10 canvas and apply state-wide effects."""
    h, w = len(rows), len(rows[0])
    dx = (CANVAS_W - w) // 2
    dy = CANVAS_H - h - 1  # 1 spare row for hopping
    if state == "working":
        dy += tick % 2  # bob
    elif state == "done" and tick % 16 < 4:
        dy = max(0, dy - 1)  # jump
    elif state == "error" and tick % 20 < 6:
        dx += (-1, 1)[tick % 2]  # shake
    if state == "idle" and 0 <= tick % 60 < 2:
        dy = max(0, dy - 1)  # little hop now and then

    canvas = [["."] * CANVAS_W for _ in range(CANVAS_H)]
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch != "." and 0 <= y + dy < CANVAS_H and 0 <= x + dx < CANVAS_W:
                canvas[y + dy][x + dx] = ch

    if state == "error" and tick % 20 < 6 and tick % 2 == 0:  # red flash
        keep = set("ksrcw")
        canvas = [[c if c in keep or c == "." else "r" for c in row] for row in canvas]
    if state == "done":  # sparkles
        sparkles = (
            ((0, 1), tick % 6 < 3),
            ((CANVAS_W - 1, 0), tick % 6 >= 3),
            ((CANVAS_W - 2, 4), tick % 8 < 2),
        )
        for (x, y), on in sparkles:
            if on and canvas[y][x] == ".":
                canvas[y][x] = "y"
    if state == "idle" and tick % 40 >= 30:  # sleepy "z"
        canvas[0][CANVAS_W - 1 - (tick % 40 - 30) // 4] = "z"
    return ["".join(r) for r in canvas]


def _cell(top: str, bottom: str) -> tuple[str, Style | None]:
    """Return the half-block character and Rich style for two vertically stacked pixels."""
    if top == "." and bottom == ".":
        return " ", None
    if bottom == ".":
        return "▀", Style(color=PALETTE[top])
    if top == ".":
        return "▄", Style(color=PALETTE[bottom])
    return "▀", Style(color=PALETTE[top], bgcolor=PALETTE[bottom])


def render(canvas: list[str]) -> Text:
    """Render a 14x10 pixel canvas into 5 half-block Rich Text lines without trailing newline."""
    out = Text()
    for y in range(0, CANVAS_H, 2):
        if y > 0:
            out.append("\n")
        top_row = canvas[y]
        bot_row = canvas[y + 1]
        for x in range(CANVAS_W):
            ch, style = _cell(top_row[x], bot_row[x])
            out.append(ch, style)
    return out


def _as_utc(dt: datetime) -> datetime:
    """Attach UTC timezone to naive datetimes so subtraction never raises TypeError."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def mascot_state(agent: Agent, now: datetime) -> str:
    """Map an Agent's execution status and write recency to a mascot animation state."""
    if agent.status == Status.DONE:
        return "done"
    if agent.status in (Status.FAILED, Status.STALE):
        return "error"
    if agent.status == Status.RUNNING:
        if agent.last_write is None:
            return "working"
        elapsed = (_as_utc(now) - _as_utc(agent.last_write)).total_seconds()
        return "working" if elapsed <= config.QUIET_SECONDS else "idle"
    return "idle"
