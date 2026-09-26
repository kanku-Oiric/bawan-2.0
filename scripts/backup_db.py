"""
Backup rutin database Bawan: satu CSV per tabel + manifest.json.

    python scripts/backup_db.py                      # project UTAMA → ../bawan-backups/
    python scripts/backup_db.py --out D:/backup/bawan
    python scripts/backup_db.py --test               # project TES (SUPABASE_TEST_*)
    python scripts/backup_db.py --keep 30            # simpan 30 backup terbaru, hapus yang lebih lama

Read-only terhadap database: hanya SELECT (lewat PostgREST, key dari .env) dan
rpc ledger_audit().  Hasilnya WAJIB di luar repo — isinya Discord ID dan saldo
pemain; script menolak folder output di dalam repo.

Format CSV mengikuti COPY Postgres (format csv): NULL = kolom kosong tanpa
kutip, teks selalu dikutip, jsonb ditulis sebagai teks JSON, kolom numeric
uang dibaca sebagai teks sehingga presisinya utuh.  Cara restore: README →
"Backup & restore".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Sequence
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PAGE_SIZE = 1000

# (tabel, kolom urut untuk paginasi stabil, kolom numeric yang dibaca sebagai teks)
TABLES: Sequence[tuple] = (
    ("players",           ("guild_id", "user_id"),            ("wallet",)),
    ("currencies",        ("guild_id",),                       ()),
    ("voice_config",      ("guild_id",),                       ()),
    ("server_registry",   ("guild_id",),                       ()),
    ("world_nonces",      ("guild_id",),                       ()),
    ("world_commitments", ("algo_version",),                   ()),
    ("world_witness_log", ("event_key",),                      ()),
    ("mining_attempts",   ("guild_id", "user_id"),             ()),
    ("ledger",            ("id",),                             ("amount", "balance_after")),
    ("items",             ("item_id",),                        ()),
    ("item_disposals",    ("item_id",),                        ()),
    ("mining_results",    ("guild_id", "user_id", "attempt"),  ()),
    ("voice_ticks",       ("ref",),                            ()),
    ("production_policy", ("scope",),                          ()),
)
OPTIONAL_TABLES = {"node_state"}      # dibuat skema versi berikutnya; dilewati kalau belum ada


def csv_field(value: object) -> str:
    """Satu nilai → teks CSV ala COPY: NULL kosong tanpa kutip, teks selalu dikutip."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return '"' + str(value).replace('"', '""') + '"'


def write_csv(path: Path, columns: Sequence[str], rows: Iterable[dict]) -> int:
    count = 0
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(",".join(columns) + "\n")
        for row in rows:
            f.write(",".join(csv_field(row.get(c)) for c in columns) + "\n")
            count += 1
    return count


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_table(client, table: str, order_by: Sequence[str], exact_numeric: Sequence[str]) -> List[dict]:
    """Semua baris, dipaginasi.  Kolom numeric uang diganti versi teksnya (tanpa lewat float)."""
    select = "*" + "".join(f",{c}__exact:{c}::text" for c in exact_numeric)
    rows: List[dict] = []
    start = 0
    while True:
        query = client.table(table).select(select)
        for col in order_by:
            query = query.order(col)
        page = query.range(start, start + PAGE_SIZE - 1).execute().data
        if not isinstance(page, list) or any(not isinstance(r, dict) for r in page):
            raise RuntimeError(f"{table}: respons bukan dari API Supabase — cek SUPABASE_URL")
        for row in page:
            for c in exact_numeric:
                row[c] = row.pop(f"{c}__exact")
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        start += PAGE_SIZE


def refuse_inside_repo(out: Path) -> None:
    out, repo = out.resolve(), ROOT.resolve()
    if out == repo or repo in out.parents:
        raise SystemExit(f"✗ Folder backup {out} ada DI DALAM repo — data pemain tidak boleh ikut ter-commit. "
                         f"Pakai folder di luar {repo}.")


