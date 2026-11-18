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
- **Başlıklar (`titles.py`):** `TITLE_MAX=60`. Claude alt ajanı: `meta.json` `description` → yoksa ilk isteminden türetilir; agy/Codex: istemden türetilir (baştaki `/komut`, `GOAL:`/`TASK:`… ve `Read and follow the brief in <yol>` atılır; ilk cümle/yan cümle, boşluklar sadeleşir, kelime sınırında `…` ile kesilir). Tam istem PROMPT olayında kalır.
- **Fiyat tablosu (`pricing.py`, USD / 1M token: girdi · önbellekten okunan girdi · çıktı):** kaynak claude-api becerisinin model tablosu (birinci taraf API liste fiyatı). Model kimliği normalleştirilir (büyük/küçük harf, `anthropic.` öneki, `[1m]`, `-high/-low…` çaba eki, `-YYYYMMDD` tarih eki, `5.5` → `5-5`). Tabloda olmayan modelde (Gemini/agy, GPT/Codex, eski Claude 4.5 ve öncesi, takma ad `sonnet`) maliyet yok (`None`). Önbellek okuma oranı tabloda yazılı değilse girdinin 0.1 katı alınır; önbellek yazma ayrı izlenmediği için düz girdi sayılır (hafif eksik tahmin). Haiku 5.5 fiyatı 100K token'a kadar olan istemler içindir.

  | Model | Girdi | Önbellek | Çıktı |
  |---|---|---|---|
  | `claude-fable-5-2`, `claude-mythos-5-2` | 8.00 | 0.20 | 40.00 |
  | `claude-fable-5-1`, `claude-mythos-5-1` | 10.00 | 0.25 | 50.00 |
  | `claude-fable-5`, `claude-mythos-5` | 10.00 | 1.00 | 50.00 |
  | `claude-opus-5-5` | 4.00 | 0.20 | 20.00 |
  | `claude-opus-5`, `-4-8`, `-4-7`, `-4-6` | 5.00 | 0.50 | 25.00 |
  | `claude-sonnet-5-5` | 2.00 | 0.10 | 10.00 |
  | `claude-sonnet-5` | 2.00 | 0.20 | 10.00 |
  | `claude-sonnet-4-6` | 3.00 | 0.30 | 15.00 |
  | `claude-haiku-5-5` | 0.10 | 0.01 | 0.50 |
  | `claude-haiku-4-5` | 1.00 | 0.10 | 5.00 |
