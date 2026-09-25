"""
Jalankan semua pengecekan live sekaligus, lalu tampilkan ringkasan.

    python cek_live.py

Urutan: --security (project utama, read-only) → --live-db & --live (project
TES) → --commitment → tes worldgen LANGKAH 4 (offline).  Semua langkah tetap
dijalankan walau ada yang gagal.
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

STEPS = [
    ("Keamanan (RLS, service_role, anon)", ["db_ekonomi_pusat.py", "--security"]),
    ("Registry: idempotensi & insert-only", ["world_registry.py", "--live-db"]),
    ("DB tes: tulis/baca/hapus", ["db_ekonomi_pusat.py", "--live"]),
    ("Pepper commitment", ["world_seed.py", "--commitment"]),
    ("Worldgen LANGKAH 4: isolasi & stratifikasi (offline)", ["tests/test_worldgen_langkah4.py"]),
]

env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
results = []
for title, args in STEPS:
    print(f"\n{'═' * 78}\n▶ {title}   (python {' '.join(args)})\n{'═' * 78}", flush=True)
    code = subprocess.call([sys.executable, *args], cwd=HERE, env=env)
    results.append((title, code == 0))

print(f"\n{'═' * 78}\nRINGKASAN\n{'═' * 78}")
for title, ok in results:
    print(f"  {'✓' if ok else '✗'}  {title}")
failed = sum(1 for _, ok in results if not ok)
print(f"\n{'SEMUA LULUS ✓' if not failed else f'{failed} langkah gagal ✗ — lihat pesan ✗ di atas'}\n")
sys.exit(1 if failed else 0)
