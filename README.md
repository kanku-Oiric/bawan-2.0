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
| roll mining | `roll = HMAC-SHA256(seed, "mining\|{guild_id}\|{user_id}\|{node_id}\|{n}")`, `n` = counter percobaan (guild, user) yang dinaikkan atomik di DB **sebelum** roll dihitung | Pemain: tidak ada (counter tidak bisa dimundurkan/dihapus). Operator: lihat *Known limitations* |

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

## Known limitations

### Operator bisa memprediksi roll mining

Pemegang pepper (operator) bisa menghitung `seed` server mana pun, dan counter `n` tersimpan di DB yang ia kelola. Artinya ia bisa menghitung **hasil ayunan berikutnya** setiap pemain di setiap node sebelum ayunan itu terjadi, lalu memakai informasi itu (memilih node/waktu untuk akunnya sendiri, atau membocorkannya ke pemain tertentu).

Pemain biasa tidak bisa memprediksi roll karena tidak tahu pepper. Counter `n` juga tidak bisa dimundurkan atau dihapus lewat API, termasuk dengan key service_role (trigger menolaknya). Batas perlindungan ini:

- Pemilik project Supabase bisa menghapus trigger lewat SQL Editor lalu memundurkan counter. Trigger melindungi dari pemegang key, bukan dari pemilik database.
- Belum ada log per ayunan (node, `n`, hasil). Setelah pepper dibuka, roll bisa dihitung ulang, tapi belum bisa dicocokkan dengan riwayat ayunan yang sebenarnya terjadi.

**Rencana mitigasi (BELUM aktif):** masukkan randomness drand ke pre-image roll:

```
roll = HMAC-SHA256(seed, "mining|{guild_id}|{user_id}|{node_id}|{n}|{drand_round}|{drand_randomness}")
```

dengan `drand_round` = ronde pertama **setelah** counter `n` dinaikkan (waktu dari jam DB), dan ronde + signature-nya disimpan per percobaan supaya bisa diaudit. Karena ronde itu belum ada saat `n` dikunci, operator pun tidak bisa tahu hasilnya lebih dulu. Konsekuensi yang harus diterima kalau ini diaktifkan: tiap ayunan menunggu ±3 detik (periode quicknet), dan kalau drand tidak bisa dihubungi ayunan ditolak (tanpa fallback, sama seperti Stage 0).
