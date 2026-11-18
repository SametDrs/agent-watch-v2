"""agent-watch application: dashboard wiring, polling loops and CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from textual.app import App

from agent_watch import __version__, config, launcher, quota
from agent_watch.poller import PollResult, Poller
from agent_watch.quota import QuotaService
from agent_watch.sources.agy import AgyScanner
from agent_watch.sources.claude import ClaudeScanner
from agent_watch.sources.codex import CodexScanner
from agent_watch.store import Store
from agent_watch.ui.conversation import ConversationScreen
from agent_watch.ui.dashboard import DashboardScreen
from agent_watch.ui.widgets import AgentCard, AgentList

SCANNING_SUBTITLE = "taranıyor…"
QUOTA_INTERVAL_SECONDS = 60.0

_logger = logging.getLogger(__name__)


class AgentWatchApp(App):
    """Textual app showing Claude subagents, agy conversations, and Codex rollouts."""

    TITLE = "agent-watch"

    def __init__(
        self,
        claude_home: Path | str | None = None,
        agy_home: Path | str | None = None,
        codex_home: Path | str | None = None,
        no_quota: bool = False,
        window_hours: int = config.WINDOW_HOURS,
        poll_ms: int = config.POLL_MS,
        proc_provider: Callable[[], list[Any]] | None = None,
    ) -> None:
        super().__init__()
        self.claude_home = Path(claude_home) if claude_home else config.claude_home()
        self.agy_home = Path(agy_home) if agy_home else config.agy_home()
        self.codex_home = Path(codex_home) if codex_home else config.codex_home()
        self.no_quota = no_quota
        self.poll_seconds = poll_ms / 1000
        self.store = Store()
        self.poller = Poller(
            self.store,
            ClaudeScanner(self.claude_home, window_hours),
            AgyScanner(self.agy_home, window_hours, proc_provider),
            CodexScanner(self.codex_home, window_hours, proc_provider),
        )
        self.quota_service = QuotaService()
        self.dashboard = DashboardScreen(self.store)
        self.first_poll_done = False
        self.last_poll_seconds: float | None = None
        self._instance_lock: int | None = None

    # ---- lifecycle ----
    def _release_claim_safely(self) -> None:
        try:
            launcher.release_claim()
        except OSError as exc:
            _logger.warning("Failed to release launch claim: %s", exc)
            self.log.warning(f"Failed to release launch claim: {exc}")

    def on_mount(self) -> None:
        try:
            handle = launcher.acquire_instance_lock_retrying()
        except OSError as exc:
            _logger.warning("Failed to acquire instance lock: %s", exc)
            self.log.warning(f"Failed to acquire instance lock: {exc}")
            self._release_claim_safely()
        else:
            if handle is None:
                self.exit(message="Agent Watch already open")
                return
            self._instance_lock = handle
            try:
                launcher.write_lock()
            except OSError as exc:
                _logger.warning("Failed to write lock file: %s", exc)
                self.log.warning(f"Failed to write lock file: {exc}")
            self._release_claim_safely()
        self.theme = "textual-dark"
        self.sub_title = SCANNING_SUBTITLE
        self.push_screen(self.dashboard)
        # Start the loops only after the dashboard has been shown.
        self.call_after_refresh(self._start_loops)

    def on_unmount(self) -> None:
        if self._instance_lock is None:
            return
        try:
            launcher.release_lock()
        except OSError as exc:
            _logger.warning("Failed to release lock file: %s", exc)
            self.log.warning(f"Failed to release lock file: {exc}")
        finally:
            launcher.release_instance_lock(self._instance_lock)
            self._instance_lock = None

    def _start_loops(self) -> None:
        self.run_worker(self._poll_loop(), name="poll", group="loops")
        if not self.no_quota:
            self.run_worker(self._quota_loop(), name="quota", group="loops")

    # ---- polling ----
    async def _poll_loop(self) -> None:
        """Poll in a worker thread; ticks never overlap because the loop is sequential."""
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            result = await asyncio.to_thread(self.poller.poll_once)
            self.last_poll_seconds = loop.time() - started
            self._apply_poll(result)
            await asyncio.sleep(self.poll_seconds)

    def _apply_poll(self, result: PollResult) -> None:
        if not self.first_poll_done:
            self.first_poll_done = True
        self.sub_title = self.poller.last_error or ""
        if result.changed:
            self.dashboard.refresh_sessions(result.changed)
        if result.events:
            self.dashboard.add_events(result.events)
        if result.codex_limits is not None:
            self.dashboard.update_codex_limits(result.codex_limits)
        if self.screen_stack and isinstance(self.screen, ConversationScreen):
            self.screen.refresh_events()

    # ---- quota ----
    async def _quota_loop(self) -> None:
        while True:
            await self._refresh_quota()
            await asyncio.sleep(QUOTA_INTERVAL_SECONDS)

    async def _refresh_quota(self, force: bool = False) -> None:
        try:
            buckets = await self.quota_service.get(force=force)
            self.dashboard.update_quota(quota.summarize(buckets))
        except Exception as exc:  # noqa: BLE001 - quota is best effort
            self.notify(f"Kota alınamadı: {exc}", severity="warning")

    def on_dashboard_screen_quota_refresh_requested(self, message: DashboardScreen.QuotaRefreshRequested) -> None:
        message.stop()
        if self.no_quota:
            self.notify("Kota kapalı (--no-quota)")
            return
        self.run_worker(self._refresh_quota(force=True), name="quota-refresh")

    # ---- conversation ----
    def _open_conversation(self, agent_key: str) -> None:
        if self.screen_stack and isinstance(self.screen, ConversationScreen):
            return
        self.push_screen(ConversationScreen(self.store, agent_key))

    def on_agent_card_opened(self, message: AgentCard.Opened) -> None:
        message.stop()
        self._open_conversation(message.agent_key)

    def on_agent_list_selected(self, message: AgentList.Selected) -> None:
        message.stop()
        self._open_conversation(message.agent_key)

    # ---- open ----
    def transcript_dir(self, agent_key: str) -> Path | None:
        """Return the transcript folder for an agent key, or None when unknown."""
        kind, _, ident = agent_key.partition(":")
        if kind == "agy" and ident:
            return self.agy_home / "brain" / ident
        if kind == "claude" and ident:
            projects = self.claude_home / "projects"
            for path in projects.glob(f"*/*/subagents/agent-{ident}.jsonl"):
                return path.parent
        return None

    def on_dashboard_screen_open_requested(self, message: DashboardScreen.OpenRequested) -> None:
        message.stop()
        target = self.transcript_dir(message.agent_key)
        if target is None or not target.exists():
            self.notify("Transkript klasörü bulunamadı", severity="warning")
            return
        try:
            _open_folder(target)
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Klasör açılamadı: {exc}", severity="warning")


def _open_folder(path: Path) -> None:
    """Open a folder in the platform file manager."""
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def build_parser() -> argparse.ArgumentParser:
    """Create the command line parser."""
    parser = argparse.ArgumentParser(prog="agent-watch")
    parser.add_argument("--version", action="version", version=f"agent-watch {__version__}")
    parser.add_argument("--claude-home", help="Claude Code home directory (default ~/.claude)")
    parser.add_argument("--agy-home", help="agy home directory (default ~/.gemini/antigravity-cli)")
    parser.add_argument("--codex-home", help="Codex CLI home directory (default ~/.codex)")
    parser.add_argument("--window-hours", type=int, default=config.WINDOW_HOURS,
                        help="only show agy and Codex activity from the last N hours (applies to agy/Codex only)")
    parser.add_argument("--no-quota", action="store_true", help="skip agy /usage quota calls")
    parser.add_argument("--new-window", action="store_true",
                        help="open Agent Watch in a new PowerShell window if not already running")
    parser.add_argument("--auto", action="store_true",
                        help="honor auto-open setting when launching with --new-window")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the agent-watch CLI."""
    args = build_parser().parse_args(argv)
    if args.new_window:
        return launcher.launch(auto=args.auto)
    if args.claude_home:
        os.environ["CLAUDE_HOME"] = args.claude_home
    if args.agy_home:
        os.environ["AGY_HOME"] = args.agy_home
    if args.codex_home:
        os.environ["CODEX_HOME"] = args.codex_home
    AgentWatchApp(
        claude_home=config.claude_home(),
        agy_home=config.agy_home(),
        codex_home=config.codex_home(),
        no_quota=args.no_quota,
        window_hours=args.window_hours,
    ).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
