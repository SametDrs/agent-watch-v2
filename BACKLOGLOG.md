# Backloglog — çalışma notları ve tamamlananlar

## Çalışma notları
- Yazım: agy `gemini-4-hydrogen-xhigh --mode accept-edits` (shell çalıştırmaz); testleri ve commit'i yönetici çalıştırır. İnceleme: Hydrogen `--mode plan`.
- agy task brief'leri Global Constraints'i görmez; `.superpowers/sdd/.../agy-common.md` ile verilir (Task 2'de `EVENT_CAP` 500 yazılmıştı, 2000'e düzeltildi).
- `pyproject.toml` `[dev]` extra değil `[dependency-groups] dev` kullanıyor; dev paketleri ayrıca kurulur.
- Preflight kararları R1–R11 ledger'da; ilgili task'a `task-N-rulings.md` ile verilir.

## Tamamlananlar
- 2026-11-18 `d597661` docs: v2 tasarım, maskot referansı, uygulama planı.
- 2026-11-18 `4f469bd` Task 1: canlı agy sohbeti ve yönetim özellikleri kaldırıldı, `run_oneshot` → `runner.py`. 167 → 137 test. İnceleme temiz.
- 2026-11-18 `fb86c81` Task 2: konuşma destekli model, sınırlı olay deposu, canlı oturum kapsamı. 143 test. İnceleme temiz (3 minor park).
- 2026-11-18 `93914b5` Task 3: Claude kaynağı — canlı oturumlar, ana ajan, gövdeler, effort. 150 test. İnceleme temiz (4 minor park).
- 2026-11-18 `8ea6872` Task 4: agy kaynağı — gövdeler, effort; `conversation_metadata.json` okuması kaldırıldı. 156 test. İnceleme temiz (4 minor park).
- 2026-11-18 `004a982` + `91ebb5c` Task 5: Codex kaynağı. İnceleme 1 Important (timeout FAILED sonraki DONE'u kilitliyordu) → düzeltildi. 196 test.
- 2026-11-18 `e9e06bf` + `20bea57` Task 6: Codex bağlama, Unlinked grubu. İnceleme 1 Important (`continue` girintisi 1:1 bağlamayı bozuyordu) → düzeltildi. 197 test.
- 2026-11-18 `2363d3e` Task 7: kart maskotları (`ui/card_mascot.py`). İnceleme temiz (2 minor park).
- 2026-11-18 `90bdb33` Task 8: maskotlu kartlar, main-önce ızgara, Codex kota satırı. Ekran dışı tick testi `grid-rows` eksikliğini yakaladı. 207 test. İnceleme temiz (7 minor park).
- 2026-11-18 `b76609f` + `2244465` Task 9: salt okunur konuşma ekranı + snapshot. İnceleme 2 Important (mount öncesi refresh yarışı, animasyon sırasında takip) → düzeltildi. `db7f25a` tıklama flake'i focus+Enter ile giderildi.
- 2026-11-18 `ec9f9a2` + `68ad2d3` + `c5bbd28` Task 10: launcher, tek örnek. Eşzamanlılık incelemesi 5 Important → OS kilidi (`msvcrt.locking`) + claim çocuk pid'i; yeniden inceleme I1 (prob başlayan TUI'yi çıkarıyordu) → kilit alma 20×50 ms yeniden deneme. 248 test.
- 2026-11-18 `630d95e` Task 11: `claude-mod/` (oturum başında otomatik açılış, `/agent-watch`). `claude plugin validate`/`test` temiz. `~/.claude/settings.json` ayarı yapıldı (kullanıcı onaylı).
- 2026-11-18 `2d21273` Fixture'lardaki kişisel kullanıcı yolu `user` yapıldı, kullanılmayan agy metadata fixture'ı silindi.
- 2026-11-18 `885ece0` `b5ab0fb` `acdce7d` `60de035` Final dal incelemesi düzeltmeleri: biten ajanın yeniden açılması, açılışta eski Unlinked geçmişinin gizlenmesi, pid değişiminde Claude sayaçlarının iki kez sayılmaması; sohbet döneminden kalan ölü kod silindi; README ve v2 elle test listesi. 218 test.

## Notlar
- Final incelemede geçmişte kişisel yol bulundu; kullanıcı public repo için tek commit'e sıkıştırmayı seçti, commit yazar e-postası olduğu gibi kalıyor.
- Textual `run_test` içinde `print()` yakalanır; uygulama çalışırken `main()` çıktısını test etmek için `contextlib.redirect_stdout` kullan.
- agy `--print-timeout` 580 s'yi aşan task'lar `agy --continue` ile aynı oturumda bitirilir.
- Snapshot testleri ilk üretimde `--snapshot-update` ister; agy shell çalıştırmadığı için yönetici üretir.
- 2026-11-18 `a9bbf46` public `main` olarak gönderildi (tek commit, tarama temiz). Tam geçmiş yerel `history-backup` dalında; kullanıcı bu hâliyle kabul etti. Yerel `main` origin'den ayrıştı; origin'e push edilmez.
- Bazı küçük düzeltmeler (≤3 dosya) agy yerine yönetici tarafından yazıldı; kullanıcı kabul etti.

## 2026-11-18 — UI yenileme
- Dal `ui-refresh`: konuşma ekranı ve dashboard modern koyu temaya geçti; 222 test yeşil. Henüz main'e merge/push edilmedi.
