# Agent Watch v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the demo into a read-only, separate-window watcher of every agent (main Claude, Claude subagents, agy, Codex) of the live Claude Code sessions, with mascot cards, a conversation screen, auto-open and `/agent-watch`.

**Architecture:** Keep the demo's pipeline (scanners → linker → `Store` → Textual screens, polled every 500 ms). Extend the data model so the store keeps a capped per-agent event list with full bodies, add a Codex scanner and Codex linking, scope sessions by live Claude process, then rebuild cards (mascot + stats) and add a conversation screen. A small launcher owns single-instance window opening; a Claude Code mod calls it on session start and on `/agent-watch`.

**Tech Stack:** Python 3.13, Textual ≥1.0, psutil ≥6.0, pytest + pytest-asyncio + pytest-textual-snapshot; Claude Code mod (TypeScript hooks module).

**Spec:** `docs/superpowers/specs/2026-11-18-agent-watch-v2-design.md` (sprites: `docs/superpowers/specs/2026-11-18-card-mascots-reference.py`).

## Global Constraints

- Read-only access to `~/.claude`, `~/.codex`, `~/.gemini`. Never write there; never read or copy `conversation_metadata.json` (contains credentials).
- No management features: no sending messages, no start/stop, no model/mode pickers.
- A finished agent stays visible while its session is live; sessions whose Claude process is gone disappear after `SESSION_GRACE_SECONDS`.
- Python `>=3.13`; no new runtime dependencies beyond `textual>=1.0`, `psutil>=6.0`.
- Test command (all tasks, after Task 1): `.venv/Scripts/python -m pytest -q` from the repo root `C:\Users\user\Desktop\projects\agent-watch\demo`.
- Fixtures copied from real logs must have every prompt/message/tool-result text replaced by placeholders (`"<prompt>"`, `"<text>"`, `"<result>"`).
- New config constants (in `src/agent_watch/config.py`): `SESSION_GRACE_SECONDS = 60`, `UNLINKED_GRACE_SECONDS = 60`, `QUIET_SECONDS = 300`, `EVENT_CAP = 2000`, `BODY_CAP = 4000` (chars), `MASCOT_TICK = 0.15`, `CODEX_LINK_WINDOW_SECONDS = 10`.
- Commit messages: no AI attribution lines.

## Review Focus

1. A log line caught half-written (no trailing newline yet) — must not be parsed or lost; it is read on the next poll once complete. Test in Task 5.
2. A tool result of several MB (e.g. `cat` of a big file) — body truncated to `BODY_CAP`, per-agent events capped at `EVENT_CAP`, UI stays responsive. Test in Task 2.
3. Two Claude Code sessions starting at the same second — exactly one Agent Watch window opens. Test in Task 10.
4. A session resumed (`claude --resume`, same session id, new pid) — its tab reappears with its agents, keyed by session id, not pid. Test in Task 6.
5. Two Codex runs launched in parallel with the same prompt — each links to a distinct rollout (1:1). Test in Task 6.

---

### Task 1: Working venv and removal of management features

**Files:**
- Delete: `src/agent_watch/sources/agy_proc.py`, `src/agent_watch/ui/chat.py`, `src/agent_watch/ui/chat_status.py`, `tests/test_agy_session.py`, `tests/test_chat.py`, `tests/test_chat_status.py`, `tests/fake_agy.py` (only if nothing else imports it)
- Create: `src/agent_watch/runner.py`
- Modify: `src/agent_watch/quota.py:15`, `src/agent_watch/app.py` (chat args, `on_dashboard_screen_chat_requested`, `on_chat_screen_back_requested`, CLI `--cwd`, `--model`), `src/agent_watch/ui/dashboard.py` (`ChatRequested`, binding `c`, `action_chat`), `src/agent_watch/config.py` (`DEFAULT_CHAT_MODEL`, `DEFAULT_CHAT_MODE`, `CHAT_MODES`), `tests/test_quota.py:23`, `tests/test_app.py`

**Interfaces:**
- Produces: `agent_watch.runner.run_oneshot(args: list[str], timeout: float = 30.0) -> tuple[int, str, str]` (moved verbatim from `agy_proc.py:448`, same behaviour).

- [ ] **Step 1:** Reinstall: `py -3.13 -m venv .venv --clear` then `.venv/Scripts/python -m pip install -e ".[dev]"` (if `[dev]` extra does not exist, install the dev deps listed in `pyproject.toml`). Expected: `.venv/Scripts/python -c "import agent_watch"` exits 0 and `.venv/Scripts/agent-watch.exe` exists.
- [ ] **Step 2:** Run the suite: expected `167 passed`.
- [ ] **Step 3:** Write failing tests in `tests/test_app.py`:

