"""
Transparent-to-Spark privacy watcher.

Tracks what happens to each transparent coin received at an invoice address:
still unspent, spent by a Spark mint, or spent some other way. It only reads
from the node (address index, raw transactions) and uses the merchant's
Spark view key to check whether a mint's Spark outputs belong to the
connected Spark wallet.

It never signs, mints, imports keys or changes a payment. Each pass rebuilds
the state of every unfinished row from the chain, so a crash, node restart,
dropped transaction or reorg is handled by simply running again.

Node requirements: addressindex=1 and txindex=1.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.database import AsyncSessionLocal
from app.models.models import Payment, PaymentStatus, XpubWalletConnection
from app.models.privacy import PaymentPrivacyInput, PrivacySettings
from app.models.spark import SparkWalletConnection
from app.services import privacy_core as core

_state: dict = {"checked_at": None, "last_error": None, "node_ready": None, "rows_checked": 0}
_lock = asyncio.Lock()
_ADDR_CHUNK = 200


def watcher_state() -> dict:
    return dict(_state)


def _aware(dt):
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class NodeNotReady(Exception):
    pass


class _Chain:
    """Read-only node access with a per-pass cache."""

    def __init__(self, rpc):
        self.rpc = rpc
        self._tx: dict[str, dict | None] = {}

    async def tx(self, txid: str) -> dict | None:
        if txid not in self._tx:
            try:
                self._tx[txid] = await self.rpc.call("getrawtransaction", txid, 1)
            except Exception as e:
                logger.debug(f"[privacy] getrawtransaction {txid[:12]} failed: {e}")
                self._tx[txid] = None
        return self._tx[txid]

    async def input_total(self, tx: dict) -> int | None:
        total = 0
        for i in tx.get("vin") or []:
            if "txid" not in i:
                return None
            if "valueSat" in i:
                total += int(i["valueSat"])
                continue
            if "value" in i:
                total += core.sats(i["value"])
                continue
            prev = await self.tx(i["txid"])
            if prev is None:
                return None
            outs = prev.get("vout") or []
            n = int(i["vout"])
            if n >= len(outs):
                return None
            total += core.output_sats(outs[n])
        return total

    async def scan(self, addresses: list[str]) -> tuple[dict, dict]:
        """credits: address -> [credit]; spent: (txid, vout) -> spending txid."""
        credits: dict[str, list[dict]] = {a: [] for a in addresses}
        spent: dict[tuple[str, int], str] = {}
        for start in range(0, len(addresses), _ADDR_CHUNK):
            chunk = addresses[start:start + _ADDR_CHUNK]
            try:
                deltas = await self.rpc.call("getaddressdeltas", {"addresses": chunk}) or []
                mem = await self.rpc.call("getaddressmempool", {"addresses": chunk}) or []
            except Exception as e:
                raise NodeNotReady(str(e)[:160])
            for d in deltas:
                s = int(d.get("satoshis", 0))
                if s > 0 and d.get("address") in credits:
                    credits[d["address"]].append({"txid": d["txid"], "vout": int(d.get("index", 0)), "sats": s,
                                                  "height": d.get("height"), "time": None})
                elif s < 0:
                    tx = await self.tx(d["txid"])
                    vin = (tx or {}).get("vin") or []
                    idx = int(d.get("index", -1))
                    if 0 <= idx < len(vin) and "txid" in vin[idx]:
                        spent[(vin[idx]["txid"], int(vin[idx]["vout"]))] = d["txid"]
            for d in mem:
                s = int(d.get("satoshis", 0))
                if s > 0 and d.get("address") in credits:
                    credits[d["address"]].append({"txid": d["txid"], "vout": int(d.get("index", 0)), "sats": s,
                                                  "height": None, "time": d.get("timestamp")})
                elif s < 0 and d.get("prevtxid") is not None:
                    spent.setdefault((d["prevtxid"], int(d.get("prevout", 0))), d["txid"])
        return credits, spent

    async def spender(self, address: str, txid: str, vout: int) -> str | None:
        _, spent = await self.scan([address])
        return spent.get((txid, vout))


def _matches(vk, tx: dict) -> dict[int, tuple[int, str] | None]:
    mints = core.mint_outputs(tx)
    if not mints:
        return {}
    if vk is None:
        return {n: None for n, _, _ in mints}
    import base64
    try:
        ctx = base64.b64encode(core.serial_context(tx.get("vin") or [])).decode()
    except ValueError:
        return {n: None for n, _, _ in mints}
    out: dict[int, tuple[int, str] | None] = {}
    for n, _, coin in mints:
        try:
            found = vk.identify_coin(coin, ctx)
        except Exception:
            found = None
        out[n] = (int(found.value), found.coin_tag) if found is not None else None
    return out


async def trace_outpoint(chain: _Chain, vk, txid: str, vout: int, own_sats: int,
                         spent: dict) -> tuple[core.Trace, str | None]:
    """Follows one invoice coin. Returns the trace and an optional detail."""
    t = core.Trace()
    spend_txid = spent.get((txid, vout))
    if not spend_txid:
        return t, None
    tx = await chain.tx(spend_txid)
    if tx is None:
        return t, "Spending transaction not readable. Check txindex=1."
    total = await chain.input_total(tx)
    if not total:
        return t, "Input values not readable."
    summary = core.summarize_spend(tx, total, _matches(vk, tx))
    outs = core.apply_spend(t, summary, own_sats, first=True)
    if not summary.has_mint:
        return t, None
    t.change_sats = sum(s for _, s, _ in outs)
    while outs and len(outs) == 1 and outs[0][2] and t.hops < core.MAX_CHANGE_HOPS:
        n, part, addr = outs[0]
        t.change_outpoint = f"{tx['txid']}:{n}"
        nxt_id = await chain.spender(addr, tx["txid"], n)
        if not nxt_id:
            t.change_followable = True
            break
        nxt = await chain.tx(nxt_id)
        nxt_total = await chain.input_total(nxt) if nxt else None
        if not nxt or not nxt_total:
            t.change_followable = True
            break
        nsum = core.summarize_spend(nxt, nxt_total, _matches(vk, nxt))
        if not nsum.has_mint:
            t.see(nsum.confirmations)
            t.hops += 1
            break
        outs = core.apply_spend(t, nsum, part, first=False)
        t.hops += 1
        t.change_sats = sum(s for _, s, _ in outs)
        tx = nxt
    if vk is None and t.unmatched_sats:
        return t, "No Spark view key connected."
    return t, None


def _credit_ok(payment: Payment, c: dict) -> bool:
    if c["height"] is not None and payment.start_block_height is not None and c["height"] < payment.start_block_height:
        return False
    created = _aware(payment.created_at)
    if c["height"] is None and c["time"] and created and datetime.fromtimestamp(int(c["time"]), tz=timezone.utc) < created:
        return False
    return True


def _apply_row(row: PaymentPrivacyInput, credit: dict | None, tip: int | None, trace: core.Trace,
               detail: str | None, now: datetime) -> bool:
    found = credit is not None
    height = credit["height"] if credit else row.height
    confs = (tip - height + 1) if (found and tip is not None and height is not None) else 0
    state, final = core.decide(found, confs, trace)
    if not found:
        detail = "Payment transaction not found on the node right now."
    values = {
        "height": height,
        "confirmations": max(0, confs),
        "credit_found": found,
        "state": state,
        "final": final,
        "spend_txid": trace.spend_txid,
        "spend_confirmations": trace.spend_confirmations,
        "mint_txid": trace.mint_txid,
        "minted_sats": trace.minted_sats,
        "unmatched_sats": trace.unmatched_sats,
        "fee_sats": trace.fee_sats,
        "change_sats": trace.change_sats,
        "change_outpoint": trace.change_outpoint,
        "change_hops": trace.hops,
        "coin_tags": json.dumps(trace.coin_tags) if trace.coin_tags else None,
        "detail": detail,
    }
    changed = False
    for k, v in values.items():
        if getattr(row, k) != v:
            setattr(row, k, v)
            changed = True
    row.checked_at = now
    return changed


async def _view_keys(db, merchant_ids: set[str]) -> dict:
    from app.services.payment_engine import _get_view_key
    res = await db.execute(select(SparkWalletConnection).where(
        SparkWalletConnection.merchant_id.in_(merchant_ids), SparkWalletConnection.is_active == True))
    return {c.merchant_id: _get_view_key(c) for c in res.scalars().all()}


async def _merchants(db) -> set[str]:
    res = await db.execute(select(XpubWalletConnection.merchant_id).where(XpubWalletConnection.is_active == True))
    ids = {r for r in res.scalars().all()}
    if not ids:
        return ids
    off = await db.execute(select(PrivacySettings.merchant_id).where(
        PrivacySettings.merchant_id.in_(ids), PrivacySettings.tracking_enabled == False))
    return ids - set(off.scalars().all())


async def run_pass(rpc, db) -> int:
    """One full pass. Returns the number of rows checked."""
    now = datetime.now(timezone.utc)
    merchants = await _merchants(db)
    if not merchants:
        return 0
    res = await db.execute(select(Payment).where(
        Payment.transparent_address.is_not(None),
        Payment.merchant_id.in_(merchants),
    ))
    payments = [p for p in res.scalars().all()
                if (p.transparent_received or 0) > 0 or p.status in (PaymentStatus.pending, PaymentStatus.confirming)]
    if not payments:
        return 0
    rows_res = await db.execute(select(PaymentPrivacyInput).where(
        PaymentPrivacyInput.payment_id.in_([p.id for p in payments])))
    by_payment: dict[str, list[PaymentPrivacyInput]] = {}
    for r in rows_res.scalars().all():
        by_payment.setdefault(r.payment_id, []).append(r)

    active = []
    for p in payments:
        rows = by_payment.get(p.id, [])
        open_status = p.status in (PaymentStatus.pending, PaymentStatus.confirming)
        if rows and all(r.final for r in rows) and not open_status:
            continue
        active.append(p)
    if not active:
        return 0

    chain = _Chain(rpc)
    try:
        tip = await rpc.call("getblockcount")
    except Exception as e:
        raise NodeNotReady(str(e)[:160])
    addresses = sorted({p.transparent_address for p in active})
    credits, spent = await chain.scan(addresses)
    keys = await _view_keys(db, {p.merchant_id for p in active})

    checked = 0
    for p in active:
        rows = {(r.txid, r.vout): r for r in by_payment.get(p.id, [])}
        found = {(c["txid"], c["vout"]): c for c in credits.get(p.transparent_address, []) if _credit_ok(p, c)}
        for key, c in found.items():
            if key in rows:
                continue
            exists = (await db.execute(select(PaymentPrivacyInput).where(
                PaymentPrivacyInput.txid == key[0], PaymentPrivacyInput.vout == key[1]))).scalar_one_or_none()
            if exists is not None:
                if exists.payment_id == p.id:
                    rows[key] = exists
                continue
            row = PaymentPrivacyInput(payment_id=p.id, merchant_id=p.merchant_id, txid=key[0], vout=key[1],
                                      address=p.transparent_address, sats=c["sats"], height=c["height"],
                                      first_seen_at=now, state_changed_at=now)
            db.add(row)
            rows[key] = row
        vk = keys.get(p.merchant_id)
        for key, row in rows.items():
            if row.final:
                continue
            trace, detail = await trace_outpoint(chain, vk, row.txid, row.vout, row.sats, spent)
            prev_state = row.state
            _apply_row(row, found.get(key), tip, trace, detail, now)
            if row.state != prev_state:
                row.state_changed_at = now
            checked += 1
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            logger.warning(f"[privacy] duplicate outpoint skipped for payment {p.id[:8]}")
    return checked


async def check_privacy_inputs() -> None:
    if _lock.locked():
        return
    async with _lock:
        from app.services.firo_rpc import get_rpc
        _state["checked_at"] = datetime.now(timezone.utc).isoformat()
        try:
            async with AsyncSessionLocal() as db:
                _state["rows_checked"] = await run_pass(get_rpc(), db)
            _state["node_ready"] = True
            _state["last_error"] = None
        except NodeNotReady as e:
            _state["node_ready"] = False
            _state["last_error"] = str(e)
            logger.info(f"[privacy] node not ready (needs addressindex=1 and txindex=1): {e}")
        except Exception as e:
            _state["last_error"] = str(e)[:160]
            logger.warning(f"[privacy] watcher error: {e}")
