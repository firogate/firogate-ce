from datetime import datetime, timezone
import asyncio
from loguru import logger
from app.services.event_bus import EventBus, make_event
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.core.config import get_settings
from app.models.models import Payment, PaymentStatus, User, PlanOrder
from app.services.firo_rpc import get_rpc, FiroRPCError
from app.services.webhook import fire_webhook

settings = get_settings()


async def _get_locked_utxos(db: AsyncSession) -> set[tuple]:

    res = await db.execute(
        select(Payment.txid, Payment.vout).where(
            Payment.txid.is_not(None),
            Payment.status.in_([PaymentStatus.confirming, PaymentStatus.confirmed]),
        )
    )
    locked = {(r[0], r[1]) for r in res.fetchall() if r[0] is not None}


    res2 = await db.execute(
        select(PlanOrder.txid).where(
            PlanOrder.txid.is_not(None),
            PlanOrder.status.in_([PaymentStatus.confirming, PaymentStatus.confirmed]),
        )
    )
    for row in res2.fetchall():
        if row[0]:
            locked.add((row[0], 0))

    return locked


async def _assign_utxo_safe(
    db: AsyncSession,
    payment: Payment,
    txid: str,
    vout: int,
    received: float,
    confs: int,
    locked_utxos: set[tuple],
) -> bool:
    utxo_key = (txid, vout)


    if utxo_key in locked_utxos:
        logger.warning(
            f"[double-spend] UTXO {txid[:16]}:{vout} already locked in memory"
        )
        return False


    res = await db.execute(
        select(Payment).where(Payment.id == payment.id).with_for_update()
    )
    p_locked = res.scalar_one_or_none()
    if not p_locked:
        return False


    if p_locked.txid is not None:
        logger.warning(
            f"[double-spend] Payment {payment.id[:8]} already has txid={p_locked.txid[:16]}"
        )
        return False

    p_locked.txid            = txid
    p_locked.vout            = vout
    p_locked.amount_received = received
    p_locked.confirmations   = confs
    p_locked.status          = PaymentStatus.confirming


    payment.txid            = txid
    payment.vout            = vout
    payment.amount_received = received
    payment.confirmations   = confs
    payment.status          = PaymentStatus.confirming


    db.add(p_locked)
    try:
        await db.flush()
        locked_utxos.add(utxo_key)
        logger.info(
            f"Payment {payment.id[:8]} → CONFIRMING | "
            f"utxo={txid[:16]}…:{vout} amt={received:.8f} confs={confs}"
        )
        # Notify SSE — checkout page gets instant update
        asyncio.create_task(EventBus.publish_payment(str(payment.id), make_event(
            "payment.confirming",
            payment_id=str(payment.id),
            status="confirming",
            confirmations=int(confs),
            txid=txid,
        )))
        asyncio.create_task(EventBus.publish_merchant(str(payment.merchant_id), make_event(
            "payment.confirming", payment_id=str(payment.id), status="confirming"
        )))
        return True
    except IntegrityError:
        await db.rollback()
        logger.warning(
            f"[double-spend] BLOCKED: txid={txid[:16]}:{vout} "
            f"already used by another payment (IntegrityError)"
        )
        return False


def _get_shield_engine():
    try:
        from app.enterprise.services.shield_engine import notify_payment_confirmed
        return notify_payment_confirmed
    except ImportError:
        async def _noop(): pass
        return _noop
    return notify_payment_confirmed


async def check_pending_payments():
    async with AsyncSessionLocal() as db:
        try:
            await _run_payments(db)
        except Exception as e:
            logger.exception(f"Payment monitor error: {e}")


