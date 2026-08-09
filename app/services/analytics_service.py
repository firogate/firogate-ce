from datetime import datetime, timezone, timedelta
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.models.models import DailyStats, UserDailyStats, User, Payment, PaymentStatus


def _pct_change(current: float, previous: float) -> float:
    """Percent change vs. the prior period of equal length. None (not 0)
    when there's no prior-period baseline, so the UI can show '—' instead
    of a misleading '+100%'/'0%'."""
    if not previous:
        return None
    return round((current - previous) / previous * 100, 1)


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _date_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _build_daily_series(daily_data, start_date: str, end_date: str) -> dict:
    """Zero-fill a contiguous per-day series between start/end from whatever
    UserDailyStats rows actually exist shared by the period-scoped
    analytics chart and the long-range history endpoint so both stay
    consistent."""
    chart_dates, chart_sales, chart_orders = [], [], []
    current = datetime.strptime(start_date, "%Y-%m-%d").date()
    end     = datetime.strptime(end_date,   "%Y-%m-%d").date()
    daily_dict = {d.date: d for d in daily_data}

    while current <= end:
        date_str = current.strftime("%Y-%m-%d")
        chart_dates.append(date_str)
        if date_str in daily_dict:
            chart_sales.append(round(daily_dict[date_str].gross_sales_firo or 0, 8))
            chart_orders.append(daily_dict[date_str].orders_count or 0)
        else:
            chart_sales.append(0)
            chart_orders.append(0)
        current += timedelta(days=1)

    return {"dates": chart_dates, "sales": chart_sales, "orders": chart_orders}


async def get_user_chart_history(db: AsyncSession, user_id: str, days: int = 1095) -> dict:
    """Long-range daily chart series (default up to 3 years) for the
    draggable/scrollable Sales Volume and Activity Heatmap views decoupled
    from the 24H/7D/30D/90D period buttons, which only scope the summary
    stat cards. Capped at 1095 days (3 years) to keep the query bounded."""
    days = max(1, min(days, 1095))
    end_date   = _today_str()
    start_date = _date_str(datetime.now(timezone.utc) - timedelta(days=days - 1))

    res = await db.execute(
        select(UserDailyStats).where(
            UserDailyStats.user_id == user_id,
            UserDailyStats.date >= start_date,
            UserDailyStats.date <= end_date
        ).order_by(UserDailyStats.date)
    )
    return _build_daily_series(res.scalars().all(), start_date, end_date)


async def get_user_hourly_chart(db: AsyncSession, user_id: str) -> dict:
    """Last 24 hours of confirmed-payment activity, bucketed by hour the
    24H button needs finer-than-a-day granularity, which UserDailyStats
    can't provide. Computed on the fly straight from Payment rows since
    it's a small, bounded 24-bucket window rather than a pre-aggregated
    table like the daily stats."""
    now        = datetime.now(timezone.utc)
    end_hour   = now.replace(minute=0, second=0, microsecond=0)
    start_hour = end_hour - timedelta(hours=23)

    res = await db.execute(
        select(Payment.confirmed_at, Payment.amount_firo, Payment.order_id).where(
            Payment.merchant_id == user_id,
            Payment.status == PaymentStatus.confirmed,
            Payment.confirmed_at >= start_hour
        )
    )

    labels, buckets = [], {}
    current = start_hour
    while current <= end_hour:
        key = current.strftime("%Y-%m-%dT%H:00")
        labels.append(key)
        buckets[key] = {"sales": 0.0, "orders": 0}
        current += timedelta(hours=1)

    for confirmed_at, amount_firo, order_id in res.all():
        if not confirmed_at:
            continue
        # Plan-purchase payments aren't "this merchant's sales" — same
        # exclusion as the daily/long-range series above.
        if (order_id or "").startswith("plan:"):
            continue
        key = confirmed_at.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:00")
        bucket = buckets.get(key)
        if bucket:
            bucket["sales"]   = round(bucket["sales"] + (amount_firo or 0), 8)
            bucket["orders"] += 1

    return {
        "labels": labels,
        "sales":  [buckets[k]["sales"]  for k in labels],
        "orders": [buckets[k]["orders"] for k in labels],
    }


