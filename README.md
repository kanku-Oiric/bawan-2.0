# Bawan 2.0

Bot Discord ekonomi MMORPG per-server. Dunia tiap server (unsur, ore, crystal, mata uang) dibangkitkan secara deterministik dari seed yang **tidak bisa dipilih siapa pun, termasuk operator**.

## Setup

1. `pip install -r requirements.txt`
2. Salin `.env.example` → `.env`, lalu isi semua nilainya (lihat komentar di file itu).
3. Jalankan seluruh `supabase/schema.sql` di Supabase SQL Editor. Aman diulang.
4. `python main_core.py`

## Asal-usul dunia (bisa diverifikasi publik)

| Stage | Isi | Siapa yang bisa memanipulasi |
|---|---|---|
| 0 — registrasi | `world_nonce` = randomness ronde **drand quicknet** pertama setelah `registered_at` | Tidak ada: rondenya di masa depan saat registrasi |
| 1 — seed | `seed = HMAC-SHA256(pepper, "BAWAN\|{algo_version}\|{guild_id}\|{world_nonce}")` | Tidak ada: pepper terkunci oleh commitment di bawah |
| 2 — stream | `stream(seed, domain, i) = HMAC-SHA256(seed, "{domain}\|{i}")`, `unit(h) = (h >> 203) / 2**53`; domain: `genetic`, `material`, `spawner`, `periodic` (+ cadangan `wood`, `flora`, `mob`, `season`) | Tidak ada: deterministik dari seed; tiap domain independen |

Setiap registrasi dan aktivasi dipublikasikan ke webhook saksi publik. `/worldproof` di server mana pun menampilkan semua bahan verifikasi.

### Pepper commitment

Pepper dirahasiakan selama eksperimen dan **dibuka di akhir**. Commitment-nya dipublikasikan sekarang supaya pepper tidak bisa diganti diam-diam:

| algo_version | SHA-256(pepper) | Dicatat |
|---|---|---|
| v1 | `06c57bcb03dab95deb76329e8b1426637b30860971337878ba791f73027a122d` | 2026-09-25 |

Cara mendapatkan nilainya: `python world_seed.py --commitment`. Nilai yang sama tercatat di tabel `world_commitments` dan di webhook saksi. Bot menolak start kalau pepper di `.env` tidak cocok dengan commitment yang tersimpan.

### Verifikasi setelah reveal

1. `SHA-256(bytes.fromhex(pepper)) == commitment` di tabel atas.
2. Untuk tiap server di `/worldproof`: cek `world_nonce` di relay drand, lalu hitung ulang `seed` dengan rumus Stage 1.
