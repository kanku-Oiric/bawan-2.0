# Bawan 2.0

Bot Discord ekonomi MMORPG per-server. Dunia tiap server (unsur, ore, crystal, mata uang) dibangkitkan secara deterministik dari seed yang **tidak bisa dipilih siapa pun, termasuk operator**.

## Setup

1. `pip install -r requirements.txt`
2. Salin `.env.example` → `.env`, lalu isi semua nilainya (lihat komentar di file itu).
3. Jalankan seluruh `supabase/schema.sql` di Supabase SQL Editor. Aman diulang. **Kalau bot versi lama sedang jalan, matikan dulu**: sejak v7 saldo hanya bisa berubah lewat fungsi ledger, jadi penyimpanan bot lama akan ditolak.
4. `python main_core.py`
5. Cek semuanya sekaligus: `python cek_live.py` (keamanan, registry, ledger, commitment, tes worldgen).

## Backup & restore

`scripts/backup_db.py` menyimpan satu CSV per tabel + `manifest.json` (jumlah baris, SHA-256 tiap file, snapshot `ledger_audit()`). Read-only terhadap database. **Folder backup wajib di luar repo** — isinya Discord ID dan saldo; script menolak folder di dalam repo.

```
python scripts/backup_db.py                          # project utama → ../bawan-backups/<ref>/<waktu>/
python scripts/backup_db.py --out D:/backup/bawan    # atau set BAWAN_BACKUP_DIR
python scripts/backup_db.py --keep 30                # hapus backup lama, sisakan 30 terbaru
python scripts/backup_db.py --test                   # project TES
```

Rutin harian:

- Windows (Task Scheduler): `schtasks /create /tn "Bawan backup" /sc daily /st 03:00 /tr "\"D:\bawan file 2.0\venv\Scripts\python.exe\" \"D:\bawan file 2.0\scripts\backup_db.py\" --keep 30"`
- VPS (cron): `0 3 * * * cd /opt/bawan && venv/bin/python scripts/backup_db.py --out /var/backups/bawan --keep 30`

Exit code 1 kalau `ledger_audit()` tidak bersih saat backup — jadikan itu alarm.

Restore (ke project kosong): jalankan `supabase/schema.sql`, lalu dari `psql` (connection string di Supabase → Settings → Database) `\copy public.<tabel> (<kolom sesuai header CSV>) from '<tabel>.csv' with (format csv, header true)` dengan urutan: `server_registry`, `world_nonces`, `world_commitments`, `world_witness_log`, `currencies`, `voice_config`, `mining_attempts`, `players`, `items`, `ledger`, `item_disposals`, `mining_results`, `voice_ticks`. Sebelum `players`, jalankan `select set_config('bawan.ledger', 'on', false);` di sesi yang sama (trigger wallet menolak saldo ≠ 0 tanpa itu). Sesudahnya: `select setval('public.ledger_id_seq', (select max(id) from public.ledger));`, samakan `production_policy` dengan CSV lewat UPDATE, lalu pastikan `select public.ledger_audit();` bersih.

## Saldo & ledger (schema v7)

Database adalah satu-satunya sumber kebenaran. Setiap perubahan pemain = **satu fungsi = satu transaksi Postgres**, dan setiap perubahan saldo meninggalkan satu baris di `ledger` (insert-only). Bot tidak punya flush berkala; memori hanya salinan baca yang diisi dari hasil fungsi.

| Aksi | Fungsi DB | Jaminan |
|---|---|---|
| Ayunan (manual & auto) | `begin_swing` → roll → `record_swing` | n naik atomik sebelum roll; hasil tercatat sekali per n di `mining_results`; stamina dicek di DB |
| `/sell_ore`, `/sell_crystal` | `sell_item` | barang terkunci; `item_disposals` (PK) → satu barang hanya bisa dijual sekali |
| Reward voice | `apply_voice_tick` | satu tick = satu transaksi; `ref` tetap → retry setelah timeout tidak dobel |
| `/rest`, pickaxe, auto mine | `rest_player`, `set_player_prefs` | bukan uang, tanpa ledger |

