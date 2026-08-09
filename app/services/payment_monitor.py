from datetime import datetime, timezone
import asyncio
from loguru import logger
from app.services.event_bus import EventBus, make_event
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.core.config import get_settings
from app.models.models import Payment, PaymentStatus, User, PaymentAuditLog, PaymentAuditEvent
from app.services.firo_rpc import get_rpc, FiroRPCError
from app.services.webhook import fire_webhook

settings = get_settings()


async def check_pending_payments():
    async with AsyncSessionLocal() as db:
        try:
            await _run_payments(db)
        except Exception as e:
            logger.exception(f"Payment monitor error: {e}")


async def _run_payments(db: AsyncSession):
    """Persists "expired" status for any payment past its deadline. Coin
    detection itself is Spark-only now (payment_engine.py)."""
    now = datetime.now(timezone.utc)

    res = await db.execute(
        select(Payment).where(
            Payment.status.in_([PaymentStatus.pending, PaymentStatus.confirming]),
            Payment.expires_at.is_not(None),
            Payment.expires_at < now,
        )
    )
    payments: list[Payment] = res.scalars().all()
    if not payments:
        return

    logger.debug(f"Monitor: {len(payments)} expired payment(s) to persist")

    for p in payments:
        p.status = PaymentStatus.expired
        db.add(p)
        await _write_audit_log(db, p, PaymentAuditEvent.payment_expired)
        # Plan-purchase payments are excluded from merchant analytics the
        # FIRO goes to the operator's wallet, not the buyer's own
        # storefront, so it isn't the buyer's "failed sale".
        _is_plan = bool(p.order_id and p.order_id.startswith("plan:"))
        if not _is_plan and p.metadata_json:
            try:
                import json as _json_exp
                _is_plan = bool(_json_exp.loads(p.metadata_json).get("plan_purchase"))
            except Exception:
                pass
        if not _is_plan:
            from app.services.analytics_service import on_payment_failed
            await on_payment_failed(db, p)
        logger.info(f"Payment {p.id[:8]} → EXPIRED")
        await db.commit()
        asyncio.create_task(EventBus.publish_payment(str(p.id), make_event(
            "payment.expired", payment_id=str(p.id), status="expired"
        )))
        asyncio.create_task(EventBus.publish_merchant(str(p.merchant_id), make_event(
            "payment.expired", payment_id=str(p.id), status="expired"
        )))
        from app.services.webhook import fire_expired_webhook
        asyncio.create_task(fire_expired_webhook(p))


async def _write_audit_log(
    db: AsyncSession,
    payment: Payment,
    event: PaymentAuditEvent,
    detail: str | None = None,
):
    entry = PaymentAuditLog(
        payment_id    = str(payment.id),
        merchant_id   = str(payment.merchant_id),
        event         = event,
        amount_firo   = payment.amount_firo,
        amount_received = payment.amount_received,
        txid          = payment.txid,
        confirmations = payment.confirmations,
        detail        = detail,
    )
    db.add(entry)


def _meter_confirmed_payment(merchant: User, now: datetime) -> None:
    """Informational usage counter never blocks anything. A confirmed
    payment already settled on-chain to the merchant's own wallet; refusing
    to record/notify it would help no one. TIER_ENABLED only controls
    whether this number means anything to the dashboard/upgrade prompts."""
    if not get_settings().TIER_ENABLED:
        return

    rollover = merchant.rollover_requests or 0
    exp = merchant.rollover_expires_at
    if rollover > 0 and exp is not None:
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp < now:
            used  = merchant.requests_used  or 0
            total = merchant.requests_total or 0
            merchant.requests_total      = max(used, total - rollover)
            merchant.rollover_requests   = 0
            merchant.rollover_expires_at = None

    merchant.requests_used = (merchant.requests_used or 0) + 1