```python
def test_cli_has_no_chat_options():
    opts = {a for act in build_parser()._actions for a in act.option_strings}
    assert "--model" not in opts and "--cwd" not in opts

def test_dashboard_has_no_chat_binding():
    assert all(b.key != "c" for b in DashboardScreen.BINDINGS)
```

- [ ] **Step 4:** Move `run_oneshot` to `runner.py`, delete the files listed, strip chat wiring from `app.py`, `dashboard.py`, `config.py`; update imports in `quota.py` and `tests/test_quota.py`.
- [ ] **Step 5:** Run the suite: all pass, `grep -r "agy_proc\|chat_status\|ChatScreen" src tests` finds nothing.
- [ ] **Step 6:** Commit: `git add -A src tests && git commit -m "refactor: remove agy chat/management, move run_oneshot to runner"`

### Task 2: Data model and store for conversations and live scoping

**Files:**
- Modify: `src/agent_watch/model.py`, `src/agent_watch/store.py`, `src/agent_watch/config.py`
- Test: `tests/test_model.py`, `tests/test_store.py`

**Interfaces:**
- Produces (model.py):
  - `Source.CODEX = "codex"`.
  - `EventKind.PROMPT = "prompt"` (text written *to* the agent), `EventKind.THINKING = "thinking"`. `TEXT` = agent reply; `TOOL_START.body` = tool args (JSON text); `TOOL_END.body` = tool result.
  - `Event` new fields: `body: str = ""`, `effort: str | None = None`, `tokens_cached: int = 0`, `tokens_reasoning: int = 0`. Token fields on events are **deltas**.
  - `Agent` new fields: `effort: str = ""`, `is_main: bool = False`, `cached_tokens: int = 0`, `reasoning_tokens: int = 0`, `last_write: datetime | None = None`.
  - `main_agent_key(session_key: str) -> str` → `f"{session_key}:main"` (must not collide with the subagent key format used in `sources/claude.py`).
  - `effort_from_model(model: str) -> str` → last `-` segment if in `{"minimal","low","medium","high","xhigh","max"}`, else `""`.
  - `UNLINKED_SESSION_KEY = STANDALONE_SESSION_KEY` kept; display name becomes `"Unlinked"` (poller sets it, Task 6).
- Produces (store.py):
  - `Store.events(agent_key: str) -> list[Event]` — ordered, last `config.EVENT_CAP` events of that agent; each `body` already cut to `config.BODY_CAP` chars (suffix `"…"` when cut).
  - `Store.apply` additionally: sets `agent.effort` from `event.effort` when non-empty, adds `tokens_cached`/`tokens_reasoning`, sets `agent.last_write = event.ts`.
  - `Store.drop_dead_sessions(now: datetime, live_session_keys: set[str], grace_seconds: int) -> set[str]` — removes a non-live session (never `UNLINKED_SESSION_KEY`) once it has been continuously non-live for `grace_seconds`; a session that becomes live again before that is kept; returns removed keys.
  - `Store.drop_finished_unlinked(now: datetime, running_agent_keys: set[str], grace_seconds: int) -> set[str]` — removes agents of `UNLINKED_SESSION_KEY` not in `running_agent_keys` for `grace_seconds`.
  - `Store.mark_stale` and `config.STALE_MINUTES` are deleted (with their tests).

- [ ] **Step 1: Write failing tests**

```python
def test_effort_from_model():
    assert effort_from_model("gemini-4-hydrogen-xhigh") == "xhigh"
    assert effort_from_model("gpt-5.6-terra") == ""

def test_events_capped_and_bodies_truncated(store):
    big = "x" * 5_000_000
    store.apply([ev(kind=EventKind.TOOL_END, body=big) for _ in range(config.EVENT_CAP + 10)])
    evs = store.events(KEY)
    assert len(evs) == config.EVENT_CAP
    assert len(evs[-1].body) <= config.BODY_CAP + 1 and evs[-1].body.endswith("…")

def test_effort_and_tokens_accumulate(store):
    store.apply([ev(effort="high", tokens_in=10, tokens_cached=4, tokens_out=3, tokens_reasoning=2),
                 ev(tokens_in=5, tokens_out=1)])
    a = store.find_agent(KEY)
    assert (a.effort, a.in_tokens, a.cached_tokens, a.out_tokens, a.reasoning_tokens) == ("high", 15, 4, 4, 2)

def test_dead_session_dropped_after_grace_only(store):
    store.upsert_session("s1")
    assert store.drop_dead_sessions(T0, set(), 60) == set()
    assert store.drop_dead_sessions(T0 + timedelta(seconds=30), {"s1"}, 60) == set()   # came back
    assert store.drop_dead_sessions(T0 + timedelta(seconds=31), set(), 60) == set()
    assert store.drop_dead_sessions(T0 + timedelta(seconds=92), set(), 60) == {"s1"}

def test_finished_agent_kept_while_session_live(store):
    store.apply([ev(kind=EventKind.END, status=Status.DONE)])
    store.drop_dead_sessions(T0 + timedelta(hours=5), {SESSION}, 60)
    assert store.find_agent(KEY).status is Status.DONE
```

