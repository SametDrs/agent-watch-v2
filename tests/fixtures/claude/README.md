# Claude Code transcript fixtures (anonymized)

Source: a real Claude Code session (cwd `C:\Users\user`, session `9af43e82-5772-4625-a46a-7cffea98c243`), produced by
`make_fixtures.py` (kept outside the repo). Free text (prompts, assistant text, thinking, tool_result text, handbackReport,
task-notification result, Bash descriptions/commands, ai-title, descriptions) is replaced by `lorem ipsum ...` (<= 200 chars).
Kept verbatim: all keys, ids (uuid/parentUuid/agentId/toolUseId), timestamps, model names, usage numbers, tool names, file paths,
type/subtype/status fields. `thinking.signature` removed. `agy` commands keep flags and the literal `agy.exe`; only long quoted
prompt strings are lorem. Descriptions are mapped consistently ("lorem ipsum task description N") across main, meta and notifications.
References between main and subagent files are intact (agentId <-> toolUseId <-> meta.toolUseId). Main-file parentUuid chains are
sparse (lines were dropped) so a parent may not be present: parsers must tolerate dangling parentUuid.

## Files

### session_main.jsonl (~55 lines; <= 150)
Line types: ai-title, user, assistant, queue-operation, system(turn_duration), api-request-blob x2 (< 2 KB, filter test),
file-history-snapshot x2. Expected parse:

Agent tool_use -> start result (`toolUseResult.isAsync=true,status=async_launched`) / completion (`status=completed`):
| toolUseId (tool_use.id) | agentId | subagent_type | mode | start | completion |
|---|---|---|---|---|---|
| toolu_01PWRxJyioDgLutvTm68Qk31 | a5f4272a501937721 | web-fetch | foreground | none (sync) | toolUseResult status=completed, resolvedModel=claude-haiku-5-5, totalToolUseCount=1, totalDurationMs=11877, totalTokens=9879 |
| toolu_013hg661ZCZ6jycjiRYnX6CF | aba1f90b163d55be6 | Explore | background | async_launched, resolvedModel=claude-sonnet-5-5 | queue-operation task-notification (status completed) |
| toolu_0179qLmAWoeeaKwntGKbHbti | a2a3a95ad46e9c54d | Explore | background | same | task-notification |
| toolu_01YGjekGJsW9MvBXUKioo3dQ | ab1f9ef7043b49e4d | Explore | background | same | task-notification |
| toolu_01AWdZfkw8LeZFf5XCgVTZiN | aa0a81ad699cd9adb | Plan | background | async_launched, resolvedModel=claude-opus-5-5 | task-notification |
| toolu_01LWiDomaUF75iuoo6L47dyu | afd80650d75789625 | general-purpose | background | async_launched | none in file (still running) |
| toolu_012PvSH9FJT76Viuv9zYRnpR | ab46604c244453a9c | general-purpose | background | async_launched | none (running) |
| toolu_01Kt4wZ6eyi12qj8gUWSJuLr | a2fc916ae66a68b12 | general-purpose | background | async_launched | none (running; no subagent file in fixtures) |
(`run_in_background:true` in tool_use.input marks background; foreground has no such key.)

queue-operation lines (12): `enqueue` + `dequeue` pairs carrying `<task-notification>` (tags task-id, tool-use-id, output-file, status,
summary, note, result) for ab1f9..., aba1f..., a2a3a..., aa0a8... and one for a Bash background task id `blw34zalr` (task-id is not an
agentId; match against agent ids and ignore otherwise). The web-fetch hand-back appears as `<agent-message from="a5f4272a501937721">` enqueue +
`remove` (reason absorbed_mid_turn): not a task-notification.

agy ShellCalls (tool_use name Bash/PowerShell whose command contains `agy`; ts = line `timestamp`):
| ts | tool | flags |
|---|---|---|
| 2026-11-17T15:37:53.421Z | Bash | `--help` (no -p) |
| 2026-11-17T15:38:23.877Z | Bash | `-p`, `--model gemini-4-hydrogen-medium --mode plan --output-format stream-json --add-dir "." --print-timeout 90s` |
| 2026-11-17T15:39:55.679Z | Bash | `-p "/usage" --output-format json --print-timeout 60s` |
| 2026-11-17T16:03:25.485Z | PowerShell | only `agy-quota.ps1` script name (no agy.exe call; weak match, decide in parser) |
| 2026-11-17T16:04:13.033Z | Bash, run_in_background=true | `--model gemini-4-hydrogen-xhigh --mode accept-edits --add-dir ... --print-timeout 300s` |
| 2026-11-17T16:07:18.249Z / 16:07:21.800Z / 16:07:25.153Z | Bash, run_in_background=true | same flag set (3 parallel runs) |
=> 8 matching tool_use lines (7 real agy.exe invocations + 1 agy-quota.ps1). The background Bash result has `toolUseResult.backgroundTaskId`.

Other: ai-title (`aiTitle` lorem), system `subtype=turn_duration` (durationMs=11006, messageCount=25), assistant lines carry
`message.model`, `message.usage`, `stop_reason`; one has a `thinking` block (no signature).

### subagent_1.jsonl + subagent_1.meta.json  (20 lines) - a5f4272a501937721
web-fetch, FOREGROUND (`requestShape=foreground`), finished=true. Model claude-haiku-5-5 (assistant `message.model`; meta has no `model`,
only `pickedEffortModel=claude-sonnet-5-5`). 2 tool_use (WebFetch, then SubagentHandback), tool_result at the end with
`toolEndsTurn=true`. Status: done. Last action: SubagentHandback. 2026-11-17T15:32:46.256Z -> 15:32:58.121Z.

### subagent_2.jsonl + subagent_2.meta.json  (48 lines) - a2a3a95ad46e9c54d
Explore, BACKGROUND, meta `model=sonnet`, finished=true. Assistant model claude-sonnet-5-5. 9 Bash tool_use, last tool
toolu_01X4nUzUWE3J6UuE9vLUfvCZ followed by a final assistant text with `stop_reason=end_turn` (no SubagentHandback). Status: done.
15:51:10.102Z -> 15:52:09.340Z. Contains attachment, api-request, api-request-shape (shape reduced to `{"truncated": true}`) and api-request-blob lines to filter.

### subagent_3.jsonl + subagent_3.meta.json  (60 lines, truncated copy of a live agent) - afd80650d75789625
general-purpose, BACKGROUND, meta `effort=medium`, NO `finished` key => running. Assistant model claude-sonnet-5-5. 10 Bash tool_use (several
`agy` invocations incl. --model/--mode/--add-dir/--print-timeout/--output-format flags); last line is an `api-request` after the 10th Bash
result, last assistant stop_reason=tool_use. Status: running (no end_turn). 16:04:54.435Z -> 16:06:44.986Z.

### sessions_pid.json
sessions/<pid>.json sample: pid 24116, sessionId 9af43e82-..., cwd `C:\Users\user`, kind interactive, status `busy`, startedAt/updatedAt
epoch ms (1794929238710 / 1794931388624), version 2.1.326. name and messagingSocketPath anonymized. The real dir also holds `<pid>.<hash>.key`
files (ignored; never copied).
