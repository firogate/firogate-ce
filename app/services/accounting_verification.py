"""
Accounting Verification Service

Periodically compares in-DB merchant lifetime statistics against the authoritative
source of truth: confirmed Payment records. Any drift is logged as a MerchantStatsCheck
row and written to the PaymentAuditLog so it appears in the immutable audit trail.

This service never modifies merchant stats automatically it only detects and reports
mismatches. Corrections must be applied manually or via a dedicated repair command.
"""
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.models.models import (
    User, UserRole, Payment, PaymentStatus,
    MerchantStatsCheck, PaymentAuditLog, PaymentAuditEvent
)


_MISMATCH_TOLERANCE = 1e-6


async def verify_merchant_stats(db: AsyncSession, merchant_id: str) -> MerchantStatsCheck:
    """
    Compare one merchant's lifetime stats against their confirmed Payment records.
    Writes a MerchantStatsCheck row and, on mismatch, a PaymentAuditLog entry.
    Returns the check record.
    """
    user_res = await db.execute(select(User).where(User.id == merchant_id))
    user = user_res.scalar_one_or_none()
    if not user:
        raise ValueError(f"Merchant {merchant_id} not found")

    agg = await db.execute(
        select(
            func.count(Payment.id),
            func.sum(Payment.amount_firo),
            func.sum(Payment.amount_received),
        ).where(
            Payment.merchant_id == merchant_id,
            Payment.status == PaymentStatus.confirmed,
        )
    )
    row = agg.one()
    actual_count         = int(row[0] or 0)
    actual_gross_sales   = round(float(row[1] or 0), 8)
    actual_received      = round(float(row[2] or 0), 8)

    db_gross_sales  = round(float(user.lifetime_gross_sales_firo   or 0), 8)
    db_received     = round(float(user.lifetime_received_firo      or 0), 8)
    db_confirmed    = int(user.lifetime_confirmed_payments          or 0)
    db_orders       = int(user.lifetime_completed_orders            or 0)

    gross_mismatch    = abs(db_gross_sales - actual_gross_sales) > _MISMATCH_TOLERANCE
    received_mismatch = abs(db_received - actual_received) > _MISMATCH_TOLERANCE
    count_mismatch    = db_confirmed != actual_count
    has_mismatch      = gross_mismatch or received_mismatch or count_mismatch

    detail_parts = []
    if gross_mismatch:
        detail_parts.append(
            f"gross_sales: db={db_gross_sales} actual={actual_gross_sales} "
            f"delta={db_gross_sales - actual_gross_sales:+.8f}"
        )
    if received_mismatch:
        detail_parts.append(
            f"received: db={db_received} actual={actual_received} "
            f"delta={db_received - actual_received:+.8f}"
        )
    if count_mismatch:
        detail_parts.append(
            f"confirmed_payments: db={db_confirmed} actual={actual_count}"
        )

    mismatch_detail = "; ".join(detail_parts) if detail_parts else None

    check = MerchantStatsCheck(
        merchant_id                = merchant_id,
        db_gross_sales             = db_gross_sales,
        db_received                = db_received,
        db_confirmed_payments      = db_confirmed,
        db_completed_orders        = db_orders,
        actual_gross_sales         = actual_gross_sales,
        actual_received            = actual_received,
        actual_confirmed_payments  = actual_count,
        actual_completed_orders    = actual_count,
        has_mismatch               = has_mismatch,
        mismatch_detail            = mismatch_detail,
    )
    db.add(check)

    if has_mismatch:
        logger.warning(
            f"[accounting] MISMATCH merchant={merchant_id[:8]} | {mismatch_detail}"
        )
        audit_entry = PaymentAuditLog(
            payment_id    = "000000-accounting-check",
            merchant_id   = merchant_id,
            event         = PaymentAuditEvent.stats_mismatch_detected,
            detail        = mismatch_detail,
        )
        db.add(audit_entry)
    else:
        logger.debug(f"[accounting] Stats verified OK for merchant={merchant_id[:8]}")
        audit_entry = PaymentAuditLog(
            payment_id    = "000000-accounting-check",
            merchant_id   = merchant_id,
            event         = PaymentAuditEvent.stats_verified,
            detail        = f"gross_sales={actual_gross_sales} received={actual_received} count={actual_count}",
        )
        db.add(audit_entry)

    await db.commit()
    return check