- [ ] **Step 2:** Run `pytest tests/test_model.py tests/test_store.py -q`: FAIL (names undefined).
- [ ] **Step 3:** Implement the interfaces above; keep events in `dict[str, collections.deque[Event]]` with `maxlen=config.EVENT_CAP`; truncate in `apply` before storing. Delete `mark_stale`, `STALE_MINUTES` and their tests; fix `poller.py` call site temporarily to `drop_dead_sessions(now, live, config.SESSION_GRACE_SECONDS)`.
- [ ] **Step 4:** Run the suite: all pass.
- [ ] **Step 5:** Commit: `feat: per-agent event history, effort, live-session scoping in store`

### Task 3: Claude source — live sessions only, main agent, conversation bodies, effort

**Files:**
- Modify: `src/agent_watch/sources/claude.py`
- Test: `tests/test_claude.py` (+ fixtures under `tests/fixtures/claude/` as needed)

**Interfaces:**
- Consumes: Task 2 model fields, `main_agent_key`.
- Produces: `ClaudeScanner.scan()` events for the main agent (`agent_key=main_agent_key(session_key)`, `Agent.is_main=True`, title `"main"`), and for subagents, with:
  - user string content / text blocks → `PROMPT` (body = text); for subagents the first user message is the task prompt;
  - assistant text blocks → `TEXT` (body = text);
  - assistant `thinking` blocks → `THINKING` (body = thinking text, `""` when stored empty);
  - `tool_use` → `TOOL_START` (summary from `summarize_tool`, body = `json.dumps(input)`);
  - `tool_result` → `TOOL_END` (body = result text; list content joined);
  - assistant `usage` → `USAGE` deltas (`tokens_in` = input + cache_read + cache_creation, `tokens_cached` = cache_read, `tokens_out` = output);
  - model from `message.model`; subagent `effort`/`model` from `agent-<id>.meta.json`; `"finished": true` → `END` with `Status.DONE`.
  - Main agent effort: from the main transcript if a record carries an effort field, else `""` (card shows no effort).
- Discovery reads only transcripts and subagent logs of sessions in `live_session_keys` (the window-based scan of non-live sessions is removed). Live detection is unchanged (`sessions/*.json` + `pid_alive`).

- [ ] **Step 1: Write failing tests** (fixture: one live session JSONL with user prompt, assistant text+thinking+tool_use, tool_result, usage; one subagent JSONL + meta.json with `"effort":"low"`, `"model":"claude-opus-..."`, `"finished":true`; one non-live session file):

```python
def test_main_agent_events(scan):
    kinds = [e.kind for e in scan.events if e.agent_key == main_agent_key(SID)]
    assert kinds[:5] == [EventKind.PROMPT, EventKind.THINKING, EventKind.TEXT, EventKind.TOOL_START, EventKind.TOOL_END]

def test_tool_bodies(scan):
    start = next(e for e in scan.events if e.kind is EventKind.TOOL_START)
    assert json.loads(start.body) == {"command": "<cmd>"}

def test_subagent_effort_model_finished(scan):
    sub = [e for e in scan.events if e.agent_key == SUB_KEY]
    assert any(e.effort == "low" for e in sub)
    assert sub[-1].kind is EventKind.END and sub[-1].status is Status.DONE

def test_non_live_session_not_scanned(scan):
    assert all(e.session_key != DEAD_SID for e in scan.events)
```

- [ ] **Step 2:** Run `pytest tests/test_claude.py -q`: new tests FAIL.
- [ ] **Step 3:** Implement: pass the main agent key instead of `None` in `_handle_main_inner` (`claude.py:351-355`) and emit the kinds above from `_assistant`/`_tool_use`/`_user`; restrict `_discover` to live sids.
- [ ] **Step 4:** Run the suite: all pass.
- [ ] **Step 5:** Commit: `feat(claude): main agent card data, conversation bodies, effort, live-only scan`

### Task 4: agy source — conversation bodies and effort

**Files:**
- Modify: `src/agent_watch/sources/agy.py` (`parse_transcript_line` at `:129`), `src/agent_watch/poller.py` (effort from link)
- Test: `tests/test_agy.py`, `tests/test_poller.py`

