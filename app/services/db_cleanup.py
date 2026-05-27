
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import delete, func, select

from app.core.database import AsyncSessionLocal
from app.models.models import AuditLog, LoginAttempt


LOGIN_ATTEMPT_RETENTION_DAYS = 30
AUDIT_LOG_RETENTION_DAYS     = 90


BATCH_SIZE = 500


async def _delete_old_login_attempts(cutoff: datetime) -> int:
    total = 0
    async with AsyncSessionLocal() as db:
        while True:

            res = await db.execute(
                select(LoginAttempt.id)
                .where(LoginAttempt.created_at < cutoff)
                .limit(BATCH_SIZE)
            )
            ids = [row[0] for row in res.fetchall()]
            if not ids:
                break

            result = await db.execute(
                delete(LoginAttempt).where(LoginAttempt.id.in_(ids))
            )
            await db.commit()
            deleted = result.rowcount
            total  += deleted
            logger.debug(f"[db_cleanup] LoginAttempt: deleted batch of {deleted}")

            if deleted < BATCH_SIZE:
                break

    return total


async def _delete_old_audit_logs(cutoff: datetime) -> int:
    total = 0
    async with AsyncSessionLocal() as db:
        while True:
            res = await db.execute(
                select(AuditLog.id)
                .where(AuditLog.created_at < cutoff)
                .limit(BATCH_SIZE)
            )
            ids = [row[0] for row in res.fetchall()]
            if not ids:
                break

            result = await db.execute(
                delete(AuditLog).where(AuditLog.id.in_(ids))
            )
            await db.commit()
            deleted = result.rowcount
            total  += deleted
            logger.debug(f"[db_cleanup] AuditLog: deleted batch of {deleted}")

            if deleted < BATCH_SIZE:
                break

    return total


async def _count_table(model) -> int:
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(func.count()).select_from(model))
        return res.scalar() or 0


async def run_db_cleanup() -> dict:
    now = datetime.now(timezone.utc)
    login_cutoff = now - timedelta(days=LOGIN_ATTEMPT_RETENTION_DAYS)
    audit_cutoff = now - timedelta(days=AUDIT_LOG_RETENTION_DAYS)

    logger.info(
        f"[db_cleanup] Starting daily cleanup — "
        f"LoginAttempt cutoff={login_cutoff.date()} "
        f"AuditLog cutoff={audit_cutoff.date()}"
    )

    try:
        login_deleted = await _delete_old_login_attempts(login_cutoff)
    except Exception as exc:
        logger.error(f"[db_cleanup] LoginAttempt cleanup failed: {exc}")
        login_deleted = -1

    try:
        audit_deleted = await _delete_old_audit_logs(audit_cutoff)
    except Exception as exc:
        logger.error(f"[db_cleanup] AuditLog cleanup failed: {exc}")
        audit_deleted = -1

    login_remaining = await _count_table(LoginAttempt)
    audit_remaining = await _count_table(AuditLog)

    summary = {
        "ran_at":                now.isoformat(),
        "login_attempts_deleted": login_deleted,
        "login_attempts_remaining": login_remaining,
        "audit_logs_deleted":     audit_deleted,
        "audit_logs_remaining":   audit_remaining,
        "login_retention_days":   LOGIN_ATTEMPT_RETENTION_DAYS,
        "audit_retention_days":   AUDIT_LOG_RETENTION_DAYS,
    }

    logger.success(
        f"[db_cleanup] ✅ Done — "
        f"LoginAttempt: -{login_deleted} ({login_remaining} remain) | "
        f"AuditLog: -{audit_deleted} ({audit_remaining} remain)"
    )

    return summary