async def _run_payments(db: AsyncSession):
    now = datetime.now(timezone.utc)
    rpc = get_rpc()

    res = await db.execute(
        select(Payment).where(
            Payment.status.in_([PaymentStatus.pending, PaymentStatus.confirming])
        )
    )
    payments: list[Payment] = res.scalars().all()
    if not payments:
        return

    logger.debug(f"Monitor: {len(payments)} pending payment(s)")


    locked_utxos = await _get_locked_utxos(db)

    for p in payments:
        expires = p.expires_at
        if expires and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)


        if expires and expires < now:
            if p.status != PaymentStatus.expired:
                p.status = PaymentStatus.expired
                db.add(p)
                logger.info(f"Payment {p.id[:8]} → EXPIRED")
                await db.commit()
                # Notify checkout page via SSE
                asyncio.create_task(EventBus.publish_payment(str(p.id), make_event(
                    "payment.expired", payment_id=str(p.id), status="expired"
                )))
                asyncio.create_task(EventBus.publish_merchant(str(p.merchant_id), make_event(
                    "payment.expired", payment_id=str(p.id), status="expired"
                )))
            continue

        address = p.receiving_address
        if not address:
            continue


        if p.txid is not None:
            try:
                confs = await rpc.get_confirmations(p.txid)
                p.confirmations = confs
                db.add(p)

                req = p.required_confirmations or settings.REQUIRED_CONFIRMATIONS
                if confs >= req and p.status != PaymentStatus.confirmed:
                    await _confirm_payment(db, p, now)

                    _task = asyncio.create_task(_get_shield_engine()())
                    _task.add_done_callback(
                        lambda t: logger.warning(f"shield_engine task failed: {t.exception()}")
                        if not t.cancelled() and t.exception() else None
                    )
                else:
                    await db.commit()
            except FiroRPCError as e:
                logger.warning(f"Confirmations error {p.id[:8]}: {e}")
                await db.commit()
            continue


        try:
            txid, vout, received, confs = await rpc.find_utxo_for_address(
                address=address,
                amount=p.amount_firo,
                locked_utxos=locked_utxos,
            )
        except FiroRPCError as e:
            logger.warning(f"RPC error {p.id[:8]}: {e}")
            continue

        if not txid:
            continue


        expected  = p.amount_firo
        # Use 2% tolerance to handle floating point imprecision and minor fee variations
        tolerance = max(0.0001, expected * 0.02)

        if received < expected - tolerance:
            # Genuine partial payment
            p.status          = PaymentStatus.failed
            p.amount_received = received
            db.add(p)
            logger.warning(
                f"Payment {p.id[:8]} REJECTED: partial payment "
                f"(expected={expected:.8f} got={received:.8f})"
            )
            await db.commit()
            continue
        elif received > expected + tolerance:
            logger.warning(
                f"Payment {p.id[:8]} overpayment "
                f"(expected={expected:.8f} got={received:.8f}) — accepting"
            )


        assigned = await _assign_utxo_safe(db, p, txid, vout, received, confs, locked_utxos)
        if not assigned:
            continue


        req = p.required_confirmations or settings.REQUIRED_CONFIRMATIONS
        if confs >= req:
            await _confirm_payment(db, p, now)

            asyncio.create_task(_get_shield_engine()())
        else:
            await db.commit()