**Interfaces:**
- Consumes: `effort_from_model`, `parse_agy_command(cmd)["model"]` (`linker.py:276`).
- Produces: `parse_transcript_line` emits `PROMPT` (user request, body via `strip_user_request`), `THINKING` (`PLANNER_RESPONSE.thinking`), `TEXT` (planner response text), `TOOL_START`/`TOOL_END` with bodies; `effort` = `effort_from_model(model)` where model comes from the transcript or, failing that, from the linked call's `--model` (poller attaches it when the link is made).

- [ ] **Step 1: Write failing tests**

```python
def test_planner_thinking_and_prompt():
    evs = parse_transcript_line(PLANNER_WITH_THINKING, "c1", "s1") + parse_transcript_line(USER_REQUEST, "c1", "s1")
    assert {e.kind for e in evs} >= {EventKind.THINKING, EventKind.TEXT, EventKind.PROMPT}
    assert next(e for e in evs if e.kind is EventKind.THINKING).body == "<thinking>"

def test_linked_agy_gets_effort_from_command(poller_with_agy_call):  # call: agy -p "<prompt>" --model gemini-4-hydrogen-high
    poller_with_agy_call.poll_once()
    assert poller_with_agy_call.store.find_agent("agy:c1").effort == "high"
```

- [ ] **Step 2:** Run: FAIL. **Step 3:** Implement. **Step 4:** Suite green.
- [ ] **Step 5:** Commit: `feat(agy): conversation bodies, thinking and effort`

### Task 5: Codex source

**Files:**
- Create: `src/agent_watch/sources/codex.py`, `tests/test_codex.py`, `tests/fixtures/codex/rollout-sample.jsonl`
- Modify: `src/agent_watch/procs.py` (add `codex_processes`), `src/agent_watch/config.py` (`codex_home() -> Path`, env `CODEX_HOME`, default `~/.codex`; `codex_sessions_dir() -> Path`)

**Interfaces:**
- Produces:
  - `procs.codex_processes() -> list[ProcInfo]` — names `codex`, `codex.exe` (and `node` processes whose cmdline contains a `codex` script path).
  - `@dataclass CodexConv(rollout_id: str, path: Path, created: datetime, first_prompt: str, cwd: str, model: str, effort: str)`.
  - `@dataclass CodexLimits(used_percent: float, window_minutes: int, resets_at: datetime | None, secondary_used_percent: float | None = None)`.
  - `@dataclass CodexScanResult(events: list[Event], convs: list[CodexConv], running: set[str], limits: CodexLimits | None)`.
  - `parse_rollout_line(d: dict, rollout_id: str, session_key: str, state: dict) -> list[Event]` — `state` holds the last cumulative `total_token_usage` so `USAGE` events carry deltas.
  - `class CodexScanner: __init__(self, codex_home: Path | None = None, window_hours: int = config.WINDOW_HOURS, proc_provider: Callable[[], list[ProcInfo]] | None = None)`; `scan(self, now: datetime | None = None) -> CodexScanResult`. Agent keys: `f"codex:{rollout_id}"`; initial `session_key = UNLINKED_SESSION_KEY` (the poller moves it on link).
- Mapping (verify each record shape against the fixture; shapes marked ? are from memory): session meta / `turn_context` → model, effort, cwd (`START`); user message → `PROMPT`; `agent_message` / assistant message → `TEXT`; `reasoning` with summary text → `THINKING` body, encrypted only → `THINKING` body `""`; `function_call` / `custom_tool_call` → `TOOL_START` (body = arguments); `function_call_output` → `TOOL_END`; `event_msg` `token_count` → `USAGE` delta (`input_tokens` incl. cached → `tokens_in`, `cached_input_tokens` → `tokens_cached`, `output_tokens` → `tokens_out`, `reasoning_output_tokens` → `tokens_reasoning`) and `rate_limits.primary` → `limits`; task-complete event → `END` `DONE`.
- Status: `RUNNING` while a Codex process is alive and the rollout was written in the last 15 s or the process cmdline contains the rollout's prompt prefix; process gone and last event is task-complete → `DONE`; otherwise `FAILED` (`END` emitted once).
- Scans rollouts under `sessions/YYYY/MM/DD/` for today and yesterday (UTC and local) only, using `JsonlTail`.

- [ ] **Step 1:** Build the fixture: copy the newest real rollout, replace all user/assistant/tool text with placeholders, keep token counts, model, effort, rate limits; keep ≤60 lines. Check by eye that no prompt text remains.
- [ ] **Step 2: Write failing tests**

