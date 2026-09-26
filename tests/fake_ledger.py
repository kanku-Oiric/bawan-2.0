"""
In-memory reference model of the schema-v7 ledger RPCs (supabase/schema.sql).

Same method names, arguments, return values and rule codes as
db_ekonomi_pusat.EconomyDatabase, so bot code can be tested offline.  Every
method is one "transaction": it validates first and mutates only if every
check passed.  The real guarantees (row locks, triggers, CHECK constraints)
are tested against Postgres by `python db_ekonomi_pusat.py --live`.

Fault injection
    down                 every call raises ConnectionError before doing anything
    swing_down           only begin_swing is unavailable (attempt counter offline)
    lose_response(name)  the next call of `name` COMMITS, then raises TimeoutError
                         (the response was lost — the bot cannot know it succeeded)
"""

from __future__ import annotations

import copy
import json
import threading
import time
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Set, Tuple

from db_ekonomi_pusat import LedgerRejected, money_str
from voice_engine import level_for_xp


class LedgerModel:
    POLICY_DEFAULTS = {"stamina_cap": 100.0, "stamina_regen_per_second": 0.0,
                       "max_swings_per_minute": None, "rest_enabled": True}

    def __init__(self, clock=time.time) -> None:
        self.clock = clock
        self.lock = threading.RLock()
        self.policy: dict = dict(self.POLICY_DEFAULTS)
        self.players: Dict[Tuple[int, int], dict] = {}
        self.counters: Dict[Tuple[int, int], int] = {}
        self.ledger: List[dict] = []
        self.ledger_refs: Set[str] = set()
        self.items: Dict[str, dict] = {}
        self.disposals: Dict[str, dict] = {}
        self.mining_results: Dict[Tuple[int, int, int], dict] = {}
        self.voice_ticks: Set[str] = set()
        self.down = False
        self.swing_down = False
        self._lose: Set[str] = set()
        self.calls: List[str] = []

    # ── fault injection ─────────────────────────────────────────────────────

    def lose_response(self, name: str) -> None:
        self._lose.add(name)

    def _enter(self, name: str) -> None:
        self.calls.append(name)
        if self.down:
            raise ConnectionError(f"DB mati (simulasi) — {name}")

    def _exit(self, name: str, result):
        if name in self._lose:
            self._lose.discard(name)
            raise TimeoutError(f"respons {name} hilang setelah commit (simulasi)")
        return result

    # ── internals mirroring bawan_private.* ─────────────────────────────────

    def _player(self, g: int, u: int) -> dict:
        return self.players.setdefault((g, u), {
            "guild_id": g, "user_id": u, "stamina": 100.0, "stamina_at": self.clock(),
            "pickaxe_key": "copper_starter", "wallet": Decimal("0.0000"), "xp": 0, "level": 1,
            "voice_seconds": 0.0, "automine": False,
        })

    def _stamina_now(self, p: dict) -> float:
        regen = self.policy["stamina_regen_per_second"] * max(0.0, self.clock() - p["stamina_at"])
        return min(self.policy["stamina_cap"], p["stamina"] + regen)

    def _swings_last_minute(self, g: int, u: int) -> int:
        now = self.clock()
        return sum(1 for (gg, uu, _), r in self.mining_results.items() if (gg, uu) == (g, u) and r["t"] > now - 60)

    @staticmethod
    def _amount(value) -> Decimal:
        d = Decimal(str(value))
        if d == 0 or d != d.quantize(Decimal("0.0001")):
            raise LedgerRejected("bad_amount")
        return d

    def _ledger_post(self, ref: str, g: int, u: int, kind: str, amount: Decimal, item_id: Optional[str]) -> Decimal:
        if ref in self.ledger_refs:
            raise LedgerRejected("duplicate_ref")                    # = UNIQUE violation
        p = self._player(g, u)
        balance = p["wallet"] + amount
        if balance < 0:
            raise LedgerRejected("negative_balance")                 # = CHECK wallet >= 0
        p["wallet"] = balance
        self.ledger_refs.add(ref)
        self.ledger.append({"id": len(self.ledger) + 1, "ref": ref, "guild_id": g, "user_id": u, "kind": kind,
                            "amount": amount, "balance_after": balance, "item_id": item_id})
        return balance

    # ── reads ───────────────────────────────────────────────────────────────

    def load_player_rows(self) -> List[dict]:
        self._enter("load_player_rows")
        with self.lock:
            return [dict(p) for _, p in sorted(self.players.items())]

    def load_held_items(self) -> List[dict]:
        self._enter("load_held_items")
        with self.lock:
            return [copy.deepcopy(r) for r in self.items.values() if r["item_id"] not in self.disposals]

    def money_supply(self, g: int) -> Decimal:
        self._enter("money_supply")
        with self.lock:
            total = sum((p["wallet"] for (gg, _), p in self.players.items() if gg == g), Decimal(0))
            return self._exit("money_supply", total.quantize(Decimal("0.0001")))

    def ledger_audit(self) -> dict:
        with self.lock:
            sums: Dict[Tuple[int, int], Decimal] = {}
            chain_broken = 0
            last: Dict[Tuple[int, int], Decimal] = {}
            for row in self.ledger:
                key = (row["guild_id"], row["user_id"])
                sums[key] = sums.get(key, Decimal(0)) + row["amount"]
                chain_broken += row["balance_after"] != last.get(key, Decimal(0)) + row["amount"]
                last[key] = row["balance_after"]
            return {
                "wallet_mismatch": [{"guild_id": g, "user_id": u, "wallet": str(p["wallet"]),
                                     "ledger_sum": str(sums.get((g, u), Decimal(0)))}
                                    for (g, u), p in self.players.items() if p["wallet"] != sums.get((g, u), Decimal(0))],
                "ledger_without_player": sum(1 for k in sums if k not in self.players),
                "balance_chain_broken": chain_broken,
                "sale_without_disposal": sum(1 for r in self.ledger if r["kind"] in ("sell_ore", "sell_crystal")
                                             and not any(d["ledger_ref"] == r["ref"] for d in self.disposals.values())),
                "results_beyond_counter": sum(1 for (g, u, n) in self.mining_results if n > self.counters.get((g, u), 0)),
            }

    # ── RPCs ────────────────────────────────────────────────────────────────

    def next_mining_attempt(self, g: int, u: int) -> int:
        self._enter("next_mining_attempt")
        with self.lock:
            self.counters[(g, u)] = self.counters.get((g, u), 0) + 1
            return self._exit("next_mining_attempt", self.counters[(g, u)])

    def begin_swing(self, g: int, u: int) -> Tuple[int, float]:
        self._enter("begin_swing")
        if self.swing_down:
            raise ConnectionError("pencatat percobaan mati (simulasi)")
        with self.lock:
            p = self._player(g, u)
            limit = self.policy["max_swings_per_minute"]
            if limit is not None and self._swings_last_minute(g, u) >= limit:
                raise LedgerRejected("rate_limited")
            self.counters[(g, u)] = self.counters.get((g, u), 0) + 1
            return self._exit("begin_swing", (self.counters[(g, u)], self._stamina_now(p)))

    def record_swing(self, g: int, u: int, attempt: int, *, node_id: str, success: bool, critical_hit: bool,
                     amount_extracted: float, stamina_consumed: float, item_kind: Optional[str],
                     item_uuid: Optional[str], item_payload: Optional[dict]) -> Tuple[float, Optional[str], bool]:
        self._enter("record_swing")
        with self.lock:
            prev = self.mining_results.get((g, u, attempt))
            if prev is not None:
                return self._exit("record_swing", (prev["stamina_after"], prev["item_id"], True))
            if attempt < 1 or attempt > self.counters.get((g, u), 0):
                raise LedgerRejected("attempt_not_issued")
            if stamina_consumed < 0 or amount_extracted < 0:
                raise LedgerRejected("bad_amount")
            p = self.players.get((g, u))
            if p is None:
                raise LedgerRejected("no_player")
            limit = self.policy["max_swings_per_minute"]
            if limit is not None and self._swings_last_minute(g, u) >= limit:
                raise LedgerRejected("rate_limited")
            before = self._stamina_now(p)
            if stamina_consumed > before + 1e-9:
                raise LedgerRejected("insufficient_stamina")
            item_id = None
            if item_payload is not None:
                if item_kind not in ("ore", "crystal") or item_uuid is None:
                    raise LedgerRejected("bad_item")
                item_id = f"swing:{g}:{u}:{attempt}"
            after = max(0.0, before - stamina_consumed)
            p["stamina"], p["stamina_at"] = after, self.clock()
            if item_id is not None:
                self.items[item_id] = {"item_id": item_id, "guild_id": g, "owner_id": u, "kind": item_kind,
                                       "item_uuid": item_uuid, "payload": json.loads(json.dumps(item_payload))}
            self.mining_results[(g, u, attempt)] = {
                "node_id": node_id, "success": success, "critical_hit": critical_hit,
                "amount_extracted": amount_extracted, "stamina_before": before,
                "stamina_consumed": stamina_consumed, "stamina_after": after, "item_id": item_id, "t": self.clock(),
            }
            return self._exit("record_swing", (after, item_id, False))

    def sell_item(self, g: int, u: int, item_id: str, payout, kind: str) -> Tuple[Decimal, bool]:
        self._enter("sell_item")
        amount = Decimal(money_str(payout))
        with self.lock:
            if kind not in ("sell_ore", "sell_crystal"):
                raise LedgerRejected("bad_kind")
            if amount <= 0:
                raise LedgerRejected("bad_amount")
            item = self.items.get(item_id)
            if item is None or item["guild_id"] != g or item["owner_id"] != u:
                raise LedgerRejected("item_not_owned")
            if (kind == "sell_ore") != (item["kind"] == "ore"):
                raise LedgerRejected("bad_kind")
            ref = f"sell:{item_id}"
            if item_id in self.disposals:
                if ref in self.ledger_refs:
                    return self._exit("sell_item", (self.players[(g, u)]["wallet"], True))
                raise LedgerRejected("item_already_disposed")
            balance = self._ledger_post(ref, g, u, kind, amount, item_id)
            self.disposals[item_id] = {"item_id": item_id, "kind": "sold", "ledger_ref": ref}
            return self._exit("sell_item", (balance, False))

    def apply_voice_tick(self, g: int, ref: str, entries: Sequence[dict]) -> List[dict]:
        self._enter("apply_voice_tick")
        parsed = [(int(e["user_id"]), Decimal(money_str(e.get("coin", 0))), int(e.get("xp", 0)),
                   float(e.get("voice_seconds", 0.0))) for e in entries]
        with self.lock:
            applied = ref not in self.voice_ticks
            if applied:
                if any(xp < 0 or secs < 0 for _, _, xp, secs in parsed):
                    raise LedgerRejected("bad_amount")
                if len({uid for uid, *_ in parsed}) != len(parsed):
                    raise LedgerRejected("duplicate_ref")
                self.voice_ticks.add(ref)
                for uid, coin, xp, secs in parsed:
                    p = self._player(g, uid)
                    p["voice_seconds"] += secs
                    p["xp"] += xp
                    p["level"] = level_for_xp(p["xp"])
                    if coin > 0:
                        self._ledger_post(f"{ref}:{uid}", g, uid, "voice_reward", coin, None)
            rows = [{"user_id": uid, "wallet": self.players[(g, uid)]["wallet"], "xp": self.players[(g, uid)]["xp"],
                     "level": self.players[(g, uid)]["level"],
                     "voice_seconds": self.players[(g, uid)]["voice_seconds"], "applied": applied}
                    for uid, *_ in parsed if (g, uid) in self.players]
            return self._exit("apply_voice_tick", rows)

    def rest_player(self, g: int, u: int) -> Tuple[float, float]:
        self._enter("rest_player")
        with self.lock:
            if not self.policy["rest_enabled"]:
                raise LedgerRejected("rest_disabled")
            p = self._player(g, u)
            before = self._stamina_now(p)
            p["stamina"], p["stamina_at"] = self.policy["stamina_cap"], self.clock()
            return self._exit("rest_player", (before, self.policy["stamina_cap"]))

    def set_player_prefs(self, g: int, u: int, pickaxe_key: Optional[str] = None,
                         automine: Optional[bool] = None) -> Tuple[str, bool]:
        self._enter("set_player_prefs")
        with self.lock:
            p = self._player(g, u)
            if pickaxe_key is not None:
                p["pickaxe_key"] = pickaxe_key
            if automine is not None:
                p["automine"] = automine
            return self._exit("set_player_prefs", (p["pickaxe_key"], p["automine"]))
