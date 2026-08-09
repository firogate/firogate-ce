from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import SystemConfig

HIDE_AUTH_PAGES_KEY = "hide_auth_pages"
REQUIRED_CONFIRMATIONS_KEY = "required_confirmations"
PAYMENT_TIMEOUT_MINUTES_KEY = "payment_timeout_minutes"


async def get_setting(db: AsyncSession, key: str, default: str | None = None) -> str | None:
    row = (await db.execute(select(SystemConfig).where(SystemConfig.key == key))).scalar_one_or_none()
    if row is None or row.value is None:
        return default
    return row.value


async def set_setting(db: AsyncSession, key: str, value: str) -> None:
    row = (await db.execute(select(SystemConfig).where(SystemConfig.key == key))).scalar_one_or_none()
    if row is None:
        db.add(SystemConfig(key=key, value=value))
    else:
        row.value = value
    await db.commit()


async def hide_auth_pages_enabled(db: AsyncSession) -> bool:
    return (await get_setting(db, HIDE_AUTH_PAGES_KEY, "0")) == "1"