async def get_or_create_daily_stats(db: AsyncSession, date_str: str) -> DailyStats:
    res = await db.execute(
        select(DailyStats).where(DailyStats.date == date_str)
    )
    stats = res.scalar_one_or_none()
    if not stats:
        stats = DailyStats(date=date_str)
        db.add(stats)
        await db.flush()
    return stats


async def get_or_create_user_daily_stats(db: AsyncSession, user_id: str, date_str: str) -> UserDailyStats:
    res = await db.execute(
        select(UserDailyStats).where(
            UserDailyStats.user_id == user_id,
            UserDailyStats.date == date_str
        )
    )
    stats = res.scalar_one_or_none()
    if not stats:
        stats = UserDailyStats(user_id=user_id, date=date_str)
        db.add(stats)
        await db.flush()
    return stats


async def on_payment_confirmed(db: AsyncSession, payment: Payment):
    try:
        date_str = _date_str(payment.confirmed_at or datetime.now(timezone.utc))
        amount   = payment.amount_received or payment.amount_firo

        daily = await get_or_create_daily_stats(db, date_str)
        daily.total_volume_firo  = round((daily.total_volume_firo or 0) + amount, 8)
        daily.transactions_count = (daily.transactions_count or 0) + 1

        user_daily = await get_or_create_user_daily_stats(db, payment.merchant_id, date_str)
        user_daily.gross_sales_firo  = round((user_daily.gross_sales_firo or 0) + (payment.amount_firo or 0), 8)
        user_daily.received_firo     = round((user_daily.received_firo or 0) + amount, 8)
        user_daily.orders_count      = (user_daily.orders_count or 0) + 1
        user_daily.successful_payments = (user_daily.successful_payments or 0) + 1

        await db.flush()
        logger.debug(f"[analytics] Updated stats for payment {payment.id[:8]}")

    except Exception as e:
        logger.error(f"[analytics] Failed to update payment stats: {e}")


async def on_payment_failed(db: AsyncSession, payment: Payment):
    try:
        date_str = _date_str(
            payment.confirmed_at or payment.created_at or datetime.now(timezone.utc)
        )

        user_daily = await get_or_create_user_daily_stats(db, payment.merchant_id, date_str)
        user_daily.failed_payments = (user_daily.failed_payments or 0) + 1

        await db.flush()
        logger.debug(f"[analytics] Recorded failed payment for user {payment.merchant_id[:8]}")

    except Exception as e:
        logger.error(f"[analytics] Failed to update failed payment stats: {e}")


async def on_user_registered(db: AsyncSession, user: User):
    try:
        date_str = _today_str()

        daily = await get_or_create_daily_stats(db, date_str)
        daily.new_users = (daily.new_users or 0) + 1

        await db.flush()
        logger.debug("[analytics] Recorded new user registration")

    except Exception as e:
        logger.error(f"[analytics] Failed to update new user stats: {e}")


def _get_date_range(period: str) -> tuple[str, str]:
    today = datetime.now(timezone.utc).date()

    if period == "today":
        start = today
    elif period == "7d":
        start = today - timedelta(days=6)
    elif period == "30d":
        start = today - timedelta(days=29)
    elif period == "90d":
        start = today - timedelta(days=89)
    else:
        start = today - timedelta(days=6)

    return start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")


def _get_previous_range(period: str, start_date: str) -> tuple[str, str]:
    """The immediately-preceding window of the same length, for delta badges
    ("vs. previous period") e.g. for 7d this is the 7 days before start_date."""
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    days = {"today": 1, "7d": 7, "30d": 30, "90d": 90}.get(period, 7)
    prev_end   = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)
    return prev_start.strftime("%Y-%m-%d"), prev_end.strftime("%Y-%m-%d")


