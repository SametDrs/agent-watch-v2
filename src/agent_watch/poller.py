"""Synchronous polling orchestration: scanners -> linker -> store."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from . import config, linker
from .model import UNLINKED_SESSION_KEY, Event, Source, main_agent_key
from .sources.agy import AgyScanner, AgyScanResult
from .sources.claude import ClaudeScanner, ScanResult
from .sources.codex import CodexLimits, CodexScanner, CodexScanResult
from .store import Store

UNLINKED_NAME = "Unlinked"
STANDALONE_NAME = UNLINKED_NAME
AGY_PREFIX = "agy:"
CODEX_PREFIX = "codex:"


@dataclass
class PollResult:
    """Outcome of one poll round."""

    changed: set[str] = field(default_factory=set)
    events: list[Event] = field(default_factory=list)
    codex_limits: CodexLimits | None = None


class Poller:
    """Runs Claude, agy, and Codex scanners, links conversations, and updates the store."""

    def __init__(
        self,
        store: Store,
        claude_scanner: ClaudeScanner,
        agy_scanner: AgyScanner,
        codex_scanner: CodexScanner | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.claude_scanner = claude_scanner
        self.agy_scanner = agy_scanner
        self.codex_scanner = codex_scanner
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._links: dict[str, linker.Link] = {}
        self._codex_links: dict[str, linker.Link] = {}
        self._live_keys: set[str] = set()
        self.last_error: str | None = None

    def poll_once(self, now: datetime | None = None) -> PollResult:
        """Scan everything once and return changed session keys plus applied events."""
        now = now or self._clock()
        self.last_error = None
        result = PollResult()
        try:
            self._poll(now, result)
        except Exception as exc:  # noqa: BLE001 - polling must never raise
            self.last_error = f"poll: {exc!r}"
        return result

    # ---- steps ----
    def _poll(self, now: datetime, result: PollResult) -> None:
        cr = self._scan_claude(now)
        self._apply_claude(cr, result)

        ar = self._scan_agy(now)
        xr = self._scan_codex(now)
        result.codex_limits = xr.limits

        agy_links = self._link(ar, cr)
        codex_links = self._link_codex(xr, cr)

        if self.store.upsert_session(UNLINKED_SESSION_KEY, name=UNLINKED_NAME):
            result.changed.add(UNLINKED_SESSION_KEY)

        result.changed |= self._relocate(AGY_PREFIX, agy_links)
        result.changed |= self._relocate(CODEX_PREFIX, codex_links)

        running_agents = {f"{AGY_PREFIX}{c}" for c in ar.running} | set(xr.running)
        raw_events = [
            *(self._rewrite(ev, AGY_PREFIX, agy_links) for ev in ar.events),
            *(self._rewrite(ev, CODEX_PREFIX, codex_links) for ev in xr.events),
        ]
        events = [
            ev
            for ev in raw_events
            if ev.session_key != UNLINKED_SESSION_KEY
            or ev.agent_key in running_agents
            or self.store.find_agent(ev.agent_key) is not None
        ]
        result.changed |= self.store.apply(events)
        result.events.extend(events)

        result.changed |= self._relocate(AGY_PREFIX, agy_links)
        result.changed |= self._relocate(CODEX_PREFIX, codex_links)

        if self.store.drop_finished_unlinked(now, running_agents, config.UNLINKED_GRACE_SECONDS):
            result.changed.add(UNLINKED_SESSION_KEY)

        live = set(self._live_keys)
        live |= {agy_links[c].session_key for c in ar.running if c in agy_links}
        live |= {
            codex_links[rid].session_key
            for k in xr.running
            if k.startswith(CODEX_PREFIX) and (rid := k[len(CODEX_PREFIX):]) in codex_links
        }
        result.changed |= self.store.drop_dead_sessions(now, live, config.SESSION_GRACE_SECONDS)

    def _scan_claude(self, now: datetime) -> ScanResult:
        try:
            cr = self.claude_scanner.scan(now)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"claude: {exc!r}"
            return ScanResult(live_session_keys=set(self._live_keys))
        self._live_keys = set(cr.live_session_keys)
        return cr

    def _scan_agy(self, now: datetime) -> AgyScanResult:
        try:
            return self.agy_scanner.scan(now)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"agy: {exc!r}"
            return AgyScanResult()

    def _scan_codex(self, now: datetime) -> CodexScanResult:
        if self.codex_scanner is None:
            return CodexScanResult()
        try:
            return self.codex_scanner.scan(now)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"codex: {exc!r}"
            return CodexScanResult()

    def _apply_claude(self, cr: ScanResult, result: PollResult) -> None:
        for sid in getattr(cr, "reset_session_keys", ()):
            session = self.store.sessions.get(sid)
            if session is None:
                continue
            main_key = main_agent_key(sid)
            for agent_key, agent in list(session.agents.items()):
                if agent.source is Source.CLAUDE and (
                    agent_key == main_key or agent_key.startswith("claude:")
                ):
                    if self.store.reset_agent(agent_key):
                        result.changed.add(sid)
        for info in cr.sessions:
            if self.store.upsert_session(
                info.key, name=info.name, cwd=info.cwd, status=info.status, started=info.started
            ):
                result.changed.add(info.key)
        result.changed |= self.store.apply(cr.events)
        result.events.extend(cr.events)

    def _link(self, ar: AgyScanResult, cr: ScanResult) -> dict[str, linker.Link]:
        convs = [
            linker.AgyConv(
                conv_id=c.conv_id,
                first_input_ts=c.first_input_ts,
                first_input_text=c.first_input_text,
                workspace_paths=list(c.workspace_paths),
            )
            for c in ar.convs
        ]
        try:
            links = linker.link(convs, cr.shell_calls, self._links)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"link: {exc!r}"
            links = {c.conv_id: self._links.get(c.conv_id) or _standalone() for c in convs}
        self._links.update(links)
        return links

    def _link_codex(self, xr: CodexScanResult, cr: ScanResult) -> dict[str, linker.Link]:
        try:
            links = linker.link_codex(xr.convs, cr.shell_calls, self._codex_links)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"codex_link: {exc!r}"
            links = {
                c.rollout_id: self._codex_links[c.rollout_id]
                for c in xr.convs
                if c.rollout_id in self._codex_links
            }
        self._codex_links.update(links)
        return links

    @staticmethod
    def _rewrite(event: Event, prefix: str, links: dict[str, linker.Link]) -> Event:
        """Point an agent event at the session its conversation/rollout is linked to."""
        if not event.agent_key.startswith(prefix):
            return event
        link = links.get(event.agent_key[len(prefix):])
        if link is None or link.session_key == event.session_key:
            return event
        return replace(event, session_key=link.session_key)

    def _relocate(self, prefix: str, links: dict[str, linker.Link]) -> set[str]:
        """Move already-known agents whose link changed and apply link effort."""
        changed: set[str] = set()
        for conv_id, link in links.items():
            agent = self.store.find_agent(f"{prefix}{conv_id}")
            if agent is None:
                continue
            if agent.session_key != link.session_key or agent.parent_agent_key != link.parent_agent_key:
                changed |= self.store.move_agent(agent.key, link.session_key, link.parent_agent_key)
            if link.effort and not agent.effort:
                agent.effort = link.effort
                changed.add(agent.session_key)
        return changed


def _standalone() -> linker.Link:
    return linker.Link(session_key=UNLINKED_SESSION_KEY, parent_agent_key=None, method="standalone")
