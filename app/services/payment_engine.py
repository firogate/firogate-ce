"""
Detects Spark payments without spend authority: fetches the public Spark
anonymity set from the node (chain-wide, not merchant-specific), and for
each new coin, checks it against every connected merchant's view key
locally. A match reveals the diversifier (which order), the paid amount,
and a unique coin tag all via app/services/sparkmobile.py, which never
touches spend keys.

Mirrors app/services/payment_monitor.py's structure and reuses its
_confirm_payment()/_write_audit_log() so Spark payments go through the
exact same webhook/email/analytics pipeline as transparent ones.
"""
from datetime import datetime, timezone
import asyncio
import base64
import json
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.core.config import get_settings
from app.core.security import decrypt_field
from app.models.models import Payment, PaymentStatus, PaymentAuditEvent
from app.models.spark import SparkWalletConnection, SparkScanState
from app.services.event_bus import EventBus, make_event
from app.services.firo_rpc import get_rpc, FiroRPCError
from app.services.sparkmobile import SparkViewKey, SparkViewKeyError

settings = get_settings()

# Cache deserialized view keys per merchant for the process lifetime avoid
# re-deserializing the same key on every 20s poll. Invalidated on reconnect
# (see app/api/spark_connect.py).
_view_key_cache: dict[str, SparkViewKey] = {}


def invalidate_view_key_cache(merchant_id: str) -> None:
    _view_key_cache.pop(merchant_id, None)


def _get_view_key(conn: SparkWalletConnection) -> SparkViewKey | None:
    cached = _view_key_cache.get(conn.merchant_id)
    if cached is not None:
        return cached
    try:
        raw = decrypt_field(conn.view_key_enc)
        vk = SparkViewKey(raw, is_testnet=(conn.network != "mainnet"))
    except Exception as e:
        logger.error(f"[spark_scanner] bad view key for merchant={conn.merchant_id[:8]}: {type(e).__name__}")
        return None
    _view_key_cache[conn.merchant_id] = vk
    return vk


async def check_spark_payments():
    async with AsyncSessionLocal() as db:
        try:
            await _run_spark_scan(db)
        except Exception as e:
            logger.exception(f"Spark scanner error: {e}")


