"""Throwaway spike v2: card mascots with per-state animations (half-block pixel art).

Run:  python mascot_spike.py      (Ctrl+C to quit)
"""
import os
import sys
import time

os.system("")  # enable ANSI on Windows consoles
sys.stdout.reconfigure(encoding="utf-8")

PAL = {
    "o": "#d97757", "k": "#1a1a1a",                       # Clawd
    "a": "#f0883e", "b": "#6ea8fe", "B": "#7c9cff",       # Gravi
    "v": "#b388ff", "w": "#e6f7ff", "p": "#7c9cff",
    "u": "#3d7bf0", "U": "#2c5fcf", "s": "#101a33",       # Codex cloud bot
    "c": "#a8ecff",
    "r": "#f85149", "y": "#ffd866", "g": "#7ee787",       # effects
    "z": "#8b949e",
}

W, H = 14, 10      # canvas: 14 px wide, 10 px tall (5 terminal rows)
TICK = 0.15        # seconds per tick

# ---------------------------------------------------------------- sprites ----

def clawd(state, t):
    eyes = "..okooko.."
    legs = "..o.oo.o.."
    arms = "oooooooooo"
    if state == "idle":
        if t % 24 in (22, 23):
            eyes = "..oooooo.."                          # blink
        if 40 <= t % 60 < 48:                            # look around
            eyes = "..kooko..." if (t // 4) % 2 else "...okooko."
    elif state == "working":
        legs = ("..o.oo.o..", ".o.o..o.o.", "..o.oo.o..", "...o..o...")[t % 4]
        arms = ("oooooooooo", ".oooooooo.")[t % 2]
    elif state == "done":
        eyes = "..kkookk.."
        if t % 16 < 4:
            arms = "o.oooooo.o"                          # arms up
    elif state == "error":
        eyes = "..kook.k.."[:10] if t % 2 else "..okooko.."
    return ["..oooooo..", eyes, arms, "..oooooo..", legs]


def gravi(state, t):
    ant = "....a....."
    eyes = ".bwkwwkwb."
    jet = ".........."
    if state == "idle":
        if t % 28 in (26, 27):
            eyes = ".bwwwwwwb."
        ant = "....a....." if (t // 6) % 2 else "....y....."
    elif state == "working":
        ant = ("....y.....", "....a.....")[t % 2]
        jet = ("...p..p...", "..p....p..", "...p..p...", "....pp....")[t % 4]
        eyes = (".bwkwwkwb.", ".bwwkwwkb.")[(t // 3) % 2]       # eyes scan
    elif state == "done":
        eyes = ".bkwkkwkb."
    elif state == "error":
        eyes = ".bwkwwkwb." if t % 2 else ".bkwkwkwb."
    return [ant, "..bbbbbb..", ".bwwwwwwb.", eyes, ".bBBBBBBv.", "..vvvvvv..", jet]


FACE = {
    "normal": ["csssss", "scssss", "csscss"],
    "off":    ["csssss", "scssss", "csssss"],
    "blank":  ["ssssss", "ssssss", "ssssss"],
    "happy":  ["ssssss", "cssssc", "sccccs"],
    "error":  ["rsrrsr", "srssrs", "rsrrsr"],
}
TYPING = [["csssss", "scssss", "cscsss"],
          ["csssss", "scssss", "csccss"],
          ["csssss", "scssss", "cscccs"],
          ["csssss", "scssss", "csssss"]]


def codex(state, t):
    face = FACE["normal"] if (t // 4) % 2 else FACE["off"]     # cursor blink
    top = ".uu.uu.uu."
    if state == "idle":
        if t % 30 in (28, 29):
            face = FACE["blank"]                                # screen flicker
        if 50 <= t % 70 < 58:
            top = ("..uu.uu.u.", ".u.uu.uu..")[(t // 2) % 2]   # cloud puffs drift
    elif state == "working":
        face = TYPING[(t // 2) % 4]
    elif state == "done":
        face = FACE["happy"]
    elif state == "error":
        face = FACE["error"]
    return [top,
            "uuuuuuuuuu",
            "uu" + face[0] + "uu",
            "uu" + face[1] + "uu",
            "uu" + face[2] + "uu",
            ".uuuuuuuu."]


MASCOTS = {"Clawd (claude)": clawd, "Gravi (agy)": gravi, "Cloud head (codex)": codex}

# -------------------------------------------------------------- effects ----

def compose(rows, state, t):
    """Place a sprite on the canvas and apply state-wide effects."""
    h, w = len(rows), len(rows[0])
    dx = (W - w) // 2
    dy = H - h - 1                                   # 1 spare row for hopping
    if state == "working":
        dy += t % 2                                  # bob
    elif state == "done" and t % 16 < 4:
        dy = max(0, dy - 1)                          # jump
    elif state == "error" and t % 20 < 6:
        dx += (-1, 1)[t % 2]                         # shake
    if state == "idle" and 0 <= t % 60 < 2:
        dy = max(0, dy - 1)                          # little hop now and then
    canvas = [["."] * W for _ in range(H)]
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch != "." and 0 <= y + dy < H and 0 <= x + dx < W:
                canvas[y + dy][x + dx] = ch
    if state == "error" and t % 20 < 6 and t % 2 == 0:  # red flash
        keep = set("ksrcw")
        canvas = [[c if c in keep or c == "." else "r" for c in row] for row in canvas]
    if state == "done":                              # sparkles
        for (x, y), on in (((0, 1), t % 6 < 3), ((W - 1, 0), t % 6 >= 3), ((W - 2, 4), t % 8 < 2)):
            if on and canvas[y][x] == ".":
                canvas[y][x] = "y"
    if state == "idle" and t % 40 >= 30:            # sleepy "z"
        canvas[0][W - 1 - (t % 40 - 30) // 4] = "z"
    return ["".join(r) for r in canvas]

# ------------------------------------------------------------- terminal ----

def rgb(h):
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def render(rows):
    out = []
    for y in range(0, H, 2):
        line = []
        for x in range(W):
            a, b = rows[y][x], rows[y + 1][x]
            if a == "." and b == ".":
                line.append(" ")
            elif b == ".":
                line.append("\x1b[38;2;%d;%d;%dm▀\x1b[0m" % rgb(PAL[a]))
            elif a == ".":
                line.append("\x1b[38;2;%d;%d;%dm▄\x1b[0m" % rgb(PAL[b]))
            else:
                line.append("\x1b[38;2;%d;%d;%d;48;2;%d;%d;%dm▀\x1b[0m" % (rgb(PAL[a]) + rgb(PAL[b])))
        out.append("".join(line))
    return out


STATES = ["idle", "working", "done", "error"]
DIM, BOLD, RST = "\x1b[2m", "\x1b[1m", "\x1b[0m"
CARDS = [
    ("Clawd (claude)", "working", "claude", "Explore · opus · low", "00:41 · 8.2k tok", "Grep \"token\" (14 matches)"),
    ("Gravi (agy)", "idle", "agy", "gemini-4-hydrogen · high", "02:14 · 12.4k tok", "waiting for model..."),
    ("Cloud head (codex)", "done", "codex", "gpt-5.6-terra · medium", "05:02 · 95.4k tok", "✓ done"),
    ("Cloud head (codex)", "error", "codex", "gpt-5.6-terra · high", "00:12 · 1.1k tok", "\x1b[31m✗ exited 1\x1b[0m"),
]


def main():
    sys.stdout.write("\x1b[?25l\x1b[2J")
    t = 0
    try:
        while True:
            out = ["\x1b[H" + BOLD + "Mascot spike v2  " + RST + DIM + "(Ctrl+C to quit)" + RST, ""]
            out.append(" " * 20 + "".join(s.ljust(18) for s in STATES))
            for name, fn in MASCOTS.items():
                blocks = [render(compose(fn(s, t), s, t)) for s in STATES]
                for i in range(H // 2):
                    label = (BOLD + name.ljust(20) + RST) if i == 2 else " " * 20
                    out.append(label + "".join(b[i] + " " * 4 for b in blocks))
                out.append("")
            out.append(BOLD + "As cards:" + RST)
            for name, state, src, model, stats, step in CARDS:
                spr = render(compose(MASCOTS[name](state, t), state, t))
                text = ["", BOLD + src + RST + "  " + model, DIM + stats + RST, step, ""]
                out.append("╭" + "─" * 60 + "╮")
                for i in range(H // 2):
                    out.append("│ " + spr[i] + "  " + text[i] + "\x1b[63G│")
                out.append("╰" + "─" * 60 + "╯")
            sys.stdout.write("\n".join(line + "\x1b[K" for line in out))
            sys.stdout.flush()
            t += 1
            time.sleep(TICK)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\x1b[0m\x1b[?25h\n")


if __name__ == "__main__":
    main()
