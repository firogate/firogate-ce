"""
Analytics API Endpoints - Fast dashboard analytics with pre-computed data
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.api.users import get_current_user
from app.models.models import User
from app.services.analytics_service import (
    get_admin_analytics, get_user_analytics, backfill_analytics
)

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


# /api/analytics/admin — only available in Enterprise Edition
# In Community Edition this endpoint is not registered


@router.get("/user")
async def user_analytics(
    period: str = Query("7d", regex="^(today|7d|30d)$"),
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

# /api/analytics/backfill — Enterprise only