async def verify_all_merchants(db: AsyncSession) -> dict:
    """
    Run accounting verification for every active merchant.
    Returns a summary dict with counts of checked, mismatched, and ok merchants.
    """
    res = await db.execute(
        select(User.id).where(
            User.role == UserRole.merchant,
            User.is_active == True,
        )
    )
    merchant_ids = [row[0] for row in res.all()]

    checked    = 0
    mismatched = 0

    for mid in merchant_ids:
        try:
            check = await verify_merchant_stats(db, mid)
            checked += 1
            if check.has_mismatch:
                mismatched += 1
        except Exception as e:
            logger.error(f"[accounting] Failed to verify merchant {mid[:8]}: {e}")

    logger.info(
        f"[accounting] Verification complete: {checked} merchants checked, "
        f"{mismatched} mismatches detected"
    )
    return {
        "checked":    checked,
        "mismatched": mismatched,
        "ok":         checked - mismatched,
    }


async def repair_merchant_stats(db: AsyncSession, merchant_id: str) -> dict:
    """
    Recompute merchant lifetime stats from confirmed Payment records and overwrite
    the stored values. Only call this after human review of a mismatch.
    Writes an audit log entry for the correction.
    """
    user_res = await db.execute(select(User).where(User.id == merchant_id))
    user = user_res.scalar_one_or_none()
    if not user:
        raise ValueError(f"Merchant {merchant_id} not found")

    agg = await db.execute(
        select(
            func.count(Payment.id),
            func.sum(Payment.amount_firo),
            func.sum(Payment.amount_received),
        ).where(
            Payment.merchant_id == merchant_id,
            Payment.status == PaymentStatus.confirmed,
        )
    )
    row = agg.one()
    actual_count       = int(row[0] or 0)
    actual_gross_sales = round(float(row[1] or 0), 8)
    actual_received    = round(float(row[2] or 0), 8)

    old_values = {
        "lifetime_gross_sales_firo":   user.lifetime_gross_sales_firo,
        "lifetime_received_firo":      user.lifetime_received_firo,
        "lifetime_confirmed_payments": user.lifetime_confirmed_payments,
        "lifetime_completed_orders":   user.lifetime_completed_orders,
    }

    user.lifetime_gross_sales_firo   = actual_gross_sales
    user.lifetime_received_firo      = actual_received
    user.lifetime_confirmed_payments = actual_count
    user.lifetime_completed_orders   = actual_count
    db.add(user)

    audit_entry = PaymentAuditLog(
        payment_id  = "000000-accounting-repair",
        merchant_id = merchant_id,
        event       = PaymentAuditEvent.merchant_stats_updated,
        detail      = (
            f"REPAIR from accounting verification. "
            f"old={old_values} "
            f"new=gross_sales={actual_gross_sales} received={actual_received} count={actual_count}"
        ),
    )
    db.add(audit_entry)
    await db.commit()

    logger.info(f"[accounting] Stats repaired for merchant={merchant_id[:8]}")
    return {
        "merchant_id":                 merchant_id,
        "lifetime_gross_sales_firo":   actual_gross_sales,
        "lifetime_received_firo":      actual_received,
        "lifetime_confirmed_payments": actual_count,
        "lifetime_completed_orders":   actual_count,
    }


async def run_scheduled_verification():
    """Entry point for the periodic scheduler (called from main.py scheduler loop)."""
    from app.core.database import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            result = await verify_all_merchants(db)
            if result["mismatched"]:
                logger.warning(
                    f"[accounting] {result['mismatched']} merchant(s) have stats mismatches "
                    f"review merchant_stats_checks table for details"
                )
    except Exception as e:
        logger.error(f"[accounting] Scheduled verification failed: {e}")
