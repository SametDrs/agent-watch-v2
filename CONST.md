# Değişmez gerçekler

- **İsim:** Agent Watch (paket `agent_watch`, komut `agent-watch`, sürüm 0.1.0).
- **Platform:** Windows 11; ayrı PowerShell/cmd penceresinde çalışır, Claude Code'un içinde değil.
- **Dil/çalışma ortamı:** Python `>=3.13`, sanal ortam `.venv`.
- **Çalışma bağımlılıkları:** `textual>=1.0`, `psutil>=6.0` — yenisi eklenmez.
- **Geliştirme bağımlılıkları:** `pytest>=8`, `pytest-asyncio>=0.24`, `pytest-textual-snapshot>=1.0` (`[dependency-groups] dev`).
- **Derleme:** hatchling; giriş noktası `agent_watch.app:main`.
- **Kaynaklar (salt okunur):**
  - Claude: `~/.claude/projects/<slug>/<sid>.jsonl`, `subagents/agent-<id>.jsonl` + `.meta.json`, canlı oturumlar `~/.claude/sessions/<pid>.json`.
  - Codex: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`.
  - agy: `~/.gemini` altındaki brain transcript'leri.
- **Sabitler (`config.py`):** `SESSION_GRACE_SECONDS=60`, `UNLINKED_GRACE_SECONDS=60`, `QUIET_SECONDS=300`, `EVENT_CAP=2000`, `BODY_CAP=4000`, `MASCOT_TICK=0.15`, `CODEX_LINK_WINDOW_SECONDS=10`, `POLL_MS=500`.
- **Maskotlar:** claude → Clawd, agy → Gravi, codex → bulut kafa; 14×10 px tuval, yarım blok karakter; kaynak `docs/superpowers/specs/2026-11-18-card-mascots-reference.py`.
- **Tek örnek:** kilit `%LOCALAPPDATA%\agent-watch\agent-watch.lock` (`{pid, started_at_ms}`); otomatik açılış `AGENT_WATCH_AUTO_OPEN=0` ile kapanır.
- **Remote'lar:** `origin` = private eski repo (push yok); `public` = `SametDrs/agent-watch-v2`.
