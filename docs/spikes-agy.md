# agy spikes (2026-11-17, model gemini-4-hydrogen-medium, `-p`, `--mode accept-edits`)

7 agy invocations total (1 failed on arg parsing, 1 failed on schema error, 1 killed before init). Scripts: scratchpad `spike-agy/s1.py`, `s2.py` (not in repo).

## S1: live transcript and RUNNING signal

| Question | Finding |
|---|---|
| transcript.jsonl written live? | Yes. One line appended per completed (DONE) step, within ~0.1-0.2 s of the stdout `state:DONE` event. ACTIVE steps are NOT in the file (only DONE). Parallel tool calls produce several lines at the same instant. |
| Line schema | `step_index, source (USER_EXPLICIT\|MODEL), type, status, created_at (UTC Z)`. |
| `type` values | `USER_INPUT` (`content` = `<USER_REQUEST>..`), `PLANNER_RESPONSE` (model turn: `model, input_tokens, cache_read_tokens, output_tokens, thinking?, tool_calls:[{name,args}]` or final `content` text), `GENERIC` (tool result: `content` = "Created At/Completed At ... <result text>"; NO tool name, link to the call by order after the PLANNER_RESPONSE). |
| Tool names (stdout) | `step_type:"tool"`, `tool_name` (`write_file`, `read_file`), `tool_info:{name,parameters:{Path}}`, `duration_seconds` on DONE. Tool args in transcript are JSON-in-string (extra quoting, double-escaped paths). |
| Presence lock | `presence/<convId>.lock`, 0 bytes, created at conversation start (just before `init`, mtime = start). It is NOT removed when the run ends or is killed (dozens of stale locks exist). Not a running signal. |
| conversation_metadata.json | NOT updated by print-mode runs (mtime unchanged, spike conversations absent after run). Not usable for these. Contains other conversations' prompts incl. credentials: never read Preview, never copy raw. |
| Process cmdline | Yes: `agy.exe -p "<full prompt>" --model .. --mode .. --add-dir <dir> --print-timeout .. --output-format ..`; psutil cwd = the launch dir. Single process (no child agy). `--conversation <id>` appears in cmdline when resumed. |
| Init timing | `init` event comes ~3 s after spawn (conversation created, lock + brain dir appear then). |
| Cancel (kill at 9 s) | Transcript just stops: lines 0..2 (user input, planner, first tool result), the in-flight step 3 is never written. No terminal/error marker, no metadata change, lock stays, exit code 1. Kill before init (3 s) creates no conversation at all. |
| Gaps | Normal run between steps: up to ~5 s with no write (model thinking, e.g. steps 5->9 took 4.5 s, final answer ~5 s). A final `PLANNER_RESPONSE` with `content` and no `tool_calls` marks completion of a turn. |

### RUNNING rule (decision)
A conversation is RUNNING iff an `agy.exe` process exists whose cmdline (a) contains `--conversation <id>` for resumed convs, or (b) for new convs: started within the last few seconds of the brain dir creation / `--add-dir`+cwd match and the prompt text equals the transcript line 0 `USER_INPUT` content. Fallback heuristic when no process match: transcript mtime within ~15 s AND last line is not a final PLANNER_RESPONSE (no tool_calls). Otherwise IDLE/DONE; if last line is a PLANNER_RESPONSE with `tool_calls` or a USER_INPUT and no process alive and mtime stale -> ABORTED/CANCELLED. Do not use presence locks or conversation_metadata.json.
Caveat: process-to-conversation mapping for new `-p` convs is heuristic (no id in cmdline); interactive agy processes were not studied (? TUI mode hangs under automation).

## S2: `--input-format stream-json`

Help text: "reads one NDJSON message per line from stdin and runs a turn for each; requires --output-format stream-json".
- `-p` needs `-p=` (empty prompt) when no prompt is given, else it eats `--model`: `-p= --input-format stream-json --output-format stream-json`.
- Schema discovered from error messages + strings in agy.exe, then verified: field `event` is REQUIRED; only `"user"` supported (others: "ignored, unknown event" warning / "not supported yet"). `message` required; `message.content` is a string OR list of `{"type":"text","text":..}` blocks (only "text" supported).
- Rejected: `{"type":"user","message":..}` -> `error: stream input message is missing the "event" field` (result status ERROR, process exits 1); the other candidates fail identically since the process exits after the first error.

WORKING: `{"event":"user","message":{"role":"user","content":"..."}}` (verified both content forms).
- Same process, same `conversation_id` across turns; context kept (turn 2 answered `PINEAPPLE`).
- `init` event is emitted only after the first user message (not at spawn); one `result` per turn (`num_turns` cumulative: 1, 2), `step_index` continues (0.. then 2..).
- Process stays alive between turns (checked at 25 s idle). Closing stdin -> process exits 0 (stderr log "stream input closed after 2 turn(s)" by strings).
- `result.duration_seconds` on turn 2 (29.3) includes idle time, not turn time: do not trust.
- Invalid message -> process exits (not recoverable).

### Chat transport decision
Use one long-lived `agy -p= --input-format stream-json --output-format stream-json --model .. --mode ..` per chat, write `{"event":"user","message":{"role":"user","content":TEXT}}\n` per turn, read until `result` event. Fallback (not needed/tested): `agy -p "<msg>" --conversation <id>`.

## Surprises
- Only DONE steps in transcript; tool results (`GENERIC`) lack tool names.
- Locks never cleaned; metadata not written for print mode.
- Arg parser: `-p` followed by a flag consumes it as the prompt.
- Fixtures: `tests/fixtures/agy/` (`metadata_sample.json` is synthetic, structure only, because spike conversations were never written to metadata and the real file holds unrelated private data). `models.txt`, `usage.*` in that dir were not created by me.