async def _confirm_payment(db: AsyncSession, p: Payment, now: datetime):

    if p.status == PaymentStatus.confirmed:
        logger.debug(f"Payment {p.id[:8]} already confirmed skipping")
        await db.commit()
        return

    received = p.amount_received or p.amount_firo

    p.status       = PaymentStatus.confirmed
    p.confirmed_at = now
    db.add(p)

    await _write_audit_log(db, p, PaymentAuditEvent.payment_confirmed,
                           f"confirmed txid={p.txid} confs={p.confirmations} received={received:.8f}")

    res = await db.execute(
        select(User).where(User.id == p.merchant_id).with_for_update()
    )
    merchant = res.scalar_one_or_none()

    if merchant:
        merchant.lifetime_gross_sales_firo   = round((merchant.lifetime_gross_sales_firo or 0) + (p.amount_firo or 0), 8)
        merchant.lifetime_received_firo      = round((merchant.lifetime_received_firo or 0) + received, 8)
        merchant.lifetime_confirmed_payments = (merchant.lifetime_confirmed_payments or 0) + 1
        merchant.lifetime_completed_orders   = (merchant.lifetime_completed_orders or 0) + 1
        _meter_confirmed_payment(merchant, now)
        db.add(merchant)

        await _write_audit_log(db, p, PaymentAuditEvent.merchant_stats_updated,
                               f"lifetime_gross_sales_firo={merchant.lifetime_gross_sales_firo:.8f} "
                               f"lifetime_confirmed_payments={merchant.lifetime_confirmed_payments}")

    logger.info(
        f"Payment {p.id[:8]} → CONFIRMED ✅ | received={received:.8f}"
    )

    await db.commit()

    asyncio.create_task(EventBus.publish_payment(str(p.id), make_event(
        "payment.confirmed",
        payment_id=str(p.id),
        status="confirmed",
        txid=p.txid,
        confirmations=int(p.confirmations or 0),
        amount_firo=float(p.amount_firo or 0),
        amount_received=float(received),
        confirmed_at=p.confirmed_at.isoformat() if p.confirmed_at else None,
    )))
    asyncio.create_task(EventBus.publish_merchant(str(p.merchant_id), make_event(
        "payment.confirmed",
        payment_id=str(p.id),
        status="confirmed",
        amount_firo=float(p.amount_firo or 0),
        amount_received=float(received),
        order_id=p.order_id,
    )))

    await fire_webhook(db, p)

    try:
        if p.customer_email and merchant:
            from app.services.mailer import send_payment_receipt_email
            from app.core.config import get_settings as _gs
            _settings = _gs()
            _is_test = _settings.is_testnet
            _explorer = (
                f"https://testexplorer.firo.org/tx/{p.txid}" if (p.txid and _is_test)
                else f"https://explorer.firo.org/tx/{p.txid}" if p.txid
                else None
            )
            import asyncio as _asyncio
            _asyncio.create_task(send_payment_receipt_email(
                p.customer_email,
                (merchant.app_name or "").strip() or "Store",
                order_id          = p.order_id,
                order_description = p.order_description,
                amount_received   = p.amount_received,
                amount_firo       = p.amount_firo,
                txid              = p.txid,
                confirmed_at_iso  = p.confirmed_at.isoformat() if p.confirmed_at else None,
                is_testnet        = _is_test,
                explorer_url      = _explorer,
            ))
    except Exception as e:
        logger.warning(f"Failed to schedule receipt email for {p.id[:8]}: {e}")

    try:
        if merchant and getattr(merchant, "notify_on_payment", True):
            from app.core.config import get_settings as _gs2
            import asyncio as _asyncio2
            _s2 = _gs2()
            _tg_chat = getattr(merchant, "telegram_chat_id", None)
            _tg_on   = bool(getattr(merchant, "notify_telegram", False))
            if _tg_chat and _tg_on and _s2.telegram_bot_enabled:
                from app.services.telegram_bot import send_payment_notification as _tg_notify
                _asyncio2.create_task(_tg_notify(
                    _tg_chat,
                    merchant_name     = (merchant.app_name or "").strip() or "Store",
                    amount_firo       = p.amount_received or p.amount_firo,
                    order_id          = p.order_id,
                    order_description = p.order_description,
                    customer_email    = p.customer_email,
                    txid              = p.txid,
                ))
    except Exception as e:
        logger.warning(f"Failed to schedule merchant notification for {p.id[:8]}: {e}")

    from app.services.analytics_service import on_payment_confirmed
    await on_payment_confirmed(db, p)
    # on_payment_confirmed only flushes commit here or the DailyStats/
    # UserDailyStats rows are silently discarded when this session closes,
    # which is why analytics previously showed 0 / stale numbers.
    await db.commit()


