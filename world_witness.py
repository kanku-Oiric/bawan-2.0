"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         WORLD_WITNESS.PY  —  Saksi Eksternal (webhook Discord publik)        ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  Setiap event Stage 0/1 dipublikasikan ke WORLD_LOG_WEBHOOK:                 ║
║    registered  — server terdaftar (pending) + ronde drand target            ║
║    activated   — world_nonce masuk (ronde, nonce, signature)                ║
║    commitment  — SHA-256(pepper) sebuah algo_version                        ║
║                                                                              ║
║  ANTREAN RETRY TANPA STATE TAMBAHAN                                          ║
║  • Event yang SEHARUSNYA ada diturunkan dari tabel registry/commitment.     ║
║  • Event yang SUDAH terkirim dicatat di world_witness_log (insert-only).    ║
║  • Pending = selisih keduanya → tahan restart, crash, webhook mati, atau    ║
║    webhook belum dikonfigurasi.  Registrasi TIDAK pernah menunggu webhook.  ║
║                                                                              ║
║  Pesan berisi JSON kanonik (sort_keys) + event_id = SHA-256(JSON)[:16],     ║
║  jadi pengamat bisa mengarsip, men-dedupe, dan memverifikasi isinya.       ║
║  Batas: pemilik webhook (operator) tetap bisa menghapus pesan Discord —    ║
║  saksi ini kuat sejauh ada pihak luar yang ikut mengarsip.                 ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Mapping, Optional

import httpx

from world_registry import RandomnessSource, WorldRecord, STATUS_ACTIVE


EVENT_REGISTERED: str = "registered"
EVENT_ACTIVATED:  str = "activated"
EVENT_COMMITMENT: str = "commitment"

_TITLES: Dict[str, str] = {
    EVENT_REGISTERED: "🆕 Server terdaftar (pending)",
    EVENT_ACTIVATED:  "🔒 World nonce terkunci",
    EVENT_COMMITMENT: "📜 Pepper commitment",
}


def canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


@dataclass(frozen=True)
class WitnessEvent:
    key:     str                  # "registered:<gid>" | "activated:<gid>" | "commitment:<algo>"
    kind:    str
    payload: Mapping[str, object]

    @property
    def event_id(self) -> str:
        return hashlib.sha256(canonical_json(self.payload).encode("ascii")).hexdigest()[:16]


def events_for_record(record: WorldRecord, source: RandomnessSource) -> List[WitnessEvent]:
    base = {
        "guild_id":          record.guild_id,
        "algo_version":      record.algo_version,
        "registered_at":     record.registered_at.isoformat(),
        "randomness_source": record.source_id,
        "drand_round":       record.target_round,
        "drand_round_time":  source.round_time(record.target_round).isoformat(),
    }
    events = [WitnessEvent(f"{EVENT_REGISTERED}:{record.guild_id}", EVENT_REGISTERED,
                           {"event": EVENT_REGISTERED, **base})]
    if record.status == STATUS_ACTIVE:
        events.append(WitnessEvent(f"{EVENT_ACTIVATED}:{record.guild_id}", EVENT_ACTIVATED, {
            "event": EVENT_ACTIVATED, **base,
            "world_nonce":     record.world_nonce,
            "drand_signature": record.beacon.signature,
            "verify_url":      source.public_url(record.target_round),
        }))
    return events


def events_for_commitments(rows: Iterable[Mapping[str, object]]) -> List[WitnessEvent]:
    return [
        WitnessEvent(f"{EVENT_COMMITMENT}:{row['algo_version']}", EVENT_COMMITMENT, {
            "event":             EVENT_COMMITMENT,
            "algo_version":      row["algo_version"],
            "pepper_commitment": row["pepper_commitment"],
            "committed_at":      str(row["committed_at"]),
        })
        for row in rows
    ]


class WebhookWitness:
    """Kirim satu event ke webhook Discord.  Raise pada kegagalan apa pun (caller me-retry)."""

    TIMEOUT_SECONDS: float = 10.0

    def __init__(
        self,
        url: Optional[str],
        client_factory: Callable[[], httpx.Client] = lambda: httpx.Client(timeout=WebhookWitness.TIMEOUT_SECONDS),
    ) -> None:
        self._url = (url or "").strip() or None
        self._client_factory = client_factory

    @property
    def enabled(self) -> bool:
        return self._url is not None

    @staticmethod
    def message_for(event: WitnessEvent) -> dict:
        return {
            "username": "Bawan World Log",
            "content": (
                f"**{_TITLES.get(event.kind, event.kind)}** · event_id `{event.event_id}`\n"
                f"```json\n{canonical_json(event.payload)}\n```"
            ),
            "allowed_mentions": {"parse": []},
        }

    def send(self, event: WitnessEvent) -> None:
        if not self.enabled:
            raise RuntimeError("WORLD_LOG_WEBHOOK belum dikonfigurasi")
        with self._client_factory() as client:
            # wait=true → Discord only answers 200 once the message really exists.
            resp = client.post(self._url, params={"wait": "true"}, json=self.message_for(event))
            resp.raise_for_status()