async def _confirm_payment(db: AsyncSession, p: Payment, now: datetime):

    if p.status == PaymentStatus.confirmed:
        logger.debug(f"Payment {p.id[:8]} already confirmed — skipping")
        await db.commit()
        return

    # Check if this is a plan purchase payment
    is_plan_purchase = False
    plan_meta = None
    if p.metadata_json:
        try:
            import json
            plan_meta = json.loads(p.metadata_json)
            is_plan_purchase = plan_meta.get("plan_purchase", False)
        except:
            pass

    # Also check order_id prefix
    if not is_plan_purchase and p.order_id and p.order_id.startswith("plan:"):
        is_plan_purchase = True
        plan_name = p.order_id.replace("plan:", "")
        plan_meta = {"plan": plan_name, "plan_purchase": True}

    from app.core.fees import calc_net as _cn
    received = p.amount_received or p.amount_firo

    # For plan purchases: the full received amount is platform revenue.
    # No merchant balance credit, but the FIRO sits in the node wallet and
    # must be counted in get_platform_revenue_balance so the admin can withdraw it.
    if is_plan_purchase:
        platform_fee = received   # entire payment is platform revenue
        merchant_net = 0.0
    else:
        platform_fee, merchant_net = _cn(received)
        if platform_fee >= received:
            platform_fee = 0.0
            merchant_net = received

    p.status                 = PaymentStatus.confirmed
    p.confirmed_at           = now
    p.credited_to_balance_at = now
    p.platform_fee_firo      = platform_fee
    p.platform_fee_pct       = round((platform_fee / received * 100), 4) if received > 0 else 0.0
    p.merchant_net_firo      = merchant_net
    db.add(p)


    res = await db.execute(
        select(User).where(User.id == p.merchant_id).with_for_update()
    )
    merchant = res.scalar_one_or_none()
    
    if is_plan_purchase and merchant and plan_meta:
        # Activate the plan for the merchant
        await _activate_plan_from_payment(db, p, merchant, plan_meta, now)
        
        logger.info(
            f"Plan Payment {p.id[:8]} → CONFIRMED ✅ (plan purchase) | "
            f"received={received:.8f} plan={plan_meta.get('plan')}"
        )
        
        await db.commit()
        
        # Fire plan.activated webhook instead of payment.confirmed
        from app.services.webhook import fire_plan_activated_webhook
        await fire_plan_activated_webhook(db, p, merchant, now)
        return

    # Regular payment: credit to merchant balance
    if merchant:
        # Community Edition — no request counting
        merchant.balance_firo      = round((merchant.balance_firo or 0) + merchant_net, 8)
        merchant.balance_pending   = max(0, round((merchant.balance_pending or 0) - p.amount_firo, 8))
        merchant.total_earned_firo = round((merchant.total_earned_firo or 0) + received, 8)
        merchant.total_fees_firo   = round((merchant.total_fees_firo or 0) + platform_fee, 8)
        db.add(merchant)

    logger.info(
        f"Payment {p.id[:8]} → CONFIRMED ✅ | "
        f"received={received:.8f} fee={platform_fee:.8f} net={merchant_net:.8f}"
    )

    await db.commit()

    # Notify SSE subscribers — instant checkout confirmation
    asyncio.create_task(EventBus.publish_payment(str(p.id), make_event(
        "payment.confirmed",
        payment_id=str(p.id),
        status="confirmed",
        txid=p.txid,
        confirmations=int(p.confirmations or 0),
        amount_firo=float(p.amount_firo or 0),
        confirmed_at=p.confirmed_at.isoformat() if p.confirmed_at else None,
    )))
    asyncio.create_task(EventBus.publish_merchant(str(p.merchant_id), make_event(
        "payment.confirmed",
        payment_id=str(p.id),
        status="confirmed",
        amount_firo=float(p.amount_firo or 0),
        merchant_net=float(merchant_net),
        order_id=p.order_id,
    )))

    await fire_webhook(db, p)

    # Send merchant-branded receipt email to the customer (fire-and-forget)
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

    # ─ Instant merchant notification email ─
    try:
        if merchant and getattr(merchant, "notify_on_payment", True):
            from app.services.mailer import send_merchant_payment_notification
            from app.core.config import get_settings as _gs2
            import asyncio as _asyncio2
            _s2 = _gs2()
            _notify_to = (
                getattr(merchant, "notify_email", None)
                or merchant.email
            )
            if _notify_to:
                _dash_url = (
                    _s2.DASHBOARD_URL.rstrip("/")
                    if getattr(_s2, "DASHBOARD_URL", "")
                    else _s2.BASE_URL.rstrip("/") + "/dashboard"
                )
                _asyncio2.create_task(send_merchant_payment_notification(
                    _notify_to,
                    merchant_name     = (merchant.app_name or "").strip() or "Store",
                    amount_firo       = p.amount_received or p.amount_firo,
                    net_firo          = p.merchant_net_firo or 0.0,
                    fee_firo          = p.platform_fee_firo or 0.0,
                    order_id          = p.order_id,
                    order_description = p.order_description,
                    customer_email    = p.customer_email,
                    payment_id        = str(p.id),
                    txid              = p.txid,
                    dashboard_url     = _dash_url,
                ))
    except Exception as e:
        logger.warning(f"Failed to schedule merchant notification for {p.id[:8]}: {e}")

    # Update analytics stats
    from app.services.analytics_service import on_payment_confirmed
    await on_payment_confirmed(db, p)