async def verify_manual_txhash(payment_id: str, txhash: str) -> dict:
    async with AsyncSessionLocal() as db:

        res = await db.execute(
            select(Payment).where(Payment.id == payment_id).with_for_update()
        )
        p = res.scalar_one_or_none()
        if not p:
            return {"result": "not_found", "message": "Payment not found"}

        if p.status == PaymentStatus.confirmed:
            return {"result": "already_confirmed", "message": "Already confirmed"}
        if p.status == PaymentStatus.cancelled:
            return {"result": "cancelled", "message": "This payment was cancelled and can no longer be confirmed."}
        if p.status == PaymentStatus.failed:
            return {"result": "failed", "message": "This payment failed and can no longer be confirmed."}
        if p.status == PaymentStatus.expired:
            return {"result": "expired", "message": "Payment expired"}

        now    = datetime.now(timezone.utc)
        expires = p.expires_at
        if expires:
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires < now:
                p.status = PaymentStatus.expired
                db.add(p)
                await db.commit()
                return {"result": "expired", "message": "Payment expired"}


        # A txid already fully processed against this exact payment (same
        # hash re-submitted on a repeated manual check) — short-circuit
        # before re-verifying/re-accumulating it a second time.
        if p.txid == txhash and p.manual_check_result == "matched":
            return {"result": "already_used", "message": "This transaction was already applied to this payment"}

        res2 = await db.execute(
            select(Payment).where(
                Payment.txid == txhash,
                Payment.id   != payment_id,
                Payment.status.in_([PaymentStatus.confirming, PaymentStatus.confirmed]),
            ).limit(1)
        )
        if res2.scalars().first():
            return {
                "result":  "already_used",
                "message": "This transaction is already assigned to another payment",
            }

        res_m = await db.execute(select(User).where(User.id == p.merchant_id))
        merchant = res_m.scalar_one_or_none()
        from app.core.payment_policy import resolve_tolerance_firo
        tolerance = resolve_tolerance_firo(merchant)

        remaining = max(0.0, p.amount_firo - (p.amount_received or 0))

        rpc    = get_rpc()
        valid, confs, received = await rpc.verify_utxo(
            txid=txhash, vout=0,
            address=p.receiving_address,
            amount=remaining,
            tolerance=tolerance,
        )

        if not valid:
            p.manual_check_result = "mismatch"
            db.add(p)
            await db.commit()
            return {
                "result":  "mismatch",
                "message": (
                    f"TX does not match. "
                    f"Expected {remaining:.8f} FIRO to {p.receiving_address[:20]}…"
                ),
            }


        # A different txid is only blocked once this invoice is already fully
        # paid — while still partial, additional distinct txids are exactly
        # how a customer tops up a remaining balance (mirrors Spark, which
        # accumulates multiple coins per payment via spark_coin_tags_json;
        # the transparent path has no such list column, so p.txid simply
        # tracks the most-recently-applied hash once we're fully paid).
        already_complete = (p.amount_received or 0) >= p.amount_firo - tolerance
        if p.txid is not None and p.txid != txhash and already_complete:
            return {"result": "already_used", "message": "Payment already has a different TX assigned"}

        # Reject a transaction that predates this invoice — mirrors the Spark
        # scanner's historical-rejection check (payment_engine.py) so a stale
        # transaction to a reused address can't confirm a brand-new invoice.
        try:
            tx_detail = await rpc.get_transaction(txhash)
            blockhash = tx_detail.get("blockhash")
            coin_time = tx_detail.get("blocktime") or tx_detail.get("time")
            coin_height = None
            if blockhash:
                header = await rpc.get_block_header(blockhash)
                coin_height = header.get("height")
        except FiroRPCError:
            coin_height, coin_time = None, None

        p_created = p.created_at
        if p_created and p_created.tzinfo is None:
            p_created = p_created.replace(tzinfo=timezone.utc)

        if p.start_block_height is not None and coin_height is not None and coin_height < p.start_block_height:
            return {
                "result":  "mismatch",
                "message": "This transaction predates the invoice and cannot be used to confirm it.",
            }
        if coin_time is not None and p_created is not None:
            coin_dt = datetime.fromtimestamp(coin_time, tz=timezone.utc)
            if coin_dt < p_created:
                return {
                    "result":  "mismatch",
                    "message": "This transaction predates the invoice and cannot be used to confirm it.",
                }

        p.txid                = txhash
        p.amount_received     = round((p.amount_received or 0) + received, 8)
        p.confirmations       = confs
        p.manual_check_result = "matched"
        db.add(p)

        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            return {
                "result":  "already_used",
                "message": "This UTXO is already used by another payment",
            }

        amount_diff = round(p.amount_received - p.amount_firo, 8)

        # Underpaid beyond tolerance — accumulate, do not confirm, no webhook.
        if amount_diff < 0 and abs(amount_diff) > tolerance:
            p.status = PaymentStatus.pending
            db.add(p)
            await db.commit()
            return {
                "result":    "partial",
                "confirmed": False,
                "message":   f"Partial payment received ({p.amount_received:.8f}/{p.amount_firo:.8f} FIRO). Send the remaining amount to complete this invoice.",
            }

        if amount_diff > tolerance:
            # Overpaid beyond tolerance — already on-chain, nothing to
            # reject; accept and proceed, same as the Spark path.
            logger.warning(
                f"Payment {p.id[:8]} overpaid beyond tolerance: "
                f"received={p.amount_received:.8f} expected={p.amount_firo:.8f} tolerance={tolerance:.8f}"
            )

        req = p.required_confirmations if p.required_confirmations is not None else settings.REQUIRED_CONFIRMATIONS
        if confs >= req:
            await _confirm_payment(db, p, now)
            return {
                "result":     "matched",
                "confirmed":  True,
                "message":    f"Confirmed! {confs} confirmations",
                "success_url": p.success_url,
            }
        else:
            p.status = PaymentStatus.confirming
            db.add(p)
            await db.commit()
            return {
                "result":    "matched",
                "confirmed": False,
                "message":   f"TX found, waiting for confirmations ({confs}/{req})",
            }
