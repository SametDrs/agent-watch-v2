# Agent Watch v2 — Design

Date: 2026-11-18
Status: draft, awaiting approval
Base: the existing `demo` repo (Textual TUI, 18 commits, 167 passing tests).

## Intent and why

The user works in Claude Code (CLI, Windows 11) and delegates work to subagents: Claude
subagents (Agent tool), Antigravity CLI (`agy -p ...`) and Codex CLI (`codex`). agy and Codex
are launched through the Bash/PowerShell tool, so Claude Code shows them only as a shell
command; the user cannot see what they are doing.

Agent Watch is a **separate terminal window** (PowerShell or cmd) that shows, live, every
subagent of the **currently open** Claude Code session(s): one card per agent, and a
read-only conversation view per agent. It runs beside Claude Code, never inside it, so the
main Claude Code window keeps its full width for the conversation with the main agent —
the user explicitly does not want the main screen narrowed.

## Goals

1. Separate window: `agent-watch` started in its own PowerShell/cmd terminal.
2. Scope: only agents belonging to Claude Code sessions whose process is still running.
   Finished agents of those sessions stay visible (marked done/failed) for the whole life of
   the session. Agents of closed/old sessions are not shown.
3. One card per agent showing: source (claude / agy / codex), model, effort, elapsed time,
   tokens used, status (working / done / failed), current step.
4. Opening a card shows that agent's conversation, read-only, live while it runs, in order:
   - what was written to it (the task prompt, later user/parent messages),
   - what it wrote (text replies, final report),
   - its tool calls (name, arguments such as path/command, truncated results),
   - its thinking, wherever the log contains it as text (see Constraints).
5. Codex support alongside the existing Claude and agy support.
6. Quota display: agy (existing), Codex (`rate_limits`); Claude where obtainable.

## Non-goals

- Managing agents from the TUI: no sending messages, no starting/stopping agents, no model or
  mode pickers. The demo's live agy chat (C1, C3, C4, C5 commits) is removed.
- An in-Claude-Code pane/mod (earlier draft `../../../../docs/superpowers/specs/2026-11-18-agent-watch-design.md`, superseded).
- History of past sessions, analytics, web UI.
- Other OSes.

## Anti-goals

- Any write to `~/.claude`, `~/.codex`, `~/.gemini`, or anything that could block or slow the
  agents (read-only access only).
- Hiding a finished agent while its session is still open.

## Constraints (verified on this machine)

- Thinking text availability is set by each tool's logs, not by us:
  - agy: present as text (`PLANNER_RESPONSE.thinking` in the transcript) → shown.
  - Claude subagents: `thinking` blocks are stored empty (signature only) in all 23 subagent
    logs checked → shown only as a "thought" marker.
  - Codex: reasoning is stored as `encrypted_content`; a plain summary appears rarely →
    summary shown when present, otherwise a "thought" marker.
- Effort sources: Claude subagent `agent-<id>.meta.json` (`"effort"`, `"model"`); Codex rollout
  (`"effort"`, `"model"`); agy: suffix of the `--model` value (`gemini-4-hydrogen-high` → high)
  taken from the launching Bash command (already parsed by `linker.py`) or the agy process
  cmdline.
- Codex rollouts: `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`, appended live;
  carry `function_call`, `custom_tool_call`, `event_msg`, `total_token_usage`,
  `rate_limits.primary {used_percent, window_minutes, resets_at}`.