```python
def test_fixture_parses(codex_scan):
    kinds = {e.kind for e in codex_scan.events}
    assert {EventKind.PROMPT, EventKind.TEXT, EventKind.TOOL_START, EventKind.TOOL_END, EventKind.USAGE} <= kinds
    conv = codex_scan.convs[0]
    assert conv.model == "gpt-5.6-terra" and conv.effort == "medium"

def test_usage_deltas_sum_to_last_total(codex_scan):
    use = [e for e in codex_scan.events if e.kind is EventKind.USAGE]
    assert sum(e.tokens_out for e in use) == LAST_TOTAL_OUTPUT

def test_rate_limits(codex_scan):
    assert codex_scan.limits.window_minutes > 0 and 0 <= codex_scan.limits.used_percent <= 100

def test_partial_line_waits_for_newline(tmp_codex_home):           # Review Focus 1
    path = write_rollout(tmp_codex_home, FIXTURE_LINES[:5] + [FIXTURE_LINES[5][:30]])  # no trailing \n
    sc = CodexScanner(tmp_codex_home, proc_provider=lambda: [])
    n1 = len(sc.scan().events)
    append(path, FIXTURE_LINES[5][30:] + "\n")
    assert len(sc.scan().events) > 0 and n1 > 0

def test_process_gone_without_completion_is_failed(...): ...      # status FAILED, END once
```

- [ ] **Step 3:** Run `pytest tests/test_codex.py -q`: FAIL. **Step 4:** Implement. **Step 5:** Suite green.
- [ ] **Step 6:** Commit: `feat(codex): rollout scanner with usage, effort, rate limits`

### Task 6: Codex linking and poller integration (scoping, unlinked group, nesting)

**Files:**
- Modify: `src/agent_watch/linker.py`, `src/agent_watch/poller.py`, `src/agent_watch/app.py` (construct `CodexScanner`, CLI `--codex-home`)
- Test: `tests/test_linker.py`, `tests/test_poller.py`

**Interfaces:**
- Consumes: `CodexScanner`, `CodexConv` (Task 5), `Store.drop_dead_sessions` / `drop_finished_unlinked` (Task 2).
- Produces:
  - `is_codex_command(cmd: str) -> bool` — `codex` / `codex.exe` / `npx codex` followed by `exec` / `e` or a positional prompt; same splitting as `is_agy_command` (`linker.py:123`).
  - `parse_codex_command(cmd: str) -> dict[str, Any]` → `{"prompt": str, "model": str | None (-m/--model), "effort": str | None (-c model_reasoning_effort=...), "cwd": str | None (-C/--cd)}`.
  - `link_codex(convs: list[CodexConv], calls: list[ShellCall], existing: dict[str, Link] | None = None) -> dict[str, Link]` — score rule as `link()` (`+100` for ≥20-char shared normalized prompt prefix, `- abs(dt)`), greedy 1:1; fallback: first unclaimed rollout with `created` within `config.CODEX_LINK_WINDOW_SECONDS` after the call's `ts_start`; unmatched → no link.
  - `Poller.__init__(self, store, claude_scanner, agy_scanner, codex_scanner: CodexScanner | None = None, clock=None)`.
  - `PollResult` gains `codex_limits: CodexLimits | None = None`.
  - Poller: linked agy/Codex agents move to the call's session with `parent_agent_key` = the calling subagent (or the main agent key when the call came from the main transcript); unlinked ones stay in `UNLINKED_SESSION_KEY` (name `"Unlinked"`) while running, dropped by `drop_finished_unlinked(now, running, config.UNLINKED_GRACE_SECONDS)`; sessions dropped by `drop_dead_sessions(now, live, config.SESSION_GRACE_SECONDS)`. A link's effort fills the agent's effort if the source did not provide one.

- [ ] **Step 1: Write failing tests**

```python
def test_parse_codex_command():
    p = parse_codex_command('codex exec -m gpt-5.6-terra -c model_reasoning_effort="high" -C D:\\x "fix the bug in foo"')
    assert p == {"prompt": "fix the bug in foo", "model": "gpt-5.6-terra", "effort": "high", "cwd": "D:\\x"}
    assert not is_codex_command("echo codex")

def test_parallel_same_prompt_links_one_to_one():                  # Review Focus 5
    # same prompt, calls t1 (agent A) and t2 (agent B); rollouts created 1 s after each call
    links = link_codex([conv("r1", T0 + 1s), conv("r2", T0 + 2s)], [call("t1", T0, agent="A"), call("t2", T0 + 1s, agent="B")])
    assert set(links) == {"r1", "r2"}
    assert {links["r1"].parent_agent_key, links["r2"].parent_agent_key} == {"A", "B"}

def test_time_window_fallback_when_prompt_from_file():
    links = link_codex([conv("r1", T0 + 4s, first_prompt="<other>")], [call("t1", T0, cmd="codex exec - < task.md")])
    assert links["r1"].method == "window"

def test_codex_card_under_calling_subagent(poller): ...            # parent_agent_key == subagent key

def test_unlinked_codex_shown_while_running_then_dropped(poller): ...

def test_resumed_session_reappears(poller):                         # Review Focus 4
    # session id S live (pid 1) → dead > grace → dropped → sessions/2.json with same sessionId, pid alive
    assert "S" in poller.store.sessions and poller.store.find_agent(main_agent_key("S"))
```

