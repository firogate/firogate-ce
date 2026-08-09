"""
Analytics API Endpoints - Fast dashboard analytics with pre-computed data
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.rate_limit import rate_limit_strict
from app.api.users import get_current_user
from app.models.models import User, UserDailyStats
from app.services.analytics_service import get_user_analytics, get_user_chart_history, get_user_hourly_chart

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("/user")
async def user_analytics(
    period: str = Query("7d", regex="^(today|7d|30d|90d)$"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Get merchant dashboard analytics.

    Query params:
    - period: 'today', '7d', or '30d'

    Returns aggregated stats for the authenticated user.
    """
    return await get_user_analytics(db, user.id, period)


@router.get("/user/history")
async def user_chart_history(
    days: int = Query(1095, ge=1, le=1095),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Long-range daily chart series (dates/sales/orders) for the draggable
    Sales Volume and Activity Heatmap views up to 3 years back, decoupled
    from the 24H/7D/30D/90D period buttons on /api/analytics/user.
    """
    return await get_user_chart_history(db, user.id, days)


@router.get("/user/hourly")
async def user_hourly_chart(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Last 24 hours of confirmed-payment activity bucketed by hour the 24H
    chart view needs finer granularity than the daily series can provide.
    """
    return await get_user_hourly_chart(db, user.id)


class ResetAnalyticsIn(BaseModel):
    confirm: str  # must be exactly "RESET" a lightweight server-side
                  # backstop against a stray/accidental client-side call,
                  # in addition to the UI's own confirmation dialog.


@router.post("/user/reset", dependencies=[Depends(rate_limit_strict)])
async def reset_user_analytics(
    body: ResetAnalyticsIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Permanently clears this merchant's analytics history both the
    per-day breakdown (UserDailyStats, used by the charts/period filters)
    and the lifetime running totals shown on the dashboard overview cards
    (User.lifetime_gross_sales_firo etc). Existing Payment rows are left
    untouched; this only resets the derived/aggregated numbers so future
    analytics start counting from zero going forward.

    Irreversible the UI must show an explicit warning before calling this.
    """
    if body.confirm != "RESET":
        raise HTTPException(422, 'Type "RESET" to confirm this cannot be undone.')

    await db.execute(delete(UserDailyStats).where(UserDailyStats.user_id == user.id))

    user.lifetime_gross_sales_firo   = 0.0
    user.lifetime_received_firo      = 0.0
    user.lifetime_confirmed_payments = 0
    user.lifetime_completed_orders   = 0
    db.add(user)

    await db.commit()
    return {"ok": True, "message": "Analytics reset new data will start accumulating from today."}
