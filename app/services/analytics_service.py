"""
Analytics Service - Updates aggregated stats for fast dashboard queries

This service updates DailyStats and UserDailyStats tables on:
- Payment confirmation
- New user registration
- Failed/expired payments
"""

from datetime import datetime, timezone, timedelta
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.models.models import (
    DailyStats, UserDailyStats, User, UserRole,
    Payment, PaymentStatus
)


def _today_str() -> str:
    """Get today's date as YYYY-MM-DD string"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _date_str(dt: datetime) -> str:
    """Convert datetime to YYYY-MM-DD string"""
    return dt.strftime("%Y-%m-%d")


async def get_or_create_daily_stats(db: AsyncSession, date_str: str) -> DailyStats:
    """Get or create DailyStats record for a given date"""
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
    """Get or create UserDailyStats record for a given user and date"""
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
    """
    Called when a payment is confirmed.
    Updates both platform and user daily stats.
    """
    try:
        date_str = _date_str(payment.confirmed_at or datetime.now(timezone.utc))
        
        # Update platform daily stats
        daily = await get_or_create_daily_stats(db, date_str)
        daily.total_revenue = round((daily.total_revenue or 0) + (payment.amount_received or payment.amount_firo), 8)
        daily.transactions_count = (daily.transactions_count or 0) + 1
        daily.platform_fees = round((daily.platform_fees or 0) + (payment.platform_fee_firo or 0), 8)
        
        # Update user daily stats
        user_daily = await get_or_create_user_daily_stats(db, payment.merchant_id, date_str)
        user_daily.revenue = round((user_daily.revenue or 0) + (payment.merchant_net_firo or 0), 8)
        user_daily.orders_count = (user_daily.orders_count or 0) + 1
        user_daily.successful_payments = (user_daily.successful_payments or 0) + 1
        
        await db.flush()
        logger.debug(f"[analytics] Updated stats for payment {payment.id[:8]}...")
        
    except Exception as e:
        logger.error(f"[analytics] Failed to update payment stats: {e}")


async def on_payment_failed(db: AsyncSession, payment: Payment):
    """
    Called when a payment fails or expires.
    Updates user daily stats with failed count.
    """
    try:
        date_str = _today_str()
        
        user_daily = await get_or_create_user_daily_stats(db, payment.merchant_id, date_str)
        user_daily.failed_payments = (user_daily.failed_payments or 0) + 1
        
        await db.flush()
        logger.debug(f"[analytics] Recorded failed payment for user {payment.merchant_id[:8]}...")
        
    except Exception as e:
        logger.error(f"[analytics] Failed to update failed payment stats: {e}")


async def on_user_registered(db: AsyncSession, user: User):
    """
    Called when a new user registers.
    Updates platform daily stats with new user count.
    """
    try:
        date_str = _today_str()
        
        daily = await get_or_create_daily_stats(db, date_str)
        daily.new_users = (daily.new_users or 0) + 1
        
        await db.flush()
        logger.debug("[analytics] Recorded new user registration")
        
    except Exception as e:
        logger.error(f"[analytics] Failed to update new user stats: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# Query Functions for Dashboard
# ═══════════════════════════════════════════════════════════════════════════════

def _get_date_range(period: str) -> tuple[str, str]:
    """Get start and end date strings for a period"""
    today = datetime.now(timezone.utc).date()
    
    if period == "today":
        start = today
    elif period == "7d":
        start = today - timedelta(days=6)
    elif period == "30d":
        start = today - timedelta(days=29)
    else:
        start = today - timedelta(days=6)  # Default to 7 days
    
    return start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")


async def get_admin_analytics(db: AsyncSession, period: str = "7d") -> dict:
    """
    Get aggregated analytics for admin dashboard.
    Uses pre-computed daily stats for performance.
    """
    start_date, end_date = _get_date_range(period)
    
    # Get aggregated stats from daily_stats table
    res = await db.execute(
        select(
            func.sum(DailyStats.total_revenue),
            func.sum(DailyStats.transactions_count),
            func.sum(DailyStats.new_users),
            func.sum(DailyStats.platform_fees)
        ).where(
            DailyStats.date >= start_date,
            DailyStats.date <= end_date
        )
    )
    row = res.first()
    
    # Get total users (this is a simple count, fast query)
    total_users = (await db.execute(
        select(func.count(User.id)).where(User.role == UserRole.merchant)
    )).scalar() or 0
    
    # Get daily data for charts
    chart_res = await db.execute(
        select(DailyStats).where(
            DailyStats.date >= start_date,
            DailyStats.date <= end_date
        ).order_by(DailyStats.date)
    )
    daily_data = chart_res.scalars().all()
    
    # Fill in missing dates with zeros
    chart_dates = []
    chart_revenue = []
    chart_transactions = []
    
    current = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    daily_dict = {d.date: d for d in daily_data}
    
    while current <= end:
        date_str = current.strftime("%Y-%m-%d")
        chart_dates.append(date_str)
        
        if date_str in daily_dict:
            chart_revenue.append(round(daily_dict[date_str].total_revenue or 0, 8))
            chart_transactions.append(daily_dict[date_str].transactions_count or 0)
        else:
            chart_revenue.append(0)
            chart_transactions.append(0)
        
        current += timedelta(days=1)
    
    # Get latest transactions for table
    latest_tx = await db.execute(
        select(Payment).where(
            Payment.status == PaymentStatus.confirmed
        ).order_by(Payment.confirmed_at.desc()).limit(10)
    )
    transactions = latest_tx.scalars().all()
    
    # Get usernames for transactions
    tx_list = []
    for tx in transactions:
        user_res = await db.execute(select(User.username).where(User.id == tx.merchant_id))
        username = user_res.scalar() or "Unknown"
        tx_list.append({
            "txid": tx.txid or tx.id[:12] + "...",
            "amount": round(tx.amount_received or tx.amount_firo, 4),
            "status": tx.status.value if hasattr(tx.status, 'value') else str(tx.status),
            "user": username,
            "date": tx.confirmed_at.isoformat() if tx.confirmed_at else tx.created_at.isoformat()
        })
    
    return {
        "total_revenue": round(row[0] or 0, 8),
        "total_transactions": int(row[1] or 0),
        "total_users": total_users,
        "new_users": int(row[2] or 0),
        "platform_fees": round(row[3] or 0, 8),
        "chart": {
            "dates": chart_dates,
            "revenue": chart_revenue,
            "transactions": chart_transactions
        },
        "latest_transactions": tx_list,
        "period": period
    }


async def get_user_analytics(db: AsyncSession, user_id: str, period: str = "7d") -> dict:
    """
    Get aggregated analytics for a specific merchant.
    Uses pre-computed user_daily_stats for performance.
    """
    start_date, end_date = _get_date_range(period)
    
    # Get aggregated stats from user_daily_stats table
    res = await db.execute(
        select(
            func.sum(UserDailyStats.revenue),
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
    
    # Get daily data for charts
    chart_res = await db.execute(
        select(UserDailyStats).where(
            UserDailyStats.user_id == user_id,
            UserDailyStats.date >= start_date,
            UserDailyStats.date <= end_date
        ).order_by(UserDailyStats.date)
    )
    daily_data = chart_res.scalars().all()
    
    # Fill in missing dates with zeros
    chart_dates = []
    chart_revenue = []
    chart_orders = []
    
    current = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    daily_dict = {d.date: d for d in daily_data}
    
    while current <= end:
        date_str = current.strftime("%Y-%m-%d")
        chart_dates.append(date_str)
        
        if date_str in daily_dict:
            chart_revenue.append(round(daily_dict[date_str].revenue or 0, 8))
            chart_orders.append(daily_dict[date_str].orders_count or 0)
        else:
            chart_revenue.append(0)
            chart_orders.append(0)
        
        current += timedelta(days=1)
    
    return {
        "revenue": round(row[0] or 0, 8),
        "orders_count": int(row[1] or 0),
        "successful_payments": int(row[2] or 0),
        "failed_payments": int(row[3] or 0),
        "chart": {
            "dates": chart_dates,
            "revenue": chart_revenue,
            "orders": chart_orders
        },
        "period": period
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Backfill Function - Run once to populate historical data
# ═══════════════════════════════════════════════════════════════════════════════

async def backfill_analytics(db: AsyncSession):
    """
    One-time backfill of analytics data from existing payments.
    Run this once after adding the analytics tables.
    """
    logger.info("[analytics] Starting backfill of historical data...")
    
    # Get all confirmed payments
    payments_res = await db.execute(
        select(Payment).where(Payment.status == PaymentStatus.confirmed)
    )
    payments = payments_res.scalars().all()
    
    # Aggregate by date for platform stats
    platform_stats = {}
    user_stats = {}
    
    for p in payments:
        date_str = _date_str(p.confirmed_at or p.created_at)
        
        # Platform stats
        if date_str not in platform_stats:
            platform_stats[date_str] = {
                "revenue": 0, "transactions": 0, "fees": 0
            }
        platform_stats[date_str]["revenue"] += (p.amount_received or p.amount_firo)
        platform_stats[date_str]["transactions"] += 1
        platform_stats[date_str]["fees"] += (p.platform_fee_firo or 0)
        
        # User stats
        key = (p.merchant_id, date_str)
        if key not in user_stats:
            user_stats[key] = {"revenue": 0, "orders": 0, "success": 0}
        user_stats[key]["revenue"] += (p.merchant_net_firo or 0)
        user_stats[key]["orders"] += 1
        user_stats[key]["success"] += 1
    
    # Insert platform stats
    for date_str, stats in platform_stats.items():
        daily = await get_or_create_daily_stats(db, date_str)
        daily.total_revenue = round(stats["revenue"], 8)
        daily.transactions_count = stats["transactions"]
        daily.platform_fees = round(stats["fees"], 8)
    
    # Insert user stats
    for (user_id, date_str), stats in user_stats.items():
        user_daily = await get_or_create_user_daily_stats(db, user_id, date_str)
        user_daily.revenue = round(stats["revenue"], 8)
        user_daily.orders_count = stats["orders"]
        user_daily.successful_payments = stats["success"]
    
    # Count new users by registration date
    users_res = await db.execute(
        select(User).where(User.role == UserRole.merchant)
    )
    users = users_res.scalars().all()
    
    user_counts = {}
    for u in users:
        date_str = _date_str(u.created_at)
        user_counts[date_str] = user_counts.get(date_str, 0) + 1
    
    for date_str, count in user_counts.items():
        daily = await get_or_create_daily_stats(db, date_str)
        daily.new_users = count
    
    await db.commit()
    logger.success(f"[analytics] Backfill complete: {len(payments)} payments, {len(users)} users processed")
