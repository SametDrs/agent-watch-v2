"""Generate the README screenshots from invented demo data.

Nothing here reads real logs. The app is built with throw-away home directories, its
poller is replaced by a no-op, and the store is filled with fake sessions and events.
The launcher state directory is redirected to a temp dir, so the single-instance lock
of a running agent-watch is never touched.

Usage (repo root):

    .venv\\Scripts\\python scripts/demo_screenshots.py            # SVG -> PNG in docs/images
    .venv\\Scripts\\python scripts/demo_screenshots.py --svg-only  # keep SVGs in --svg-dir

The SVG -> PNG step needs Microsoft Edge (headless). Set EDGE_BIN to override its path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Redirect launcher state before agent_watch is used (never touch the real lock).
_TMP = Path(tempfile.mkdtemp(prefix="agent-watch-demo-"))
os.environ["LOCALAPPDATA"] = str(_TMP / "localappdata")
os.environ["AGENT_WATCH_RUN_DIR"] = str(_TMP / "run")

from agent_watch.app import AgentWatchApp  # noqa: E402
from agent_watch.model import Event, EventKind, Status, main_agent_key  # noqa: E402
from agent_watch.poller import PollResult  # noqa: E402
from agent_watch.sources.claude import summarize_tool  # noqa: E402
from agent_watch.sources.codex import CodexLimits  # noqa: E402

NOW = datetime.now(timezone.utc).replace(microsecond=0)
# Seconds between the start of session 1 and "now"; its main agent is still running.
NOW_OFFSET = 15 * 60 + 5

S1 = "5d1c0a9e-3b7f-4c21-9a68-0e2f7d41b3c5"
S2 = "b8e47f12-6c0d-49a3-8d15-72a9c3e0f6d4"
CWD1 = "/workspace/payment-service"
CWD2 = "/workspace/docs-site"

OPUS = "claude-opus-5-5-high"
SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"
HYDROGEN_HIGH = "gemini-4-hydrogen-high"
HYDROGEN_MED = "gemini-4-hydrogen-medium"
CODEX_MODEL = "gpt-5.6-terra-high"

K_SCOUT = "claude:a3f90c1d7e2b4a58"
K_TESTS = "claude:7be2d4106c9a3f81"
K_REVIEW = "claude:c41a8e5f20d7b936"
K_AGY_NOTES = "agy:1f6c2e9a-84d3-4b70-a5e1-93c7d02b8f46"
K_AGY_BENCH = "agy:e07b5a31-9c42-4d68-b1f0-2a8d6c43e975"
K_CODEX = "codex:019a7c3e-52d1-7f84-9b06-c8e13a4d7f20"
K_DOCS_SUB = "claude:92d5e8a07b1c4f36"


class Script:
    """Collects events for one demo agent, with offsets in seconds from a base time."""

    def __init__(self, events: list[Event], session: str, key: str, base: datetime) -> None:
        self.events, self.session, self.key, self.base = events, session, key, base
        self._n = 0

    def add(self, kind: EventKind, at: float, **kw) -> None:
        self.events.append(Event(
            ts=self.base + timedelta(seconds=at), session_key=self.session,
            agent_key=self.key, kind=kind, **kw,
        ))

    def start(self, at: float, title: str | None, model: str, effort: str | None = None) -> None:
        self.add(EventKind.START, at, title=title, model=model, effort=effort)

    def prompt(self, at: float, body: str) -> None:
        self.add(EventKind.PROMPT, at, summary=" ".join(body.split())[:120], body=body)

    def think(self, at: float, body: str) -> None:
        self.add(EventKind.THINKING, at, summary=" ".join(body.split())[:120], body=body)

    def text(self, at: float, body: str) -> None:
        self.add(EventKind.TEXT, at, summary=" ".join(body.split())[:120], body=body)

    def usage(self, at: float, tin: int, cached: int, tout: int, reasoning: int = 0) -> None:
        self.add(EventKind.USAGE, at, tokens_in=tin, tokens_cached=cached,
                 tokens_out=tout, tokens_reasoning=reasoning, ctx_tokens=tin)

    def tool(self, at: float, name: str, args: dict, result: str, *, took: float = 1.5,
             error: bool = False, summary: str | None = None) -> None:
        """Emit a paired tool start/end (Claude-style summary unless one is given)."""
        self._n += 1
        tid = f"toolu_{self.key[-6:]}_{self._n}"
        self.add(EventKind.TOOL_START, at, tool=name, summary=summary or summarize_tool(name, args, ""),
                 body=json.dumps(args), tool_id=tid)
        self.add(EventKind.TOOL_END, at + took, tool=name, summary=f"error: {name}" if error else "",
                 body=result, tool_id=tid, status=Status.FAILED if error else None)

    def end(self, at: float, status: Status = Status.DONE, summary: str = "") -> None:
        self.add(EventKind.END, at, status=status, summary=summary)

    def fail(self, at: float, summary: str) -> None:
        self.add(EventKind.ERROR, at, summary=summary, status=Status.FAILED)


def _scout(events: list[Event], base: datetime) -> None:
    s = Script(events, S1, K_SCOUT, base)
    s.start(0, "Scout API handlers", SONNET)
    s.prompt(0, "List every HTTP handler under src/payments/ and note which ones call the legacy gateway client.")
    s.tool(8, "Glob", {"pattern": "src/payments/**/handlers*.py"},
           "src/payments/handlers.py\nsrc/payments/webhooks/handlers.py")
    s.usage(10, 18_400, 11_000, 620)
    s.tool(40, "Grep", {"pattern": "LegacyGateway\\("},
           "src/payments/handlers.py:41\nsrc/payments/refund.py:77\nsrc/payments/webhooks/handlers.py:23")
    s.usage(45, 31_000, 22_000, 880)
    s.tool(95, "Read", {"file_path": "src/payments/handlers.py"}, "...")
    s.usage(100, 46_600, 37_000, 1_900)
    s.text(185, "Found 9 handlers in 3 modules; 4 still call LegacyGateway.")
    s.usage(186, 0, 0, 800)
    s.end(192)


def _review(events: list[Event], base: datetime) -> None:
    s = Script(events, S1, K_REVIEW, base)
    s.start(0, "Review diff", HAIKU)
    s.prompt(0, "Review the staged diff for naming and error-handling issues.")
    s.tool(6, "Bash", {"command": "git diff --staged --stat", "description": "Show staged diff stats"},
           " src/payments/refund.py | 48 ++++++++++\n 3 files changed")
    s.usage(9, 22_000, 14_000, 700)
    s.tool(50, "Read", {"file_path": "src/payments/refund.py"}, "...")
    s.usage(55, 36_000, 26_000, 1_400)
    s.text(96, "2 nits: rename `amt` to `amount_cents`; wrap `post_ledger` in try/except.")
    s.end(102)


def _tests(events: list[Event], base: datetime) -> None:
    """The conversation shown in the screenshot (one tool call fails)."""
    s = Script(events, S1, K_TESTS, base)
    s.start(0, "Write migration tests", SONNET)
    s.prompt(0, "Write pytest tests for migration 0042_add_refund_ledger (upgrade + downgrade). "
                "Reuse tests/conftest.py; do not touch src/.")
    s.think(6, "Check how db fixtures are built, then assert upgrade creates the table and index and downgrade drops them.")
    s.text(9, "I'll read the migration and look for existing database fixtures.")
    s.usage(10, 21_000, 12_000, 540, 310)
    s.tool(12, "Read", {"file_path": "migrations/versions/0042_add_refund_ledger.py"},
           "def upgrade():\n    op.create_table(\n        'refund_ledger',\n"
           "        sa.Column('id', sa.Integer, primary_key=True),\n"
           "        sa.Column('payment_id', sa.Integer, nullable=False),\n"
           "        sa.Column('amount_cents', sa.Integer, nullable=False),\n    )\n"
           "    op.create_index('ix_refund_payment', 'refund_ledger', ['payment_id'])\n\n"
           "def downgrade():\n    op.drop_table('refund_ledger')")
    s.tool(26, "Grep", {"pattern": "def db_session", "path": "tests/"},
           "tests/conftest.py:18:def db_session(tmp_path):")
    s.usage(30, 38_000, 29_000, 1_100)
    s.tool(52, "Bash", {"command": "pytest tests/migrations -q"},
           "ImportError while importing test module 'tests/migrations/test_0042_refund_ledger.py'.\n"
           "tests/migrations/test_0042_refund_ledger.py:4: in <module>\n"
           "    from payments.ledger import RefundLedger\n"
           "E   ModuleNotFoundError: No module named 'payments.ledger'", took=4.1, error=True)
    s.usage(58, 56_000, 44_000, 1_500, 220)
    s.text(60, "The test imports a module that does not exist. The model lives in `payments.models`, "
               "so I'll fix the import.")
    s.tool(72, "Edit", {"file_path": "tests/migrations/test_0042_refund_ledger.py",
                        "old_string": "from payments.ledger import RefundLedger",
                        "new_string": "from payments.models import RefundLedger"},
           "The file tests/migrations/test_0042_refund_ledger.py has been updated.")
    s.usage(80, 73_000, 66_000, 6_400)
    s.text(120, "Fixed the import in the new test file. I did not re-run the suite yet.")
    s.end(124)


def _agy_notes(events: list[Event], base: datetime) -> None:
    s = Script(events, S1, K_AGY_NOTES, base)
    s.start(0, "Draft changelog notes", HYDROGEN_HIGH, "high")
    s.prompt(0, "Draft changelog notes for the refund ledger change.")
    s.tool(10, "view_file", {"AbsolutePath": "CHANGELOG.md"}, "...", summary="view_file CHANGELOG.md")
    s.usage(14, 14_000, 0, 700, 480)
    s.tool(60, "write_to_file", {"TargetFile": "docs/changelog-draft.md"}, "ok",
           summary="write_to_file docs/changelog-draft.md")
    s.usage(70, 17_000, 0, 1_100, 160)
    s.text(150, "Wrote docs/changelog-draft.md")
    s.end(158)


def _agy_bench(events: list[Event], base: datetime) -> None:
    s = Script(events, S1, K_AGY_BENCH, base)
    s.start(0, "Benchmark serializer", HYDROGEN_MED, "medium")
    s.prompt(0, "Benchmark the ledger serializer against the old JSON encoder.")
    s.tool(12, "run_command", {"CommandLine": "python bench/serializer.py --rounds 500"}, "...",
           summary="run_command python bench/serializer.py --rounds 500")
    s.usage(16, 22_000, 0, 900, 300)
    s.fail(72, "benchmark timed out after 60s")


def _codex(events: list[Event], base: datetime) -> None:
    s = Script(events, S1, K_CODEX, base)
    s.start(0, "Fix flaky retry test", CODEX_MODEL, "high")
    s.prompt(0, "tests/test_retry.py::test_backoff is flaky, make it deterministic.")
    s.tool(15, "shell", {"command": "pytest tests/test_retry.py -q"}, "1 failed, 4 passed",
           summary="shell pytest tests/test_retry.py -q")
    s.usage(20, 62_000, 50_000, 2_800, 900)
    s.tool(90, "apply_patch", {"path": "src/payments/retry.py"}, "Success",
           summary="apply_patch src/payments/retry.py")
    s.usage(100, 81_000, 68_000, 3_500, 1_300)
    s.text(270, "Replaced the sleep with an injected clock; 5 passed.")
    s.end(278)


def _main1(events: list[Event], base: datetime) -> None:
    s = Script(events, S1, main_agent_key(S1), base)
    s.start(0, "Refactor payment service", OPUS, "high")
    s.prompt(0, "Refactor the payment service: move refunds to a ledger table and drop the legacy gateway client.")
    s.usage(5, 52_000, 20_000, 2_600, 900)
    s.text(18, "Plan: scout handlers, write migration tests, then refactor refund flow.")
    s.usage(300, 158_000, 130_000, 7_800, 2_400)
    s.usage(700, 202_000, 181_000, 8_000)
    s.tool(820, "Edit", {"file_path": "src/payments/refund.py"}, "updated", summary="Edit src/payments/refund.py")
    s.tool(846, "Bash", {"command": "pytest tests/ -q", "description": "Run test suite"}, "42 passed",
           summary="Bash Run test suite")
    s.tool(NOW_OFFSET - 14, "Edit", {"file_path": "src/payments/handlers.py"}, "updated",
           summary="Edit src/payments/handlers.py")


def _docs(events: list[Event], base: datetime) -> None:
    m = Script(events, S2, main_agent_key(S2), base)
    m.start(0, "Docs cleanup", OPUS, "high")
    m.prompt(0, "Clean up the docs: fix broken links and stale install steps.")
    m.usage(8, 41_000, 30_000, 1_500)
    m.text(20, "Delegating link checks, then updating install steps.")
    m.tool(230, "Edit", {"file_path": "docs/install.md"}, "updated", summary="Edit docs/install.md")
    m.usage(240, 47_000, 40_000, 2_400)
    m.text(300, "Docs cleanup finished.")
    m.end(305)
    s = Script(events, S2, K_DOCS_SUB, base)
    s.start(15, "Fix broken links", HAIKU)
    s.prompt(15, "Find and fix broken relative links in docs/.")
    s.tool(25, "Grep", {"pattern": "\\]\\(\\.\\./"}, "docs/api.md:12\ndocs/guide.md:40")
    s.usage(30, 29_000, 20_000, 1_100)
    s.tool(120, "Edit", {"file_path": "docs/api.md"}, "updated", summary="Edit docs/api.md")
    s.usage(130, 31_000, 24_000, 1_500)
    s.text(200, "Fixed 6 broken links.")
    s.end(205)


def build(app: AgentWatchApp) -> list[Event]:
    """Fill the app's store with fake sessions and return the events (chronological)."""
    base1 = NOW - timedelta(seconds=NOW_OFFSET)
    base2 = NOW - timedelta(minutes=52)
    events: list[Event] = []
    _main1(events, base1)
    _scout(events, base1 + timedelta(seconds=20))
    _review(events, base1 + timedelta(seconds=770))
    _agy_notes(events, base1 + timedelta(seconds=130))
    _tests(events, base1 + timedelta(seconds=260))
    _agy_bench(events, base1 + timedelta(seconds=300))
    _codex(events, base1 + timedelta(seconds=520))
    _docs(events, base2)
    events.sort(key=lambda e: e.ts)

    store = app.store
    store.upsert_session(S1, name="Refactor payment service", cwd=CWD1, status="active", started=base1)
    store.upsert_session(S2, name="Docs cleanup", cwd=CWD2, status="active", started=base2)
    store.apply(events)

    for key in (K_SCOUT, K_TESTS, K_REVIEW, K_AGY_NOTES, K_DOCS_SUB):
        agent = store.find_agent(key)
        assert agent is not None
        agent.parent_agent_key = main_agent_key(agent.session_key)
    for key in (K_CODEX, K_AGY_BENCH):  # launched from a subagent: card shows "<- parent"
        agent = store.find_agent(key)
        assert agent is not None
        agent.parent_agent_key = K_TESTS
    # Cards count tool calls; the scripted transcripts only carry a few of them.
    calls = {main_agent_key(S1): 41, K_SCOUT: 12, K_REVIEW: 7, K_AGY_NOTES: 9,
             K_AGY_BENCH: 5, K_CODEX: 14, main_agent_key(S2): 18, K_DOCS_SUB: 11}
    for key, count in calls.items():
        agent = store.find_agent(key)
        assert agent is not None
        agent.calls = max(agent.calls, count)
    return events