async def _activate_plan_from_payment(
    db: AsyncSession, 
    payment: Payment, 
    merchant: User, 
    plan_meta: dict, 
    now: datetime
):
    """Activate plan from a confirmed plan payment."""
    from datetime import timedelta
    
    plan_name = plan_meta.get("plan", "")
    requests_quota = plan_meta.get("requests_quota")
    duration_days = plan_meta.get("duration_days")
    
    # If quota/duration not in metadata, look up from PlanConfig
    if requests_quota is None or duration_days is None:
        from app.models.models import PlanConfig
        res = await db.execute(
            select(PlanConfig).where(PlanConfig.plan == plan_name)
        )
        config = res.scalar_one_or_none()
        if config:
            requests_quota = config.requests_quota
            duration_days = config.duration_days
        else:
            # Fallback defaults
            requests_quota = requests_quota or 1000
            duration_days = duration_days or 30

    # Update merchant plan
    merchant.plan            = plan_name
    merchant.requests_total  = (merchant.requests_total or 0) + requests_quota
    merchant.plan_expires_at = now + timedelta(days=duration_days)
    db.add(merchant)

    # Also update linked PlanOrder if exists
    plan_order_id = plan_meta.get("plan_order_id")
    if plan_order_id:
        res_order = await db.execute(
            select(PlanOrder).where(PlanOrder.id == plan_order_id)
        )
        order = res_order.scalar_one_or_none()
        if order:
            order.status = PaymentStatus.confirmed
            order.activated_at = now
            order.txid = payment.txid
            order.confirmations = payment.confirmations
            db.add(order)

    logger.info(
        f"Plan {plan_name} activated for merchant {merchant.id[:8]} | "
        f"+{requests_quota} requests, expires in {duration_days} days"
    )


async def check_plan_orders():
    async with AsyncSessionLocal() as db:
        try:
            await _run_plan_orders(db)
        except Exception as e:
            logger.exception(f"Plan monitor error: {e}")


async def _run_plan_orders(db: AsyncSession):
    now = datetime.now(timezone.utc)
    rpc = get_rpc()

    res = await db.execute(
        select(PlanOrder).where(
            PlanOrder.status.in_([PaymentStatus.pending, PaymentStatus.confirming])
        )
    )
    orders: list[PlanOrder] = res.scalars().all()
    if not orders:
        return


    locked = await _get_locked_utxos(db)

    for order in orders:
        expires = order.expires_at
        if expires and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)

        if expires and expires < now:
            if order.status not in (PaymentStatus.confirmed, PaymentStatus.expired):
                order.status = PaymentStatus.expired
                db.add(order)
                await db.commit()
            continue

        if not order.receiving_address:
            continue


        if order.txid:
            try:
                confs = await rpc.get_confirmations(order.txid)
                order.confirmations = confs
                db.add(order)
                if confs >= 2 and order.status != PaymentStatus.confirmed:
                    await _activate_plan(db, order, now)
                else:
                    await db.commit()
            except Exception:
                await db.commit()
            continue


        try:
            txid, vout, received, confs = await rpc.find_utxo_for_address(
                address=order.receiving_address,
                amount=order.price_firo,
                locked_utxos=locked,
            )
        except FiroRPCError:
            continue

        if not txid:
            continue

        utxo_key = (txid, vout if vout is not None else 0)
        if utxo_key in locked:
            logger.warning(
                f"[double-spend] Plan order {order.id[:8]}: "
                f"UTXO {txid[:16]}:{vout} already locked"
            )
            continue


        res2 = await db.execute(
            select(PlanOrder).where(PlanOrder.id == order.id).with_for_update()
        )
        order_locked = res2.scalar_one_or_none()
        if not order_locked or order_locked.txid is not None:
            continue

        order_locked.txid          = txid
        order_locked.confirmations = confs
        order_locked.status        = PaymentStatus.confirming
        db.add(order_locked)
        locked.add(utxo_key)

        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            logger.warning(f"[double-spend] Plan order {order.id[:8]}: IntegrityError on UTXO assign")
            continue

        if confs >= 2:
            await _activate_plan(db, order_locked, now)
        else:
            await db.commit()