def pending_events(
    records: Iterable[WorldRecord],
    source_for: Callable[[WorldRecord], RandomnessSource],
    commitment_rows: Iterable[Mapping[str, object]],
    delivered_keys: Iterable[str],
) -> List[WitnessEvent]:
    """Semua event yang seharusnya ada dikurangi yang sudah terkirim; commitment lebih dulu."""
    done = set(delivered_keys)
    expected = list(events_for_commitments(commitment_rows))
    for record in sorted(records, key=lambda r: (r.registered_at, r.guild_id)):
        expected.extend(events_for_record(record, source_for(record)))
    return [e for e in expected if e.key not in done]


# ─────────────────────────────────────────────────────────────────────────────
# SELF-TEST  —  python world_witness.py
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from datetime import datetime, timezone
    from world_registry import Beacon, DrandQuicknet

    failures = 0

    def check(label: str, got, expected) -> None:
        global failures
        ok = got == expected
        failures += 0 if ok else 1
        print(f"  {'✓' if ok else '✗'}  {label}: {got!r}" + ("" if ok else f"  (expected {expected!r})"))

    src = DrandQuicknet()
    t = datetime(2026, 9, 25, 12, 0, 0, 123456, tzinfo=timezone.utc)
    pending_rec = WorldRecord(111, "v1", src.source_id, t, src.round_after(t), None)
    sig = "ab" * 48
    active_rec = WorldRecord(222, "v1", src.source_id, t, src.round_after(t),
                             Beacon(src.source_id, src.round_after(t), hashlib.sha256(bytes.fromhex(sig)).hexdigest(), sig))
    commits = [{"algo_version": "v1", "pepper_commitment": "c" * 64, "committed_at": "2026-09-25T00:00:00+00:00"}]

    print("\n[1] Event diturunkan dari data")
    keys = [e.key for e in pending_events([pending_rec, active_rec], lambda r: src, commits, [])]
    check("urutan & kelengkapan", keys,
          ["commitment:v1", "registered:111", "registered:222", "activated:222"])
    act = events_for_record(active_rec, src)[1]
    check("activated memuat nonce & ronde", (act.payload["world_nonce"], act.payload["drand_round"]),
          (active_rec.world_nonce, active_rec.target_round))
    check("event_id deterministik", act.event_id, events_for_record(active_rec, src)[1].event_id)
    check("pesan < 2000 karakter (batas Discord)", len(WebhookWitness.message_for(act)["content"]) < 2000, True)

    print("\n[2] Yang sudah terkirim tidak dikirim ulang")
    left = pending_events([pending_rec, active_rec], lambda r: src, commits, ["commitment:v1", "registered:222"])
    check("sisa", [e.key for e in left], ["registered:111", "activated:222"])

    print("\n[3] Pengiriman webhook")
    seen: List[dict] = []
    def ok_handler(request: httpx.Request) -> httpx.Response:
        seen.append({"wait": request.url.params.get("wait"), "body": json.loads(request.content)})
        return httpx.Response(200, json={"id": "1"})
    w = WebhookWitness("https://discord.test/api/webhooks/1/x",
                       client_factory=lambda: httpx.Client(transport=httpx.MockTransport(ok_handler)))
    w.send(act)
    check("pakai wait=true", seen[0]["wait"], "true")
    check("tidak mention siapa pun", seen[0]["body"]["allowed_mentions"], {"parse": []})
    down = WebhookWitness("https://discord.test/api/webhooks/1/x", client_factory=lambda: httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(503))))
    try:
        down.send(act)
        check("webhook 503 → raise (caller retry)", "sukses", "raise")
    except httpx.HTTPStatusError:
        check("webhook 503 → raise (caller retry)", "raise", "raise")
    check("tanpa URL → disabled", WebhookWitness(None).enabled, False)

    print(f"\n{'SEMUA TES LULUS ✓' if failures == 0 else f'{failures} TES GAGAL ✗'}\n")
    raise SystemExit(1 if failures else 0)