async def _run_spark_scan(db: AsyncSession):
    res = await db.execute(
        select(SparkWalletConnection).where(SparkWalletConnection.is_active == True)
    )
    connections: list[SparkWalletConnection] = res.scalars().all()

    if not connections:
        return

    rpc = get_rpc()

    # This cursor is group-based, not height-based, because Spark coins are
    # only enumerable via getsparkanonymityset(group, start_hash) — there is
    # no per-block Spark coin listing RPC. In steady state it already never
    # rescans from genesis: it resumes from state.coin_group_id/last_block_hash.
    # On a FRESH/wiped DB it walks every group once, by necessity (a newly
    # connected view key could match any historical coin — there's no cheaper
    # discovery method). Do not "fix" that by adding a parallel height cursor;
    # instead each invoice carries its own Payment.start_block_height floor
    # (set in spark_connect_helpers.get_next_spark_address) so a coin older
    # than the invoice is rejected in the matching loop below regardless of
    # where this cursor currently sits.
    res_state = await db.execute(select(SparkScanState).where(SparkScanState.id == 1))
    state = res_state.scalar_one_or_none()
    if state is None:
        state = SparkScanState(id=1, coin_group_id=0, last_block_hash=None)
        db.add(state)
        await db.flush()

    try:
        latest_group = await rpc.get_spark_latest_coin_id()
    except FiroRPCError as e:
        logger.warning(f"[spark_scanner] getsparklatestcoinid failed: {e}")
        return

    try:
        chain_tip = (await rpc.get_blockchain_info()).get("blocks")
    except FiroRPCError:
        chain_tip = None

    # Walk every group from our last-seen one up to the current tip. A new
    # group starting means the previous one is closed/full nothing more
    # will ever be appended to it, so we never need to revisit it once past.
    new_coins: list[tuple] = []  # (serializedCoin_b64, txHash, context_b64)
    group = max(state.coin_group_id, 1)
    start_hash = state.last_block_hash or ""
    last_result_hash = start_hash

    while group <= latest_group:
        try:
            result = await rpc.get_spark_anonymity_set(group, start_hash)
        except FiroRPCError as e:
            logger.warning(f"[spark_scanner] getsparkanonymityset({group}) failed: {e}")
            break

        coins = result.get("coins") or []
        for c in coins:
            if len(c) != 3:
                continue
            new_coins.append(tuple(c))
        last_result_hash = result.get("blockHash") or last_result_hash

        if group < latest_group:
            # Fully consumed this (now-closed) group move to the next one
            # from scratch (no startBlockHash carry-over across groups).
            group += 1
            start_hash = ""
            last_result_hash = ""
        else:
            break  # caught up to the tip of the current (still-open) group

    now = datetime.now(timezone.utc)
    tx_chain_time_cache: dict[str, tuple[int | None, int | None]] = {}

    if new_coins:
        logger.debug(f"[spark_scanner] {len(new_coins)} new coin(s) across {len(connections)} connection(s)")

        # Preload pending/confirming Spark payments per merchant so a match can
        # be resolved to an order without a query per coin.
        res_pay = await db.execute(
            select(Payment).where(
                Payment.address_type == "spark",
                Payment.status.in_([PaymentStatus.pending, PaymentStatus.confirming]),
            )
        )
        pending_by_key: dict[tuple, Payment] = {
            (p.spark_owner_id or p.merchant_id, p.spark_diversifier): p for p in res_pay.scalars().all()
        }

        scanned = 0
        for conn in connections:
            vk = _get_view_key(conn)
            if vk is None:
                continue

            for serialized_b64, tx_hash, context_b64 in new_coins:
                scanned += 1
                if scanned % 200 == 0:
                    await asyncio.sleep(0)

                try:
                    identified = vk.identify_coin(serialized_b64, context_b64)
                except SparkViewKeyError as e:
                    logger.warning(f"[spark_scanner] identify_coin error: {e}")
                    continue
                if identified is None:
                    continue

                payment = pending_by_key.get((conn.merchant_id, identified.diversifier))
                if payment is None:
                    # coin belongs to this merchant but isn't a tracked order
                    # (e.g. their own change/unrelated tx)
                    continue

                credited = json.loads(payment.spark_coin_tags_json) if payment.spark_coin_tags_json else []
                if identified.coin_tag in credited:
                    continue  # already processed, avoid double-crediting the same coin

                explorer_txid = _to_explorer_txid(tx_hash)
                coin_height, coin_time = await _resolve_coin_chain_time(rpc, explorer_txid, tx_chain_time_cache)

                if coin_height is None and coin_time is None:
                    logger.warning(
                        f"[spark_scanner] could not resolve chain time for txid={explorer_txid[:12]}… "
                        f"skipping this pass, will retry"
                    )
                    continue

                if payment.start_block_height is not None and coin_height is not None:
                    if coin_height < payment.start_block_height:
                        logger.info(
                            f"[spark_scanner] rejecting historical coin for payment {payment.id[:8]}: "
                            f"coin_height={coin_height} < start_block_height={payment.start_block_height}"
                        )
                        continue

                payment_created = payment.created_at
                if payment_created and payment_created.tzinfo is None:
                    payment_created = payment_created.replace(tzinfo=timezone.utc)
                if coin_time is not None and payment_created is not None:
                    coin_dt = datetime.fromtimestamp(coin_time, tz=timezone.utc)
                    if coin_dt < payment_created:
                        logger.info(
                            f"[spark_scanner] rejecting historical coin for payment {payment.id[:8]}: "
                            f"coin_time={coin_dt} < created_at={payment_created}"
                        )
                        continue

                await _apply_spark_payment(db, payment, identified, tx_hash, now, credited, coin_height, chain_tip)

    # Re-check confirmation depth for payments already fully-received but
    # still awaiting depth (status=confirming). The loop above only sees
    # newly-appeared anonymity-set coins, so a payment waiting purely on
    # depth — with no new coin arriving — would otherwise never be
    # re-evaluated once its coin stops being "new." Runs every pass
    # (even when new_coins is empty), which is the whole point of this block.
    if chain_tip is not None:
        from app.services.payment_monitor import _confirm_payment
        res_confirming = await db.execute(
            select(Payment).where(
                Payment.address_type == "spark",
                Payment.status == PaymentStatus.confirming,
                Payment.txid.is_not(None),
            )
        )
        for payment in res_confirming.scalars().all():
            coin_height2, _ = await _resolve_coin_chain_time(rpc, payment.txid, tx_chain_time_cache)
            if coin_height2 is None:
                continue
            new_confs = max(0, chain_tip - coin_height2 + 1)
            if new_confs != payment.confirmations:
                payment.confirmations = new_confs
                db.add(payment)
            req = payment.required_confirmations if payment.required_confirmations is not None else settings.REQUIRED_CONFIRMATIONS
            if new_confs >= req:
                await _confirm_payment(db, payment, now)

    state.coin_group_id = latest_group
    state.last_block_hash = last_result_hash
    state.updated_at = now
    db.add(state)
    await db.commit()


def _to_explorer_txid(tx_hash_b64: str) -> str:
    """getsparkanonymityset returns the tx hash base64-encoded and in
    internal (little-endian) byte order reverse + hex-encode to get the
    normal txid every block explorer and RPC call (getrawtransaction, etc.)
    actually expects."""
    try:
        return base64.b64decode(tx_hash_b64)[::-1].hex()
    except Exception:
        return tx_hash_b64