async def _activate_plan(db: AsyncSession, order: PlanOrder, now: datetime):
    if order.status == PaymentStatus.confirmed:
        await db.commit()
        return

    from app.models.models import PlanConfig
    from datetime import timedelta

    res = await db.execute(
        select(PlanConfig).where(PlanConfig.plan == order.plan)
    )
    config = res.scalar_one_or_none()
    if not config:
        return


    res2 = await db.execute(
        select(User).where(User.id == order.merchant_id).with_for_update()
    )
    merchant = res2.scalar_one_or_none()
    if not merchant:
        return

    merchant.plan            = order.plan
    merchant.requests_total  = (merchant.requests_total or 0) + config.requests_quota
    merchant.plan_expires_at = now + timedelta(days=config.duration_days)
    order.status             = PaymentStatus.confirmed
    order.activated_at       = now
    db.add(merchant)
    db.add(order)

    logger.info(f"Plan {order.plan} activated for merchant {order.merchant_id[:8]}")
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

        rpc    = get_rpc()
        valid, confs, received = await rpc.verify_utxo(
            txid=txhash, vout=0,
            address=p.receiving_address,
            amount=p.amount_firo,
        )

        if not valid:
            p.manual_check_result = "mismatch"
            db.add(p)
            await db.commit()
            return {
                "result":  "mismatch",
                "message": (
                    f"TX does not match. "
                    f"Expected {p.amount_firo:.8f} FIRO to {p.receiving_address[:20]}…"
                ),
            }


        if p.txid is not None and p.txid != txhash:
            return {"result": "already_used", "message": "Payment already has a different TX assigned"}

        p.txid                = txhash
        p.amount_received     = received
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

        req = p.required_confirmations or settings.REQUIRED_CONFIRMATIONS
        if confs >= req:
            await _confirm_payment(db, p, now)
            _task2 = asyncio.create_task(_get_shield_engine()())
            _task2.add_done_callback(
                lambda t: logger.warning(f"shield_engine task failed: {t.exception()}")
                if not t.cancelled() and t.exception() else None
            )
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


# ─ Plan Price Updater ──
async def update_plan_prices() -> None:
    """
    Runs every 2 hours via scheduler.
    Fetches FIRO/USD from CMC and updates price_firo for all paid plans.
    price_usd stays unchanged (set manually by admin).
    Free plan always stays 0 FIRO.
    """
    try:
        from app.services.price_service import get_firo_price
        from app.models.models import PlanConfig
        from app.core.database import AsyncSessionLocal
        from sqlalchemy import select

        firo_usd = await get_firo_price(fresh=True)
        if not firo_usd or firo_usd <= 0:
            logger.warning("[plan_prices] Could not fetch FIRO price — skipping update")
            return

        async with AsyncSessionLocal() as db:
            res = await db.execute(select(PlanConfig).where(PlanConfig.is_active == True))
            plans = res.scalars().all()

            updated = 0
            for plan in plans:
                # Free plan always stays 0
                if str(plan.plan).lower() == "free" or plan.price_usd == 0:
                    continue

                # Calculate FIRO equivalent from USD price
                new_price_firo = round(plan.price_usd / firo_usd, 4)

                # Only update if changed by more than 1% to avoid micro-updates
                old = plan.price_firo or 0
                if old == 0 or abs(new_price_firo - old) / old > 0.01:
                    plan.price_firo = new_price_firo
                    db.add(plan)
                    updated += 1
                    logger.info(
                        f"[plan_prices] {plan.plan}: "
                        f"${plan.price_usd} USD → {new_price_firo} FIRO "
                        f"(FIRO/USD={firo_usd:.4f})"
                    )

            if updated:
                await db.commit()
                logger.success(f"[plan_prices] Updated {updated} plan(s)")
            else:
                logger.debug("[plan_prices] No significant change — skipped")

    except Exception as e:
        logger.error(f"[plan_prices] Update failed: {e}")
