"""
Shows the buyer that a Spark payment was seen before it is mined, the way
light wallets show an incoming coin with 0 confirmations.

The node's mobile RPCs (getmempoolsparktxids / getmempoolsparktxs, which need
`mobile=1` in firo.conf) expose the serialized output coins of unmined Spark
transactions. Each coin is checked against the merchants' view keys exactly
like the block scanner does.

A sighting is kept in memory only and is never credited: no amount is added
to the payment, no status changes, no webhook, no redirect. Crediting and
confirmation stay with the block scanner and the merchant's confirmation
policy. If the transaction is dropped from the mempool the sighting simply
disappears.
"""
from datetime import datetime, timezone
import time

from loguru import logger
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.models import Payment, PaymentStatus
from app.models.spark import SparkWalletConnection
from app.services.event_bus import EventBus, make_event
from app.services.firo_rpc import get_rpc, FiroRPCError
from app.services.sparkmobile import SparkViewKeyError

_SIGHTING_TTL = 6 * 3600
_BATCH = 40

_sightings: dict[str, dict] = {}
_checked: set[str] = set()
_disabled_until = 0.0


def sighting_for(payment_id: str) -> dict | None:
    s = _sightings.get(str(payment_id))
    if not s:
        return None
    if time.time() - s["ts"] > _SIGHTING_TTL:
        _sightings.pop(str(payment_id), None)
        return None
    return {
        "amount_firo": round(s["sats"] / 1e8, 8),
        "txid": s["txid"],
        "instant_locked": bool(s["locked"]),
        "seen_at": s["seen_at"],
    }


def _hex_txid(b64: str) -> str:
    from app.services.payment_engine import _to_explorer_txid
    return _to_explorer_txid(b64)


async def check_spark_mempool() -> None:
    global _disabled_until
    if time.time() < _disabled_until:
        return
    rpc = get_rpc()
    try:
        ids = await rpc.call("getmempoolsparktxids") or []
    except FiroRPCError as e:
        _disabled_until = time.time() + 600
        logger.info(f"[spark_mempool] mempool Spark RPC unavailable (needs mobile=1): {e}")
        return

    now = time.time()
    for pid in [k for k, v in _sightings.items() if now - v["ts"] > _SIGHTING_TTL]:
        _sightings.pop(pid, None)

    current = set(ids)
    _checked.intersection_update(current)
    fresh = [i for i in ids if i not in _checked]
    if not fresh:
        return

    async with AsyncSessionLocal() as db:
        conns = (await db.execute(
            select(SparkWalletConnection).where(SparkWalletConnection.is_active == True)
        )).scalars().all()
        if not conns:
            _checked.update(fresh)
            return
        pending = (await db.execute(
            select(Payment).where(
                Payment.spark_diversifier.is_not(None),
                Payment.status == PaymentStatus.pending,
            )
        )).scalars().all()
        if not pending:
            _checked.update(fresh)
            return
        by_key = {(p.spark_owner_id or p.merchant_id, p.spark_diversifier): p for p in pending}

        from app.services.payment_engine import _get_view_key
        keys = [(c.merchant_id, _get_view_key(c)) for c in conns]
        keys = [(m, vk) for m, vk in keys if vk is not None]

        for start in range(0, len(fresh), _BATCH):
            chunk = fresh[start:start + _BATCH]
            try:
                data = await rpc.call("getmempoolsparktxs", {"txids": [_hex_txid(i) for i in chunk]}) or {}
            except FiroRPCError as e:
                logger.warning(f"[spark_mempool] getmempoolsparktxs failed: {e}")
                return
            _checked.update(chunk)
            for tx_b64, info in data.items():
                ctx = (info.get("serial_context") or [None])[0]
                coins = info.get("coins") or []
                if not ctx or not coins:
                    continue
                locked = bool(info.get("isLocked"))
                for coin_b64 in coins:
                    for merchant_id, vk in keys:
                        try:
                            found = vk.identify_coin(coin_b64, ctx)
                        except SparkViewKeyError:
                            continue
                        if found is None:
                            continue
                        payment = by_key.get((merchant_id, found.diversifier))
                        if payment is None:
                            continue
                        _remember(payment, found, tx_b64, locked)


def _remember(payment: Payment, found, tx_b64: str, locked: bool) -> None:
    pid = str(payment.id)
    s = _sightings.get(pid)
    if s is None:
        s = _sightings[pid] = {"sats": 0, "tags": set(), "txid": None, "locked": False,
                               "ts": time.time(), "seen_at": datetime.now(timezone.utc).isoformat()}
    if found.coin_tag in s["tags"]:
        return
    s["tags"].add(found.coin_tag)
    s["sats"] += int(found.value)
    s["txid"] = _hex_txid(tx_b64)
    s["locked"] = s["locked"] or locked
    s["ts"] = time.time()
    logger.info(f"[spark_mempool] payment {pid[:8]} seen unconfirmed: {s['sats'] / 1e8:.8f} FIRO")
    try:
        import asyncio
        asyncio.get_running_loop().create_task(EventBus.publish_payment(pid, make_event(
            "payment.seen", payment_id=pid,
            amount_firo=round(s["sats"] / 1e8, 8), instant_locked=bool(s["locked"]),
        )))
    except RuntimeError:
        pass
