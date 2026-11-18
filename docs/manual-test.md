# agent-watch v2 — Manuel Uçtan Uca Test Listesi

Otomatik testler ayrıştırıcıları, store'u, bağlayıcıyı (linker), başlatıcıyı ve ekranları fixture'larla doğrular. Aşağıdaki senaryolar gerçek Windows 11 terminali (Windows Terminal / PowerShell), gerçek Claude Code oturumları ve gerçek `agy` / `codex` ikili dosyaları ile doğrulanmalıdır.

Ön koşul: `agent-watch` kurulu olmalı ve `claude-mod` kurulumu [claude-mod/README.md](../claude-mod/README.md) adımlarına göre yapılmış olmalıdır.

## Başlatıcı ve `claude-mod` Entegrasyonu

- [ ] **Oturum başlangıcında otomatik açılma (`claude-mod`):** `AGENT_WATCH_AUTO_OPEN=0` ayarlı değilken yeni bir Claude Code oturumu başlat. Agent Watch'ın `Agent Watch` başlıklı ayrı bir PowerShell penceresinde otomatik açıldığını, oturum sekmesinin ve `main` ana ajan kartının göründüğünü doğrula.
- [ ] **`AGENT_WATCH_AUTO_OPEN=0` ile otomatik açılmayı kapatma:** Açık Agent Watch penceresini kapat; ortam değişkenine (veya `~/.claude/settings.json` içindeki `env` bölümüne) `AGENT_WATCH_AUTO_OPEN=0` ekle ve yeni bir Claude Code oturumu başlat. Pencerenin otomatik açılmadığını doğrula (ayrıca `.venv\Scripts\agent-watch --new-window --auto` komutunun `auto-open off` yazdırdığını kontrol et).
- [ ] **`/agent-watch` komutu:** `AGENT_WATCH_AUTO_OPEN=0` aktifken (veya Agent Watch penceresi kapalıyken) Claude Code içinde `/agent-watch` yaz. Modele yeni bir tur göndermeden Agent Watch penceresinin ayrı pencerede açıldığını doğrula.
- [ ] **İkinci başlatmada `"Agent Watch already open"`:** Agent Watch penceresi açıkken başka bir PowerShell terminalinden `.venv\Scripts\agent-watch --new-window` çalıştır (veya Claude Code içinde yeniden `/agent-watch` yaz). İkinci bir pencere açılmadığını ve çıktıda `"Agent Watch already open"` yazdığını doğrula.

## Oturum Bağlama, Kartlar ve Konuşma Ekranı

- [ ] **Aynı oturumdan Claude alt ajanı + `agy` + Codex çalıştırma:** Tek bir canlı Claude Code oturumundan sırayla bir Claude alt ajanı (`Agent` aracı), bir `agy` komutu ve bir `codex` komutu çalıştır. Üçünün de `Unlinked` yerine aynı Claude oturum sekmesi altında ayrı kartlar olarak göründüğünü doğrula (bir alt ajan tarafından başlatıldıysa kartta `← <alt-ajan>` etiketi yer alır).
- [ ] **Kart alanları ve biten ajanların görünürlüğü:** Her kartta kaynak (`claude` / `agy` / `codex`), animasyonlu maskot, model, effort (varsa), geçen süre, token sayısı (`in+out` ve `out`), durum (`●` / `✓` / `✗`) ve son adım bilgisinin canlı güncellendiğini doğrula. Çalışması biten alt ajanların ve araç çağrılarının, Claude oturumu açık kaldığı sürece kart olarak ekranda kaldığını doğrula.
- [ ] **Konuşma ekranını `Enter`/tıklama ile açma ve `Esc` ile geri dönme:** `main`, Claude alt ajanı, `agy` ve `codex` kartlarının her birini tıklayarak veya odaklıyken `Enter` tuşuna basarak aç. Salt okunur konuşma ekranında üst bilgiyi (`in / cached / out / reasoning` dökümü dahil) ve kronolojik olayları (`▶ to agent`, `◀ agent`, `… thinking`, `⚙` araç çağrıları, `↳ result`) doğrula; ajan çalışırken ekranın sona otomatik kaydığını, yukarı kaydırınca takibin durduğunu kontrol et. `Esc` tuşuna basarak panoya geri dön.
- [ ] **Oturum kapanınca kartların bekleme süresi sonunda kalkması:** Claude Code oturumunu kapat. Oturum sekmesinin ve ona bağlı tüm kartların (`main`, Claude alt ajanı, `agy`, Codex) yaklaşık 60 saniyelik bekleme süresinin (grace period) ardından panodan kaldırıldığını doğrula (bağlı bir `agy`/`codex` süreci hâlâ çalışıyorsa oturum, o süreç bittikten 60 saniye sonra kaldırılır).

## Genel Kontroller ve Salt Okunur Garanti

- [ ] **Pano kısayolları ve kota paneli:** Panoda `1-9`, `Tab` ve `Shift+Tab` ile sekmeler arasında geç; `v` ile kompakt modu aç/kapat; `o` ile desteklenen ajanların (`agy` ve Claude alt ajanları) transkript klasörünü aç; `r` ile kotayı yenile (sağ panelde `agy` kotası ve Codex `rate_limits` bilgisini kontrol et); `q` ile çık.
- [ ] **Salt okunur çalışma ve dosya kilidi olmaması:** Ajanlar çalışırken Claude Code, `agy` ve `codex`'in transkript dosyalarına sorunsuz yazmaya devam ettiğini ve `~/.claude`, `~/.codex`, `~/.gemini` altında hiçbir dosya değişikliği veya yazma yapılmadığını doğrula.