- `players.wallet` bertipe `numeric(24,4)` dan hanya bisa berubah dari dalam fungsi ledger — trigger menolak UPDATE/INSERT langsung, termasuk dengan key service_role. Baris pemain tidak bisa dihapus.
- Uang beredar M = jumlah saldo di DB (`money_supply`), satu sumber.
- Helper internal ada di skema `bawan_private` yang tidak diekspos PostgREST, jadi tidak bisa dipanggil lewat REST.
- Stamina dan batas ayunan dibaca dari tabel `production_policy`; angkanya bisa diganti tanpa mengubah skema.
- `ledger_audit()` memeriksa wallet = Σ ledger, rantai `balance_after`, dan penjualan tanpa pelepasan.
- `/mint_fiat` dinonaktifkan sampai ada desain kebijakan moneter.

## Asal-usul dunia (bisa diverifikasi publik)

| Stage | Isi | Siapa yang bisa memanipulasi |
|---|---|---|
| 0 — registrasi | `world_nonce` = randomness ronde **drand quicknet** pertama setelah `registered_at` | Tidak ada: rondenya di masa depan saat registrasi |
| 1 — seed | `seed = HMAC-SHA256(pepper, "BAWAN\|{algo_version}\|{guild_id}\|{world_nonce}")` | Tidak ada: pepper terkunci oleh commitment di bawah |
| 2 — stream | `stream(seed, domain, i) = HMAC-SHA256(seed, "{domain}\|{i}")`, `unit(h) = (h >> 203) / 2**53`; domain: `genetic`, `material`, `spawner`, `periodic` (+ cadangan `wood`, `flora`, `mob`, `season`) | Tidak ada: deterministik dari seed; tiap domain independen |
| 3 — tabel periodik | 19 unsur core + 9 slot (structural 2, conductive 2, reactive 2, catalytic 1, rare_earth 1, exotic 1) diundi berbobot `rarity_weight` tanpa pengembalian dari `stream(seed, "periodic", i)` — lihat `world_periodic.py` | Tidak ada: deterministik dari seed |
| 4 — unsur dominan | Dominan & sekunder (logam dan non-logam) **hanya** dari 28 unsur tabel server, bobot `round(10·√rarity_weight)`; sekunder = satu draw dari pool tanpa unsur dominan (bobot dinormalisasi ulang). Node `material_gen` wajib ⊆ tabel | Tidak ada: deterministik dari seed; tanpa fallback ke daftar global |
| roll mining | `roll = HMAC-SHA256(seed, "mining\|{guild_id}\|{user_id}\|{node_id}\|{n}")`, `n` = counter percobaan (guild, user) yang dinaikkan atomik di DB **sebelum** roll dihitung | Pemain: tidak ada (counter tidak bisa dimundurkan/dihapus). Operator: lihat *Known limitations* |

Setiap registrasi dan aktivasi dipublikasikan ke webhook saksi publik. `/worldproof` di server mana pun menampilkan semua bahan verifikasi.

Status 118 unsur: 19 core, 69 dalam role, 6 excluded (He Ne Ar Kr Xe Rn — bukan bijih), 24 synthetic (Am–Lr dan 104–118; tidak di-sampling, disimpan untuk crafting). Partisi ini dicek saat bot start.

### Versi worldgen

`algo_version` mengunci cara **seed** dibuat. `worldgen_version` mengunci cara **dunia** dibangun dari seed (Stage 3–4, geologi, spawn awal). Keduanya dicatat per server saat registrasi di `server_registry` (insert-only) dan ikut di event saksi `registered`.

| worldgen_version | Status | Arti |
|---|---|---|
| `dev` | aktif sekarang | Masa pengembangan: dunia **boleh berubah** saat kode berubah. Server `dev` ditandai di `server_registry.worldgen_version` dan datanya tidak dihapus. |
| `v1` | direncanakan | Dibekukan setelah LANGKAH 5 lulus: hash commit kode worldgen dipublikasikan di sini dan di webhook saksi, dan kodenya tidak pernah dihapus. Perubahan sesudahnya = `v2`, hanya untuk server baru. |

Bot menolak membangun dunia untuk `worldgen_version` yang kodenya tidak ada di build tersebut — tidak ada fallback ke versi lain.