- [ ] **Step 2:** Run: FAIL. **Step 3:** Implement. **Step 4:** Suite green.
- [ ] **Step 5:** Commit: `feat: link codex runs, live-session scoping, unlinked group`

### Task 7: Card mascots module

**Files:**
- Create: `src/agent_watch/ui/card_mascot.py`, `tests/test_card_mascot.py`

**Interfaces:**
- Produces:
  - `CANVAS_W = 14`, `CANVAS_H = 10`, `PALETTE: dict[str, str]` — copied from `PAL` in the reference file.
  - `sprite(source: Source, state: str, tick: int) -> list[str]` — ports `clawd` / `gravi` / `codex` from the reference file unchanged.
  - `compose(rows: list[str], state: str, tick: int) -> list[str]` — ported unchanged.
  - `render(canvas: list[str]) -> rich.text.Text` — 5 lines × 14 cells, half-blocks `▀`/`▄` with Rich `Style(color=..., bgcolor=...)` instead of ANSI escapes.
  - `mascot_state(agent: Agent, now: datetime) -> str` — `RUNNING` and `last_write` within `config.QUIET_SECONDS` → `"working"`; `RUNNING` quiet or `PENDING` → `"idle"`; `DONE` → `"done"`; `FAILED`/`STALE` → `"error"`.

- [ ] **Step 1: Write failing tests**

```python
@pytest.mark.parametrize("src", list(Source))
@pytest.mark.parametrize("state", ["idle", "working", "done", "error"])
def test_frames_valid(src, state):
    for t in range(80):
        c = compose(sprite(src, state, t), state, t)
        assert len(c) == CANVAS_H and all(len(r) == CANVAS_W for r in c)
        assert set("".join(c)) <= set(PALETTE) | {"."}
        assert len(render(c).split()) == CANVAS_H // 2

def test_sprite_sizes():
    assert (len(sprite(Source.CLAUDE, "idle", 0)), len(sprite(Source.AGY, "idle", 0)), len(sprite(Source.CODEX, "idle", 0))) == (5, 7, 6)

def test_mascot_state_mapping(): ...   # the four mappings above
```

- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** Suite green.
- [ ] **Step 5:** Commit: `feat(ui): card mascots (Clawd, Gravi mini, Codex cloud head)`

### Task 8: Cards, grid and quota panel

**Files:**
- Modify: `src/agent_watch/ui/widgets.py` (`AgentCard` `:55-122`, `CardGrid` `:124-160`, `QuotaPanel` `:260`), `src/agent_watch/ui/dashboard.py`, `src/agent_watch/ui/theme.py`
- Test: `tests/test_widgets.py`, `tests/test_dashboard.py`

**Interfaces:**
- Consumes: Task 7 `sprite`/`compose`/`render`/`mascot_state`; `PollResult.codex_limits`.
- Produces:
  - `AgentCard` layout (height 7 incl. border, width ≈ 52): left the 14×5 mascot, right 5 text lines:
    1. bold title (`"main"` for the main agent) + dim `"← <parent title>"` when `parent_agent_key` is a subagent;
    2. `source · short_model · effort` (effort omitted when empty);
    3. `elapsed · <in+out> tok` + dim `(<out> out)`;
    4. status icon + `last_action`;
    5. dim `"quiet 5m"` (minutes since `last_write`) when `RUNNING` and quiet ≥ `QUIET_SECONDS`, else blank.
  - `AgentCard.Opened(Message)` with `agent_key: str`, posted on click and on Enter while focused (cards are focusable).
  - `CardGrid.set_agents` orders the main agent first, then others by `started`.
  - One `set_interval(config.MASCOT_TICK)` timer on `CardGrid` advances a shared tick and redraws only cards whose region intersects the visible scroll region.
  - `QuotaPanel.update_quota(data, codex: CodexLimits | None = None)` adds a `codex  <used>% used · resets HH:MM` line.

- [ ] **Step 1: Write failing tests**

