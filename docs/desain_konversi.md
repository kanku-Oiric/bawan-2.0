# Desain konversi antar-server (DESAIN — belum dikerjakan)

**Keputusan (2026-09-26):** Model A + kuota per user berbasis produksi. Eksekusi di periode t memakai kurs t+1. Model D (pasar P2P) opsional belakangan.

**Urutan kerja:** saldo di DB → cadangan node + batas produksi → LANGKAH 5 → LANGKAH 6 → sink → konversi.

Syarat buka: LANGKAH 6 selesai · worldgen beku v1 · sink uang sudah ada · saldo otoritatif di DB.

## Definisi

- **M_s** = jumlah saldo semua pemain server s, dibaca dari DB (satu sumber).
- **R_s** = kas server s: nilai UA ore yang **sudah diekstraksi** dan masuk kas lewat `/sell_ore`. Setiap penjualan menambah R_s sebesar nilai UA ore itu (tabel harga UA global, berversi, dibekukan seperti worldgen). Geologi yang masih di tanah **tidak** dihitung.
- **Kurs** k_s = R_s / M_s (UA per unit). Semua kurs silang lewat UA: A→B = k_A / k_B.
- **Periode** t = 1 jam. Permintaan di periode t dieksekusi pada awal t+1 dengan kurs k_{t+1} (snapshot akhir periode t), yang dipublikasikan ke webhook saksi sebelum eksekusi.

Invariant wajib: M naik 10× → kurs turun ≈10×. Metrik yang murah dipalsukan (jumlah transaksi, volume, jumlah partisipan) hanya boleh jadi syarat eligibility.

## Model

| Model | Kurs | Konversi x unit A → B | Invariant | Solvensi |
|---|---|---|---|---|
| S0 (kode sekarang) | genesis tetap, dijepit 0,5–5 UA | mint B dengan kurs tetap | ✗ **ditolak** | — |
| **A** | R/M | bakar A, cetak B, **kas ikut pindah**: R_A −= x·k_A·(1−f), R_B += sama | ✓ | ✓ ΣR konstan |
| B | R/M | bakar A, cetak B, kas **diam** | ✓ | klaim dipindah dari pemegang B |
| C | (R + P)/M, P = ore ditambang tapi belum dijual | seperti A | ✓ | ✗ kurs menjanjikan lebih dari isi kas |
| D | pasar (order book, bot escrow) | P2P; A tetap tersedia sebagai jalur tebus | ✓ (dijepit jangkar A) | ✓ (P2P zero-sum) |

Sifat penting A: konversi **netral terhadap kurs**. Sesudah konversi, k_A dan k_B tidak berubah (kas pindah sebanding dengan uangnya), sehingga aliran besar tidak bisa dipakai untuk menggeser kurs, dan eksekusi di t+1 terdefinisi jelas tanpa bergantung pada batch konversi itu sendiri.

## Hasil simulasi (fee 1%, batas keluar 5% R per periode, 720 periode = 30 hari)

Server jujur H: R = 50.000 UA, M = 500.000 (k = 0,1). Ukuran nilai = klaim atas kas (unit × R/M).

### 0. Invariant

| model | rasio kurs setelah M ×10 | status |
|---|---|---|
| S0 | 1,000 | ✗ ditolak |
| A | 0,100 | ✓ |
| B | 0,100 | ✓ |
| C | 0,100 | ✓ |
| D | ≈0,1 (dijepit jangkar A ±fee) | ✓ |

### (i) Server sepi, M kecil

Penyerang bikin Q, produksi nyata 100 UA, NPC Q pelit → R_Q = 100, M_Q = 1 (kurs 100 UA/unit), lalu konversi terus ke H.

| model | untung bersih penyerang | kerugian pemegang jujur H | kekurangan kas |
|---|---|---|---|
| **A** | **−0,20** | **0** | 0 |
| B | +500 (kas Q dipakai ulang tiap periode) | 600 | 0 |
| C (+ timbunan ore 10.000 UA) | +34.904 | 34.904 | 60.295 (insolven) |
| S0 | −95 (salah harga ke arah lain) | 5 | — |

### (ii) 10 akun alt (28.800 unit/hari dari voice, tanpa produksi)

Gerbang "≥5 pemain aktif" otomatis lolos oleh alt, jadi gerbang saja tidak cukup.

| skenario | model | untung bersih | kerugian pemegang jujur H |
|---|---|---|---|
| farm di Q sendiri, bawa ke H | **A** | **0** | **0** |
| | B | +1.469 | 1.569 |
| | S0 | +10.993 | 11.093 |
| farm di H, bawa keluar | A tanpa kuota | — | **2.696 (5,4% kas H/hari)** |
| | A + kuota ≤ produksi sendiri | — | **0** |