- The demo's `.venv` has a broken editable install after the folder move (`No module named
  'agent_watch'`); tests pass with `PYTHONPATH=src`. Reinstall is part of the work.

## Approaches considered

- **Build on the demo (chosen).** It already solves agy transcript parsing, Claude↔agy
  linking, process detection (psutil), cards, session tabs and quota, with tests.
- In-Claude-Code pane (mod + collector): rejected by the user — narrows the main screen.
- Rewrite from scratch: rejected — would redo solved, tested work.

## Changes to the demo

1. **Remove management:** `sources/agy_proc.py` (live agy session manager), `ui/chat.py`'s
   input/model picker/mode cycle, `ui/chat_status.py` mode line, the chat-related bindings in
   `app.py`, and their tests. Keep the code paths that parse agy stream output only if the
   read-only view still needs them.
2. **Read-only conversation screen:** replaces the chat screen. Opened with Enter/click on a
   card; Esc returns to the dashboard. Renders the agent's events from the store as a
   transcript (role-labelled blocks for prompt, reply, tool call/result, thinking). Follows
   the tail while running; scrolling up stops following.
3. **Session scope:** a session is shown iff its Claude Code process is alive (existing
   live-session detection in the poller). Replace the 15-min staleness rule
   (`STALE_MINUTES`) for agents of live sessions: they stay until the session ends. Sessions
   that ended disappear (after a short grace period, builder's call). The standalone
   "unlinked agy" bucket is dropped unless the agy run links to a live session.
4. **Cards:** add effort and elapsed time where missing; keep model, tokens, status, current step.
5. **Codex source:** `sources/codex.py` (JSONL tail, events, usage, effort, rate limits);
   linking: extend `linker.py` so Bash/PowerShell commands invoking `codex` link to the
   launching Claude session/subagent, matching by prompt text with a time-window fallback, the
   same way agy links today.
6. **Codex quota** in the quota panel from the newest rollout's `rate_limits`.
7. **Store:** keep the full per-agent event list needed for the conversation screen (cap per
   agent, e.g. last 2,000 events, builder's call).
8. **Packaging:** reinstall the venv (`uv sync` or `pip install -e .`), `agent-watch` command
   starts the TUI. Optional `agent-watch --new-window` helper that opens it in a new
   PowerShell window (builder's call).

## Additions confirmed by the user

- **Main agent card:** each live session tab starts with a card for the main Claude agent of
  that session (model, effort if known, elapsed, tokens, current step, Clawd mascot); opening
  it shows the main conversation read-only, same screen as subagents. Its subagents follow.
- **Auto-open:** when a Claude Code session starts, the Agent Watch window opens by itself in a
  new PowerShell window. Single instance: if a window is already running (it shows all live
  sessions as tabs), nothing new opens. A config switch turns auto-open off.
- **`/agent-watch` in Claude Code:** typing it opens the window (or does nothing if it is
  already open — bringing it to front is a nice-to-have). Preferred mechanism: a tiny Claude
  Code mod in this repo (`claude-mod/`) registering the command with `$.command.register`
  and answering it without a model turn, plus the auto-open on `session.start`, launched via
  `$.process.run` of a launcher script; loaded in every session through
  `CLAUDE_CODE_PLUGIN_DIRS` in `~/.claude/settings.json`. Fallback if the mod route fails: a
  `SessionStart` hook in `~/.claude/settings.json` and a `~/.claude/commands/agent-watch.md`
  command calling the same launcher. The launcher (`agent-watch --new-window`) owns the
  single-instance check (lock file with pid in the run dir).
- **Nested agents:** flat list; a card launched by a subagent carries a small "← <subagent>"
  label.
- **Manually launched agy/Codex** (not from Claude): shown in "Unlinked" while the process
  runs, as decided below.

## Card mascots (approved by the user)

Every card starts with a small animated pixel mascot for its source. The approved sprites,
palette and animations are in `2026-11-18-card-mascots-reference.py` (same folder; a
throwaway spike, run with `python <file>`). Port them, do not redesign them.

- **claude → Clawd:** orange `#d97757` blocky creature, 10×5 px.
- **agy → Gravi (mini):** the demo's blue/violet visor robot with orange antenna, 10×7 px.
- **codex → cloud head:** blue cloud-shaped head only (no body), dark screen face with a light
  cyan `>_`, 10×6 px. Based on a reference image the user supplied.
- Canvas 14×10 px drawn with half-block characters (5 terminal rows), sprite centred, one spare
  row for hops; tick 0.15 s.
- Animations by card status, each mascot with its own variant:
  - idle: periodic blink / look-around / screen flicker / cloud-puff drift, an occasional hop,
    a sleepy "z" after a while; Gravi's antenna pulses slowly.
  - working: bob; Clawd walks, Gravi's thrusters fire and eyes scan, cloud head types `>_`.
  - done: happy face, yellow sparkles, periodic jump (Clawd raises its arms).
  - error: "x"/twitch eyes, periodic shake and red flash.
- Mapping from agent status to mascot state is the builder's (e.g. working → working,
  quiet hint → idle, done → done, failed/aborted → error).
- Rendering must stay cheap: one shared timer drives all visible mascots; cards off-screen do
  not animate.

## Decisions made during review (Claude's calls, user may override)

- **Live session detection:** reuse the demo's existing mechanism — `~/.claude/sessions/<pid>.json`
  checked with `procs.pid_alive(pid, started_at)` (`sources/claude.py`). No new detection.
- **Unlinked agy/Codex runs** (link failed, e.g. prompt passed via stdin/file): never silently
  hidden while running. Shown in an "Unlinked" group while their process is alive, plus the
  grace period; then dropped. Codex time-window fallback: first unclaimed rollout created
  within 10 s of the launching call (same order of magnitude as agy).
- **Working vs stalled:** agy/Codex — working while the process is alive (psutil, as agy
  today); process gone without a final message → failed/aborted. Claude subagent — `meta.json`
  `"finished": true` → done; otherwise working while the session is alive. An agent with no log
  write for > 5 min shows a dim "quiet 5m" hint but keeps its status.
- **Tokens on cards:** cumulative `input + output` (cached input counted in input), plus
  output alone in smaller text; the conversation screen shows the full breakdown
  (input / cached / output / reasoning) where the source provides it.
- **Claude quota:** not in v1 (no local source known). Card tokens only.
- **Startup cost:** parse only logs of live sessions and their agents (existing incremental
  `tail.py`); no full scan of history.
- **Acceptance bar:** every remaining test green, removed-feature tests deleted, new tests
  added as below. `pytest-textual-snapshot` is already a dev dependency.

## Testing

- Keep the existing suite green; remove tests of removed features.
- New tests: Codex parsing from a fixture rollout (prompts replaced with placeholders),
  Codex linking, live-session scoping (finished agents stay, dead sessions go), conversation
  screen rendering (Textual snapshot), effort extraction for all three sources.
- Manual: from one Claude Code session launch one Claude subagent, one agy run and one Codex
  run; watch cards and open each conversation; close the session and confirm its cards go.

## Left to the builder

- Grace period before a closed session disappears.
- Layout details, colours, truncation lengths, event caps.
- Whether the demo's large floating Gravi mascot stays on the dashboard (default: unchanged).
- Exact Codex event-to-step mapping and done/failed rules.