def make_app() -> AgentWatchApp:
    """Build the real app on empty fake homes with scanning disabled."""
    empty = _TMP / "empty"
    app = AgentWatchApp(
        claude_home=empty / "claude", agy_home=empty / "agy", codex_home=empty / "codex",
        no_quota=True, proc_provider=lambda: [],
    )
    app.poller.poll_once = lambda now=None: PollResult()  # never scan anything
    app._demo_events = build(app)  # type: ignore[attr-defined]
    return app


async def _settle(pilot, seconds: float = 0.6) -> None:
    await pilot.pause()
    await asyncio.sleep(seconds)
    await pilot.pause()


async def _prepare_dashboard(app: AgentWatchApp, pilot) -> None:
    await _settle(pilot, 0.5)
    dash = app.dashboard
    dash.add_events(app._demo_events)  # type: ignore[attr-defined]
    dash.selected_agent_key = K_TESTS
    dash._refresh_view()
    await _settle(pilot, 0.8)


def _fake_quota(app: AgentWatchApp) -> None:
    """Show the quota panel (it takes 36 columns, so only the timeline shot uses it)."""
    app.dashboard.update_quota(
        {"Gemini 5h": (0.78, NOW + timedelta(hours=3)), "Gemini 7d": (0.64, NOW + timedelta(days=4)),
         "Claude/GPT 5h": (0.41, NOW + timedelta(hours=2)), "Claude/GPT 7d": (0.55, NOW + timedelta(days=3)),
         "Argon 7d": (0.9, NOW + timedelta(days=5))},
        CodexLimits(used_percent=37, window_minutes=300, resets_at=NOW + timedelta(hours=2)),
    )