### Pepper commitment

Pepper dirahasiakan selama eksperimen dan **dibuka di akhir**. Commitment-nya dipublikasikan sekarang supaya pepper tidak bisa diganti diam-diam:

| algo_version | SHA-256(pepper) | Dicatat |
|---|---|---|
| v1 | `06c57bcb03dab95deb76329e8b1426637b30860971337878ba791f73027a122d` | 2026-09-25 |

Cara mendapatkan nilainya: `python world_seed.py --commitment`. Nilai yang sama tercatat di tabel `world_commitments` dan di webhook saksi. Bot menolak start kalau pepper di `.env` tidak cocok dengan commitment yang tersimpan.

### Verifikasi setelah reveal

1. `SHA-256(bytes.fromhex(pepper)) == commitment` di tabel atas.
2. Untuk tiap server di `/worldproof`: cek `world_nonce` di relay drand, lalu hitung ulang `seed` dengan rumus Stage 1.

## Known limitations

### Operator bisa memprediksi roll mining

Pemegang pepper (operator) bisa menghitung `seed` server mana pun, dan counter `n` tersimpan di DB yang ia kelola. Artinya ia bisa menghitung **hasil ayunan berikutnya** setiap pemain di setiap node sebelum ayunan itu terjadi, lalu memakai informasi itu (memilih node/waktu untuk akunnya sendiri, atau membocorkannya ke pemain tertentu).

Pemain biasa tidak bisa memprediksi roll karena tidak tahu pepper. Counter `n` juga tidak bisa dimundurkan atau dihapus lewat API, termasuk dengan key service_role (trigger menolaknya). Batas perlindungan ini:

- Pemilik project Supabase bisa menghapus trigger lewat SQL Editor lalu memundurkan counter. Trigger melindungi dari pemegang key, bukan dari pemilik database.
- Sejak v7 setiap ayunan tercatat di `mining_results` (node, `n`, hasil, stamina, barang). Setelah pepper dibuka, roll bisa dihitung ulang dan dicocokkan dengan log itu. Batasnya: log ini ada di database operator dan belum dipublikasikan ke saksi eksternal, jadi pemilik database masih bisa mengubahnya setelah menghapus trigger.

**Rencana mitigasi (BELUM aktif):** masukkan randomness drand ke pre-image roll:

```
roll = HMAC-SHA256(seed, "mining|{guild_id}|{user_id}|{node_id}|{n}|{drand_round}|{drand_randomness}")
```

dengan `drand_round` = ronde pertama **setelah** counter `n` dinaikkan (waktu dari jam DB), dan ronde + signature-nya disimpan per percobaan supaya bisa diaudit. Karena ronde itu belum ada saat `n` dikunci, operator pun tidak bisa tahu hasilnya lebih dulu. Konsekuensi yang harus diterima kalau ini diaktifkan: tiap ayunan menunggu ±3 detik (periode quicknet), dan kalau drand tidak bisa dihubungi ayunan ditolak (tanpa fallback, sama seperti Stage 0).

### Pemegang key bisa mencetak uang, tapi tidak diam-diam

Jumlah payout (`/sell_ore`, reward voice) dihitung oleh bot, lalu database mencatatnya. Siapa pun yang memegang key service_role bisa memanggil `sell_item` / `apply_voice_tick` dengan jumlah berapa pun. Ledger tidak mencegah ini. Yang dijamin ledger: setiap unit uang punya baris dengan `kind`, `ref`, dan waktu, rantai saldo tidak bisa diputus, dan tidak ada baris yang bisa diubah atau dihapus tanpa menghapus trigger dulu.

### Cadangan node masih di memori

Pengurangan cadangan node saat nambang belum disimpan ke database, jadi restart mengembalikan node ke penuh. Ini dikerjakan di langkah berikutnya (persist cadangan node + batas produksi), sebelum LANGKAH 5.

## Dokumen desain

- [`docs/desain_konversi.md`](docs/desain_konversi.md): konversi antar-server (Model A + kuota produksi). Belum dikerjakan; urutannya setelah LANGKAH 6 dan sink.
