# Agent Watch — proje haritası

Canlı Claude Code oturumlarındaki tüm ajanları (ana Claude, Claude alt ajanları, agy, Codex) ayrı bir terminal penceresinde kart + salt okunur konuşma olarak gösteren Textual TUI.

## Harita
- `src/agent_watch/sources/` — log okuyucular: `claude.py`, `agy.py`, `codex.py`; `tail.py` artımlı JSONL okuma.
- `src/agent_watch/linker.py` — Bash/PowerShell komutlarını agy/Codex çalıştırmalarına bağlar.
- `src/agent_watch/poller.py` — tarayıcılar → linker → `store.py` (500 ms döngü).
- `src/agent_watch/model.py`, `store.py` — Event/Agent/Session modeli, ajan başına sınırlı olay listesi.
- `src/agent_watch/ui/` — dashboard, kartlar, maskotlar, konuşma ekranı.
- `src/agent_watch/launcher.py` — `--new-window`, tek örnek kilidi.
- `claude-mod/` — Claude Code modu: oturum başında otomatik açılış, `/agent-watch` komutu.
- `docs/superpowers/` — tasarım (specs) ve uygulama planı (plans).

## Kurallar
- `~/.claude`, `~/.codex`, `~/.gemini` yalnızca okunur; `conversation_metadata.json` asla okunmaz.
- Yönetim özelliği yok (mesaj gönderme, başlat/durdur, model seçici).
- Test: `.venv/Scripts/python -m pytest -q` (repo kökünden).
- Commit mesajlarında AI attribution satırı yok. Repo public: gizli bilgi ve kişisel yol commit'leme.
- Sabitler `CONST.md`, yarım işler `BACKLOG.md`, çalışma kayıtları `BACKLOGLOG.md`.