def _save(app: AgentWatchApp, out: Path, name: str) -> Path:
    return Path(app.save_screenshot(filename=f"{name}.svg", path=str(out)))


async def shoot_all(out: Path) -> dict[str, Path]:
    """Capture the four SVGs; return name -> svg path."""
    shots: dict[str, Path] = {}

    app = make_app()
    async with app.run_test(size=(140, 42)) as pilot:
        await _prepare_dashboard(app, pilot)
        shots["dashboard"] = _save(app, out, "dashboard")
        _fake_quota(app)
        await pilot.resize_terminal(140, 30)  # only 7 rows of bars: a shorter window avoids dead space
        await pilot.press("t")
        await _settle(pilot, 0.8)
        shots["timeline"] = _save(app, out, "timeline")

    app = make_app()
    async with app.run_test(size=(140, 42)) as pilot:
        await _prepare_dashboard(app, pilot)
        app._open_conversation(K_TESTS)
        await _settle(pilot, 0.8)
        for _ in range(3):  # j: Read, Grep, Bash (the failing call)
            await pilot.press("j")
        await pilot.press("enter")
        await _settle(pilot, 0.6)
        shots["conversation"] = _save(app, out, "conversation")

    app = make_app()
    async with app.run_test(size=(90, 40)) as pilot:
        await _prepare_dashboard(app, pilot)
        shots["narrow"] = _save(app, out, "narrow")
    return shots