async def _sum_period(db: AsyncSession, user_id: str, start_date: str, end_date: str):
    res = await db.execute(
        select(
            func.sum(UserDailyStats.gross_sales_firo),
            func.sum(UserDailyStats.received_firo),
            func.sum(UserDailyStats.orders_count),
            func.sum(UserDailyStats.successful_payments),
            func.sum(UserDailyStats.failed_payments)
        ).where(
            UserDailyStats.user_id == user_id,
            UserDailyStats.date >= start_date,
            UserDailyStats.date <= end_date
        )
    )
    row = res.first()
    return {
        "gross_sales": float(row[0] or 0),
        "received":    float(row[1] or 0),
        "orders":      int(row[2] or 0),
        "successful":  int(row[3] or 0),
        "failed":      int(row[4] or 0),
    }


async def get_user_analytics(db: AsyncSession, user_id: str, period: str = "7d") -> dict:
    start_date, end_date = _get_date_range(period)
    cur = await _sum_period(db, user_id, start_date, end_date)

    prev_start, prev_end = _get_previous_range(period, start_date)
    prev = await _sum_period(db, user_id, prev_start, prev_end)

    gross_sales     = cur["gross_sales"]
    received        = cur["received"]
    orders          = cur["orders"]
    successful      = cur["successful"]
    failed          = cur["failed"]
    total_attempts  = successful + failed
    success_rate    = round(successful / total_attempts * 100, 1) if total_attempts else 0.0
    avg_order_value = round(gross_sales / orders, 8) if orders else 0.0

    prev_total_attempts = prev["successful"] + prev["failed"]
    prev_success_rate = (
        round(prev["successful"] / prev_total_attempts * 100, 1) if prev_total_attempts else 0.0
    )

    chart_res = await db.execute(
        select(UserDailyStats).where(
            UserDailyStats.user_id == user_id,
            UserDailyStats.date >= start_date,
            UserDailyStats.date <= end_date
        ).order_by(UserDailyStats.date)
    )
    chart_series = _build_daily_series(chart_res.scalars().all(), start_date, end_date)

    # Live invoice-status breakdown (donut chart) a point-in-time snapshot
    # of every payment this merchant currently has, not filtered by period.
    # Broken down by SOURCE + OUTCOME rather than raw status, since that's
    # more actionable for a merchant: where invoices come from (payment
    # links vs. direct/API checkout) and how many failed or were cancelled.
    # Plan-purchase payments are excluded entirely those are the merchant
    # paying the operator for their FiroGate subscription, not a customer
    # paying the merchant, so they aren't "this merchant's invoices" at all
    # (same exclusion already applied to gross_sales/received above).
    src_res = await db.execute(
        select(Payment.order_id, Payment.status)
        .where(Payment.merchant_id == user_id)
    )
    paylink_ct = direct_ct = failed_cancelled_ct = 0
    for order_id, status in src_res.all():
        order_id = order_id or ""
        if order_id.startswith("plan:"):
            continue
        status_val = status.value if hasattr(status, "value") else str(status)
        if status_val in ("failed", "cancelled", "expired"):
            failed_cancelled_ct += 1
        elif order_id.startswith("LINK-"):
            paylink_ct += 1
        else:
            direct_ct += 1
    invoice_total = paylink_ct + direct_ct + failed_cancelled_ct

    return {
        "gross_sales_firo":    round(gross_sales, 8),
        "received_firo":       round(received, 8),
        "orders_count":        orders,
        "successful_payments": successful,
        "failed_payments":     failed,
        "success_rate_pct":    success_rate,
        "avg_order_value":     avg_order_value,
        # vs. the immediately-preceding period of equal length powers the
        # ▲/▼ delta badges on the stat cards. None when there's no prior data.
        "deltas": {
            "gross_sales_firo": _pct_change(gross_sales, prev["gross_sales"]),
            "received_firo":    _pct_change(received, prev["received"]),
            "orders_count":     _pct_change(orders, prev["orders"]),
            "success_rate_pct": _pct_change(success_rate, prev_success_rate),
        },
        "invoice_status": {
            "total":            invoice_total,
            "paylink":          paylink_ct,
            "direct":           direct_ct,
            "failed_cancelled": failed_cancelled_ct,
        },
        "chart": chart_series,
        "period": period
    }