```python
def test_card_lines(agent_running_opus_low):
    t = AgentCard(agent_running_opus_low).render_agent(NOW).plain
    assert "opus" in t and "low" in t and "tok" in t and "out)" in t

def test_quiet_hint():
    a = running_agent(last_write=NOW - timedelta(minutes=6))
    assert "quiet 6m" in AgentCard(a).render_agent(NOW).plain

def test_nested_label():
    assert "← Explore" in AgentCard(agy_agent, parent_title="Explore").render_agent(NOW).plain

async def test_main_card_first(app_pilot): ...      # first card key == main_agent_key(session)
async def test_offscreen_cards_not_redrawn(app_pilot): ...   # tick advances; hidden card's render count unchanged
def test_codex_quota_line(): ...                    # "codex" and "% used" in QuotaPanel text
```

- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** Suite green (update existing card/dashboard tests to the new layout; `AgentList` sidebar stays).
- [ ] **Step 5:** Commit: `feat(ui): mascot cards with effort, elapsed, tokens; codex quota`

### Task 9: Read-only conversation screen

**Files:**
- Create: `src/agent_watch/ui/conversation.py`, `tests/test_conversation.py`, `tests/__snapshots__/` (generated)
- Modify: `src/agent_watch/app.py` (handle `AgentCard.Opened`, `AgentList` Enter), `src/agent_watch/ui/dashboard.py`

**Interfaces:**
- Consumes: `Store.events(agent_key)`, `Store.find_agent`, `AgentCard.Opened`.
- Produces:
  - `render_event(ev: Event) -> RenderableType` — role-labelled block: `PROMPT` "▶ to agent", `TEXT` "◀ agent" (Markdown), `THINKING` "… thinking" with body, or dim `"(thought — text not stored by this tool)"` when body is empty, `TOOL_START` "⚙ <summary>" + args body (first 500 chars), `TOOL_END` "↳ result" (first 800 chars), `ERROR` red, `END` status line. `USAGE`/`START` not rendered.
  - `ConversationScreen(Screen)`: `__init__(self, store: Store, agent_key: str)`; header = card fields + full token breakdown (in / cached / out / reasoning); body appends new events on each poll (`refresh_events(self) -> None`, called by the app after each poll); follows the tail while scrolled to bottom, stops following when the user scrolls up, resumes at bottom; `escape` pops. No input widgets.

- [ ] **Step 1: Write failing tests**

```python
def test_empty_thinking_marker():
    c = Console(record=True, width=80); c.print(render_event(ev(kind=EventKind.THINKING, body="")))
    assert "text not stored" in c.export_text()

async def test_opens_on_enter_and_closes_on_escape(app_pilot):
    await pilot.click(AgentCard)                 # or focus + "enter"
    assert isinstance(app.screen, ConversationScreen)
    await pilot.press("escape"); assert isinstance(app.screen, DashboardScreen)

async def test_no_input_widgets(app_pilot):
    assert not app.screen.query("Input, TextArea")

async def test_follows_tail_until_scrolled_up(app_pilot): ...

def test_snapshot(snap_compare):
    assert snap_compare(APP_WITH_FIXTURE, press=["enter"])
```

- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** Suite green; review the generated snapshot SVG by eye.
- [ ] **Step 5:** Commit: `feat(ui): read-only conversation screen`

### Task 10: Launcher — new window, single instance, auto-open switch

**Files:**
- Create: `src/agent_watch/launcher.py`, `src/agent_watch/__main__.py`, `tests/test_launcher.py`
- Modify: `src/agent_watch/app.py` (`main`: `--new-window`, `--auto`; TUI writes/removes the lock on mount/unmount)