async def _resolve_coin_chain_time(
    rpc, explorer_txid: str, cache: dict[str, tuple[int | None, int | None]]
) -> tuple[int | None, int | None]:
    """(block_height, block_time) for a Spark coin's transaction, resolved via
    a follow-up chain-scoped RPC call — getsparkanonymityset only exposes a
    blockHash per batch/group, never per coin. Uses get_raw_transaction (not
    get_transaction) because it's chain-scoped rather than wallet-scoped: a
    Spark payment sent from a customer's own wallet won't appear in our
    node's wallet-relative transaction list. Cached per scan run so a coin
    matched against multiple merchant connections only costs one RPC round
    trip, not one per connection."""
    if explorer_txid in cache:
        return cache[explorer_txid]
    try:
        raw = await rpc.get_raw_transaction(explorer_txid)
        blockhash = raw.get("blockhash")
        if not blockhash:
            result = (None, None)
        else:
            header = await rpc.get_block_header(blockhash)
            result = (header.get("height"), header.get("time") or raw.get("blocktime") or raw.get("time"))
    except FiroRPCError:
        result = (None, None)
    cache[explorer_txid] = result
    return result


async def _apply_spark_payment(
    db: AsyncSession, payment: Payment, identified, tx_hash: str, now: datetime, credited: list,
    coin_height: int | None = None, chain_tip: int | None = None,
):
    """A single logical payment can arrive as several separate Spark coins to
    the same diversifier (the sending wallet splits the amount across coins),
    so every new coin adds to the running total instead of being judged on
    its own only a coin actually seen before is skipped."""
    from app.services.payment_monitor import _confirm_payment, _write_audit_log
    from app.models.models import SparkCoinCredit, User
    from app.core.payment_policy import resolve_tolerance_firo
    from sqlalchemy.exc import IntegrityError

    explorer_txid = _to_explorer_txid(tx_hash)

    # Global dedup — a coin_tag can only ever be credited to one Payment,
    # once, across all merchants and all scan runs.
    try:
        async with db.begin_nested():
            db.add(SparkCoinCredit(
                coin_tag=identified.coin_tag,
                txid=explorer_txid,
                payment_id=payment.id,
            ))
            await db.flush()
    except IntegrityError:
        logger.warning(
            f"[spark_scanner] coin_tag={identified.coin_tag} already globally credited "
            f"(race or historical replay) — skipping for payment {payment.id[:8]}"
        )
        return

    coin_firo = identified.value / 1e8
    expected  = payment.amount_firo

    res_m = await db.execute(select(User).where(User.id == payment.merchant_id))
    merchant = res_m.scalar_one_or_none()
    tolerance = resolve_tolerance_firo(merchant)

    credited.append(identified.coin_tag)
    payment.spark_coin_tags_json = json.dumps(credited)
    payment.spark_coin_tag = identified.coin_tag
    payment.amount_received = round((payment.amount_received or 0) + coin_firo, 8)
    payment.txid = explorer_txid

    # Real confirmation depth, not an instant force-set to the target.
    if coin_height is not None and chain_tip is not None:
        payment.confirmations = max(0, chain_tip - coin_height + 1)
    else:
        payment.confirmations = payment.confirmations or 0
    db.add(payment)

    amount_diff = round(payment.amount_received - expected, 8)

    if amount_diff < 0 and abs(amount_diff) > tolerance:
        # Underpaid beyond tolerance — accumulate, do not confirm, no webhook.
        await _write_audit_log(
            db, payment, PaymentAuditEvent.payment_detected,
            detail=f"partial spark coin_tag={identified.coin_tag} received_so_far={payment.amount_received:.8f} tolerance={tolerance:.8f}",
        )
        logger.info(
            f"Spark payment {payment.id[:8]} partial coin received | "
            f"total_so_far={payment.amount_received:.8f} expected={expected:.8f}"
        )
        await db.commit()
        asyncio.create_task(EventBus.publish_payment(str(payment.id), make_event(
            "payment.detected", payment_id=str(payment.id),
            amount_received=float(payment.amount_received or 0),
            confirmations=int(payment.confirmations or 0),
        )))
        return

    if amount_diff > tolerance:
        # Overpaid beyond tolerance — the coins are already irreversibly on
        # the merchant's address, so there's nothing to "reject." Accept and
        # proceed like an on-amount payment, just log it for visibility.
        logger.warning(
            f"Spark payment {payment.id[:8]} overpaid beyond tolerance: "
            f"received={payment.amount_received:.8f} expected={expected:.8f} tolerance={tolerance:.8f}"
        )

    await _write_audit_log(
        db, payment, PaymentAuditEvent.payment_detected,
        detail=f"spark coin_tag={identified.coin_tag} diversifier={identified.diversifier}",
    )
    logger.info(
        f"Spark payment {payment.id[:8]} detected | "
        f"diversifier={identified.diversifier} received={payment.amount_received:.8f}"
    )

    req = payment.required_confirmations if payment.required_confirmations is not None else settings.REQUIRED_CONFIRMATIONS
    if payment.confirmations >= req:
        await _confirm_payment(db, payment, now)
    else:
        payment.status = PaymentStatus.confirming
        db.add(payment)
        await db.commit()