Farm di H tetap mendilusi H secara lokal (itu kebijakan reward voice, bukan FX), tapi dengan kuota berbasis produksi, hasil farm tidak bisa dibawa keluar.

### (iii) Front-running (kurs H diketahui turun 9,1% periode depan)

| aturan eksekusi | hasil tukar H→Q→H |
|---|---|
| kurs t (basi) | **+7,81%** (diambil dari pemegang jujur) |
| **kurs t+1** | **−1,99%** (cuma fee) |

### (iv) Arbitrase bolak-balik

| model | hasil |
|---|---|
| **A** | **−2,10%** |
| B | −41,00% (kurs bergeser melawan) |
| D, order salah harga 5% | arbitraser +4,21% dibayar penjual yang salah harga; harga pasar terjepit di pita [0,99×; 1,0101×] jangkar |
| D, di dalam pita | ≤ 0 setelah fee |

## Parameter awal yang diusulkan

| parameter | nilai | alasan |
|---|---|---|
| periode kurs | 1 jam, eksekusi t+1 | front-running = −fee (iii) |
| fee | 1%, dibakar di mata uang asal | bolak-balik rugi ~2% (iv); juga sink pertama |
| batas keluar per server | ≤ 5% R_asal per periode | bank run butuh ≥ 20 jam, ada waktu mendeteksi |
| **kuota per user** | ≤ nilai UA produksi sendiri (ore yang dia jual lewat `/sell_ore`) dikurangi yang sudah dikonversi | menutup (ii-b); produksi = metrik mahal *setelah* produksi dibatasi (lihat bawah) |
| eligibility server | umur mata uang ≥ 14 hari · ≥ 5 pemain dengan produksi di 7 hari terakhir · worldgen ≠ dev · R > 0 dan M > 0 · riwayat kurs ≥ 24 periode | gerbang saja, tidak masuk rumus |

## Rekomendasi

- **A** sebagai lapisan settlement. Satu-satunya model yang nol-untung di keempat skenario, solven, dan netral terhadap kurs.
- **D** opsional di atas A, setelah ada sink dan cukup pemain yang aktif di beberapa server. Pasar memberi price discovery di dalam pita ±1%; A menjepitnya. Wash trading hanya mengubah harga yang ditampilkan, tidak menyentuh rumus apa pun.
- **Ditolak:** S0 (gagal invariant), B (kas dipakai ulang → drain berulang), C (insolven).

## Prasyarat yang ditemukan

1. **Produksi belum mahal.** `/rest` gratis tanpa cooldown dan tidak ada batas kecepatan ayunan, jadi produksi hanya dibatasi kecepatan klik. Bot bisa memompa R dan kuota produksi. Sebelum produksi boleh jadi dasar kuota: stamina pulih seiring waktu (bukan `/rest` instan) dan/atau batas ayunan per menit per user, dicatat atomik di DB.
2. **Tabel harga UA global** untuk menghitung R dari penjualan: berversi dan dibekukan. Sampai diputuskan, simpan fakta mentah (unsur, purity, ton) di setiap penjualan supaya R bisa dihitung ulang nanti.
3. **Cadangan node (spawn state) masih di memori.** Restart mengembalikan node ke penuh, jadi produksi bisa diulang lewat restart. Ini perlu dipersist sebelum produksi jadi metrik yang dipercaya.

## Masalah terbuka

### Reward voice mencetak uang tanpa produksi

Setiap interval voice menambah saldo (M naik) tanpa ada ore yang masuk kas (R tetap), jadi kurs k = R/M turun untuk semua pemegang uang server itu. Selama konversi belum dibuka, efeknya cuma inflasi lokal. Setelah konversi dibuka, uang ini jadi bisa dibawa keluar (lihat skenario ii-b); kuota berbasis produksi menahannya, tapi dilusinya tetap terjadi.

Opsi (belum diputuskan):

1. **Dibayar dari kas yang sudah ada.** Reward voice diambil dari saldo yang sudah beredar (misalnya pos kas server yang diisi pajak/fee), sehingga tidak menambah M. Butuh sumber isi kas; kalau kasnya habis, reward berhenti.
2. **Dicatat sebagai penerbitan terpisah.** Reward tetap mencetak uang, tapi dengan `kind` ledger tersendiri (misalnya `issuance_voice`). Kurs dan kuota konversi bisa memperlakukannya berbeda: dihitung di M, tidak pernah masuk kuota konversi, dan jumlahnya terlihat terpisah di metrik LANGKAH 6.

Data yang dibutuhkan untuk memutuskan sudah tercatat: setiap reward voice punya baris ledger `voice_reward` dengan `ref` per tick.