def find_edge() -> str | None:
    """Locate msedge.exe (EDGE_BIN, PATH, then the standard install folders)."""
    if env := os.environ.get("EDGE_BIN"):
        return env
    if found := shutil.which("msedge"):
        return found
    for var in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(var)
        if base:
            cand = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if cand.exists():
                return str(cand)
    return None


def window_for(svg: Path, width: int) -> tuple[int, int]:
    """Return a window that fits the SVG at the given width (its height follows the aspect ratio)."""
    match = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg.read_text(encoding="utf-8")[:2000])
    if match is None:
        return width, 1150
    return width, math.ceil(float(match.group(2)) * width / float(match.group(1)))


def svg_to_png(edge: str, svg: Path, png: Path, width: int) -> None:
    """Render an SVG to PNG with headless Edge (scaled to `width` pixels wide)."""
    window = window_for(svg, width)
    png.parent.mkdir(parents=True, exist_ok=True)
    if png.exists():
        png.unlink()
    subprocess.run(
        [edge, "--headless=new", "--disable-gpu", "--hide-scrollbars",
         f"--window-size={window[0]},{window[1]}", f"--screenshot={png}", svg.resolve().as_uri()],
        check=True, timeout=120, capture_output=True,
    )


WIDTHS = {"dashboard": 1800, "timeline": 1800, "conversation": 1800, "narrow": 1200}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(REPO / "docs" / "images"), help="PNG output folder")
    parser.add_argument("--svg-dir", default=None, help="where to write the SVGs (default: temp dir)")
    parser.add_argument("--svg-only", action="store_true", help="skip the Edge PNG conversion")
    args = parser.parse_args(argv)

    svg_dir = Path(args.svg_dir) if args.svg_dir else _TMP / "svg"
    svg_dir.mkdir(parents=True, exist_ok=True)
    shots = asyncio.run(shoot_all(svg_dir))
    print("SVGs:", ", ".join(str(p) for p in shots.values()))
    if args.svg_only:
        return 0
    edge = find_edge()
    if edge is None:
        print("msedge.exe not found (set EDGE_BIN); SVGs were kept.", file=sys.stderr)
        return 1
    out = Path(args.out)
    for name, svg in shots.items():
        svg_to_png(edge, svg, out / f"{name}.png", WIDTHS[name])
        print("PNG:", out / f"{name}.png")
    shutil.rmtree(_TMP, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
