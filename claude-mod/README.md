# Agent Watch Claude Code Mod

Automatically opens the Agent Watch window when a Claude Code session starts and provides `/agent-watch` to open it on demand without invoking the model.

## Install

Add this folder to `CLAUDE_CODE_PLUGIN_DIRS` in `~/.claude/settings.json` under `env`, and set the `exe` option under `pluginConfigs`:

```json
{
  "env": {
    "CLAUDE_CODE_PLUGIN_DIRS": "C:\\path\\to\\agent-watch\\claude-mod"
  },
  "pluginConfigs": {
    "agent-watch": {
      "options": {
        "exe": "C:\\path\\to\\agent-watch\\.venv\\Scripts\\agent-watch.exe"
      }
    }
  }
}
```

Notes:
- For a `--plugin-dir` / `CLAUDE_CODE_PLUGIN_DIRS` plugin, `pluginConfigs` accepts `"agent-watch"` (or `"agent-watch@inline"`). You can also edit `exe` in `/config`.
- If `agent-watch.exe` is already on `PATH`, `exe` defaults to `"agent-watch"`.
- To disable auto-open on session start while keeping `/agent-watch`, set `AGENT_WATCH_AUTO_OPEN=0` in `env` or `"auto_open": false` in `%LOCALAPPDATA%\agent-watch\config.json`.

## Fallback (without the mod)

If your Claude Code build does not load function-hook mods via `CLAUDE_CODE_PLUGIN_DIRS`, configure a classic `SessionStart` hook and a custom slash command instead:

1. In `~/.claude/settings.json`, add a `SessionStart` hook:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"C:\\path\\to\\agent-watch\\.venv\\Scripts\\agent-watch.exe\" --new-window --auto"
          }
        ]
      }
    ]
  }
}
```

2. Create `~/.claude/commands/agent-watch.md`:

```markdown
---
description: Open the Agent Watch window
allowed-tools: Bash
---

Run `C:\path\to\agent-watch\.venv\Scripts\agent-watch.exe --new-window` and report its output line.
```