def prune(project_dir: Path, keep: int) -> List[str]:
    """Hapus backup lama di folder project ini saja, sisakan `keep` yang terbaru."""
    snapshots = sorted(p for p in project_dir.iterdir() if p.is_dir() and (p / "manifest.json").is_file())
    removed = []
    for old in snapshots[:-keep] if keep > 0 else []:
        shutil.rmtree(old)
        removed.append(old.name)
    return removed


def backup(client, host: str, out_root: Path, now: datetime) -> Path:
    refuse_inside_repo(out_root)
    project_dir = out_root / host.split(".")[0]
    snap = project_dir / now.strftime("%Y%m%dT%H%M%SZ")
    snap.mkdir(parents=True, exist_ok=False)
    manifest: Dict[str, object] = {
        "created_at": now.isoformat(), "project_host": host, "format": "csv (COPY semantics)", "tables": {},
    }
    for table, order_by, exact in TABLES + tuple((t, ("guild_id", "node_id"), ()) for t in sorted(OPTIONAL_TABLES)):
        try:
            rows = fetch_table(client, table, order_by, exact)
        except Exception as exc:
            if table in OPTIONAL_TABLES:
                manifest["tables"][table] = {"skipped": f"tidak ada di project ini ({str(exc)[:80]})"}
                continue
            raise
        columns = list(rows[0].keys()) if rows else []
        path = snap / f"{table}.csv"
        n = write_csv(path, columns, rows)
        manifest["tables"][table] = {"rows": n, "columns": columns, "sha256": sha256_file(path)}
        print(f"  ✓  {table:<18} {n:>8} baris")
    try:
        manifest["ledger_audit"] = client.rpc("ledger_audit", {}).execute().data
    except Exception as exc:
        manifest["ledger_audit"] = {"error": str(exc)[:200]}
    (snap / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return snap


def main(argv: Sequence[str]) -> int:
    from dotenv import load_dotenv
    from supabase import create_client
    from db_ekonomi_pusat import connect_test_database, normalize_supabase_url, validate_supabase_url

    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--out", default=os.getenv("BAWAN_BACKUP_DIR", str(ROOT.parent / "bawan-backups")))
    parser.add_argument("--test", action="store_true", help="backup project TES (SUPABASE_TEST_*)")
    parser.add_argument("--keep", type=int, default=0, help="simpan N backup terbaru (0 = simpan semua)")
    args = parser.parse_args(argv)

    load_dotenv(ROOT / ".env")
    out_root = Path(args.out)
    refuse_inside_repo(out_root)
    if args.test:
        client = connect_test_database(os.environ)
        url = normalize_supabase_url(os.environ["SUPABASE_TEST_URL"])
    else:
        url = validate_supabase_url(os.getenv("SUPABASE_URL", ""), "SUPABASE_URL")
        key = os.getenv("SUPABASE_KEY", "").strip()
        if not key:
            raise SystemExit("✗ SUPABASE_KEY kosong di .env")
        client = create_client(url, key)
    host = urlparse(url).hostname or "unknown"

    now = datetime.now(timezone.utc)
    print(f"\nBackup {'TES' if args.test else 'UTAMA'} ({host.split('.')[0]}) → {out_root}")
    snap = backup(client, host, out_root, now)
    audit = json.loads((snap / "manifest.json").read_text(encoding="utf-8")).get("ledger_audit", {})
    clean = isinstance(audit, dict) and audit.get("wallet_mismatch") == [] and not any(
        audit.get(k) for k in ("ledger_without_player", "balance_chain_broken", "sale_without_disposal",
                               "results_beyond_counter"))
    print(f"\n  ·  tersimpan di {snap}")
    print(f"  {'✓' if clean else '✗'}  ledger_audit saat backup: {'bersih' if clean else audit}")
    if args.keep:
        removed = prune(snap.parent, args.keep)
        print(f"  ·  backup lama dihapus: {len(removed)}")
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
