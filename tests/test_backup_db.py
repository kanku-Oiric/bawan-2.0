"""Offline test for scripts/backup_db.py (fake PostgREST client, temp folder outside the repo)."""
import csv
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import backup_db as b  # noqa: E402

failures = 0


def check(label, got, expected):
    global failures
    ok = got == expected
    failures += 0 if ok else 1
    print(f"  {'✓' if ok else '✗'}  {label}" + ("" if ok else f"\n      got      {got!r}\n      expected {expected!r}"))


class Q:
    def __init__(self, rows, table, missing):
        self.rows, self.table, self.missing = rows, table, missing
        self.sel, self.orders, self.rng = "*", [], None
    def select(self, s): self.sel = s; return self
    def order(self, c): self.orders.append(c); return self
    def range(self, a, z): self.rng = (a, z); return self
    def execute(self):
        if self.missing:
            raise RuntimeError(f"relation {self.table} does not exist")
        rows = sorted(self.rows, key=lambda r: tuple(r[c] for c in self.orders))
        page = [dict(r) for r in rows[self.rng[0]:self.rng[1] + 1]]
        for alias in [p for p in self.sel.split(",") if ":" in p]:
            name, expr = alias.split(":", 1)
            col = expr.split("::")[0]
            for r in page:
                r[name] = str(r[col]) if r[col] is not None else None
        return type("R", (), {"data": page})()


class FakeClient:
    def __init__(self, tables): self.tables = tables
    def table(self, t): return Q(self.tables.get(t, []), t, t not in self.tables)
    def rpc(self, name, params):
        return type("R", (), {"execute": lambda s: type("D", (), {"data": {"wallet_mismatch": [], "ledger_without_player": 0,
                                                                          "balance_chain_broken": 0, "sale_without_disposal": 0,
                                                                          "results_beyond_counter": 0}})()})()


tables = {t: [] for t, _, _ in b.TABLES}
tables["players"] = [{"guild_id": 1, "user_id": u, "wallet": 12.3456 if u == 1 else 0, "pickaxe_key": 'say "hi", ok',
                      "ore_bag": [{"a": 1}], "automine": u % 2 == 0, "stamina_at": None, "note": ""}
                     for u in range(1, 2502)]
tables["ledger"] = [{"id": 1, "ref": "opening:1:1", "amount": "12.3456", "balance_after": "12.3456"}]
client = FakeClient(tables)

print("\n[1] CSV semantik COPY")
out = io.StringIO()
fields = [b.csv_field(v) for v in (None, "", 'a"b', True, 7, 1.5, {"k": [1, 2]})]
check("NULL kosong, '' dikutip, kutip digandakan, bool, angka, json", fields,
      ['', '""', '"a""b"', 'true', '7', '1.5', '"{""k"":[1,2]}"'])

print("\n[2] Backup ke folder sementara di luar repo")
tmp = Path(tempfile.mkdtemp(prefix="bawan-backup-test-"))
now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
snap = b.backup(client, "abcdef.supabase.co", tmp, now)
manifest = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
check("paginasi: 2501 pemain lengkap", manifest["tables"]["players"]["rows"], 2501)
check("tabel opsional node_state dilewati dengan catatan", "skipped" in manifest["tables"]["node_state"], True)
with open(snap / "players.csv", encoding="utf-8", newline="") as f:
    rows = list(csv.reader(f))
header, first = rows[0], rows[1]
check("wallet dibaca sebagai teks eksak", first[header.index("wallet")], "12.3456")
check("teks dengan kutip & koma utuh", first[header.index("pickaxe_key")], 'say "hi", ok')
check("jsonb jadi teks JSON", json.loads(first[header.index("ore_bag")]), [{"a": 1}])
check("sha256 manifest = isi file", manifest["tables"]["players"]["sha256"], b.sha256_file(snap / "players.csv"))
check("audit ikut tersimpan", manifest["ledger_audit"]["wallet_mismatch"], [])
def tokens(line):
    """CSV fields as (text, was_quoted)."""
    out, i = [], 0
    while i <= len(line):
        if i < len(line) and line[i] == '"':
            j, buf = i + 1, []
            while True:
                if line[j] == '"' and j + 1 < len(line) and line[j + 1] == '"':
                    buf.append('"'); j += 2
                elif line[j] == '"':
                    break
                else:
                    buf.append(line[j]); j += 1
            out.append(("".join(buf), True)); i = j + 2
        else:
            j = line.find(",", i)
            j = len(line) if j < 0 else j
            out.append((line[i:j], False)); i = j + 1
    return out
raw = tokens((snap / "players.csv").read_text(encoding="utf-8").splitlines()[1])
check("NULL (stamina_at) = kosong tanpa kutip, '' (note) = kosong dengan kutip",
      (raw[header.index("stamina_at")], raw[header.index("note")]), (("", False), ("", True)))

print("\n[3] Penjaga & prune")
try:
    b.refuse_inside_repo(b.ROOT / "backups")
    check("folder di dalam repo ditolak", "diterima", "SystemExit")
except SystemExit:
    check("folder di dalam repo ditolak", "SystemExit", "SystemExit")
for i in range(1, 4):
    b.backup(client, "abcdef.supabase.co", tmp, now + timedelta(hours=i))
removed = b.prune(snap.parent, 2)
check("prune --keep 2 menyisakan 2 terbaru", (len(removed), sorted(p.name for p in snap.parent.iterdir())),
      (2, ["20260926T140000Z", "20260926T150000Z"]))

import shutil
shutil.rmtree(tmp)
print(f"\n{'SEMUA TES LULUS ✓' if failures == 0 else f'{failures} TES GAGAL ✗'}")
raise SystemExit(1 if failures else 0)