**Interfaces:**
- Produces:
  - `run_dir() -> Path` → `%LOCALAPPDATA%\agent-watch` (created on demand).
  - `lock_path() -> Path` → `run_dir() / "agent-watch.lock"`, JSON `{"pid": int, "started_at_ms": int}`.
  - `instance_running(path: Path | None = None) -> bool` — lock exists and `procs.pid_alive(pid, started_at_ms)`.
  - `write_lock(path: Path | None = None) -> None`, `release_lock(path: Path | None = None) -> None` (removes only if it holds this pid).
  - `try_claim_launch(now: float, path: Path | None = None) -> bool` — atomic `os.open(run_dir()/"launch.claim", O_CREAT | O_EXCL)`; an existing claim older than 15 s is replaced; returns False if another launcher holds it.
  - `auto_open_enabled() -> bool` — env `AGENT_WATCH_AUTO_OPEN=0` → False; else `run_dir()/"config.json"` key `"auto_open"` (default True).
  - `open_new_window() -> None` — `subprocess.Popen(["powershell.exe", "-NoProfile", "-Command", f"& '{sys.executable}' -m agent_watch"], creationflags=subprocess.CREATE_NEW_CONSOLE)`, window title `"Agent Watch"`, returns immediately (must not wait: the mod's `$.process.run` blocks until the launcher exits).
  - CLI: `agent-watch --new-window [--auto]` → exit 0 without opening if `--auto` and auto-open is off, or `instance_running()`, or `not try_claim_launch()`; else `open_new_window()`; prints one line (`"Agent Watch opened"` / `"Agent Watch already open"` / `"auto-open off"`). Plain `agent-watch` runs the TUI as before.

- [ ] **Step 1: Write failing tests** (monkeypatch `run_dir` to `tmp_path`, `open_new_window` to a recorder):

```python
def test_second_launch_while_running_opens_nothing(...):
    write_lock(); main(["--new-window"]); assert opened == []

def test_two_simultaneous_launchers_open_one_window(...):         # Review Focus 3
    assert [try_claim_launch(now), try_claim_launch(now)] == [True, False]

def test_stale_claim_replaced(...):
    try_claim_launch(0); assert try_claim_launch(16)

def test_auto_off(monkeypatch, ...):
    monkeypatch.setenv("AGENT_WATCH_AUTO_OPEN", "0"); main(["--new-window", "--auto"]); assert opened == []

def test_release_lock_keeps_foreign_lock(...): ...
```

- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** Suite green; manual: `.venv/Scripts/agent-watch.exe --new-window` opens one PowerShell window with the TUI; running it again prints `Agent Watch already open`.
- [ ] **Step 5:** Commit: `feat: single-instance launcher with new-window and auto-open switch`

### Task 11: Claude Code mod — auto-open and `/agent-watch`

**Files:**
- Create: `claude-mod/.claude-plugin/plugin.json`, `claude-mod/hooks/hooks.json`, `claude-mod/hooks/register.ts`, `claude-mod/hooks/register.test.ts`, `claude-mod/README.md`

**Interfaces:**
- Consumes: the launcher CLI (Task 10).
- Produces:
  - `plugin.json`: `{ "name": "agent-watch", "version": "0.1.0", "description": "Opens the Agent Watch window" }` plus a `userConfig` string option `exe` (path to `agent-watch.exe`, default `"agent-watch"`).
  - `session.start` hook: `await next(e)`, then `$.command.register({ name: "agent-watch", description: "Open the Agent Watch window" })`, then fire-and-forget `$.process.run([exe, "--new-window", "--auto"], { timeoutMs: 15000 })` (errors logged to debug only, never thrown).
  - `command.run` hook for `agent-watch`: runs `$.process.run([exe, "--new-window"])` and returns `{ text: <launcher's stdout line> }` without calling `next` (no model turn).
- The builder loads the `plugin-authoring` skill first and checks exact names/shapes (`command.run` matcher, `userConfig` read) in the types file it names.

- [ ] **Step 1:** Write `register.test.ts`: session start registers `agent-watch` and runs argv ending in `["--new-window", "--auto"]`; `/agent-watch` returns text from a stubbed run and makes no model call.
- [ ] **Step 2:** Run `claude plugin test claude-mod`: FAIL. **Step 3:** Implement.
- [ ] **Step 4:** `claude plugin validate claude-mod` clean; `tsc -p claude-mod` clean; `claude plugin test claude-mod` passes.
- [ ] **Step 5:** Write `README.md`: install by adding the folder to `CLAUDE_CODE_PLUGIN_DIRS` in `~/.claude/settings.json` `env`, set `exe` to `C:\...\demo\.venv\Scripts\agent-watch.exe`; fallback (if the mod route fails): `SessionStart` hook running `agent-watch.exe --new-window --auto` and `~/.claude/commands/agent-watch.md` running `agent-watch.exe --new-window`.
- [ ] **Step 6:** Commit: `feat: claude code mod for auto-open and /agent-watch`
- [ ] **Step 7 (manager, needs the user's OK):** edit `~/.claude/settings.json` as the README says — this is the only write to `~/.claude`, made by setup, not by Agent Watch.

### Task 12: Manual acceptance

- [ ] Start a new Claude Code session: the Agent Watch window opens by itself; a second session does not open a second window and appears as a new tab.
- [ ] Type `/agent-watch` with the window closed: it opens; with it open: "already open".
- [ ] From one session launch one Claude subagent, one `agy -p` run and one `codex exec` run (one of them from inside the subagent): four cards (main first), correct mascots and animations, effort shown, nested card shows "← <subagent>".
- [ ] Open each card: prompt, replies, tool calls, thinking (agy text; Claude/Codex marker or summary) appear live; Esc returns.
- [ ] Finished agents stay; close the session: its tab goes after ~60 s.
- [ ] Run `codex exec` by hand in another terminal: it shows under "Unlinked" while running, then leaves.
- [ ] Full suite green: `.venv/Scripts/python -m pytest -q`.
