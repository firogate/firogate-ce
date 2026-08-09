import asyncio
import shutil

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.config import get_settings
from app.core.security import verify_access_token
from app.core.system_settings import (
    HIDE_AUTH_PAGES_KEY, REQUIRED_CONFIRMATIONS_KEY, PAYMENT_TIMEOUT_MINUTES_KEY,
    get_setting, set_setting,
)
from app.models.models import User, UserRole, Payment

router = APIRouter(prefix="/api/panel", tags=["panel"])
settings = get_settings()


async def require_operator(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    tok = request.cookies.get("access_token") or ""
    if not tok:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            tok = auth[7:]
    uid = verify_access_token(tok) if tok else None
    if not uid:
        raise HTTPException(401, "not authenticated")
    u = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
    if not u or not u.is_active:
        raise HTTPException(401, "not authenticated")
    if u.role != UserRole.operator and not (settings.is_operator_email(u.email or "") or settings.is_operator_username(u.username or "")):
        raise HTTPException(403, "operator only")
    if settings.PANEL_REQUIRE_2FA and not u.totp_enabled:
        raise HTTPException(403, "2fa_required: enable two-factor authentication to access these settings")
    return u


@router.get("/webhook-status")
async def get_webhook_status(
    operator: User = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
):
    if not operator.webhook_url:
        return {"url": None, "healthy": True, "last_result": "No webhook configured", "recent_attempts": [], "attempt_labels": []}

    recent = (await db.execute(
        select(Payment)
        .where(Payment.merchant_id == operator.id, Payment.webhook_attempts > 0)
        .order_by(Payment.webhook_sent_at.desc().nullslast(), Payment.created_at.desc())
        .limit(5)
    )).scalars().all()
    recent = list(reversed(recent))

    attempts = [{"ok": bool(p.webhook_sent)} for p in recent]
    labels   = [(p.confirmed_at or p.created_at).strftime("%H:%M") if (p.confirmed_at or p.created_at) else "" for p in recent]
    healthy  = all(a["ok"] for a in attempts) if attempts else True
    last     = recent[-1] if recent else None

    return {
        "url":             operator.webhook_url,
        "healthy":         healthy,
        "last_result":     (last.webhook_response if last else None) or "No deliveries yet",
        "recent_attempts": attempts,
        "attempt_labels":  labels,
    }


class WebhookRetryIn(BaseModel):
    payment_id: str | None = None


@router.post("/webhook-retry")
async def retry_webhook(
    body: WebhookRetryIn = None,
    operator: User = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
):
    if not operator.webhook_url:
        raise HTTPException(400, "No webhook URL configured.")

    target = None
    if body and body.payment_id:
        target = (await db.execute(
            select(Payment).where(Payment.id == body.payment_id, Payment.merchant_id == operator.id)
        )).scalar_one_or_none()
        if not target:
            raise HTTPException(404, "Payment not found.")
    else:
        target = (await db.execute(
            select(Payment)
            .where(Payment.merchant_id == operator.id, Payment.webhook_sent == False, Payment.webhook_attempts > 0)
            .order_by(Payment.created_at.desc())
            .limit(1)
        )).scalar_one_or_none()

    if not target:
        return {"ok": False, "message": "No failed webhook deliveries to retry."}

    from app.services.webhook import fire_webhook
    await fire_webhook(db, target)
    await db.refresh(target)
    return {
        "ok": bool(target.webhook_sent),
        "message": "Delivered." if target.webhook_sent else (target.webhook_response or "Delivery failed."),
    }


@router.get("/settings")
async def get_settings_bundle(
    operator: User = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
):
    return {
        "hide_auth_pages": (await get_setting(db, HIDE_AUTH_PAGES_KEY, "0")) == "1",
        "required_confirmations": int(await get_setting(db, REQUIRED_CONFIRMATIONS_KEY, str(settings.REQUIRED_CONFIRMATIONS))),
        "payment_timeout_minutes": int(await get_setting(db, PAYMENT_TIMEOUT_MINUTES_KEY, str(settings.PAYMENT_TIMEOUT_MINUTES))),
    }


class SettingsIn(BaseModel):
    hide_auth_pages: bool | None = None
    required_confirmations: int | None = None
    payment_timeout_minutes: int | None = None


@router.patch("/settings")
async def update_settings_bundle(
    body: SettingsIn,
    operator: User = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
):
    if body.hide_auth_pages is not None:
        await set_setting(db, HIDE_AUTH_PAGES_KEY, "1" if body.hide_auth_pages else "0")
    if body.required_confirmations is not None:
        if body.required_confirmations not in (0, 1, 3, 6):
            raise HTTPException(422, "required_confirmations must be one of 0, 1, 3, 6")
        await set_setting(db, REQUIRED_CONFIRMATIONS_KEY, str(body.required_confirmations))
    if body.payment_timeout_minutes is not None:
        if not (1 <= body.payment_timeout_minutes <= 1440):
            raise HTTPException(422, "payment_timeout_minutes must be between 1 and 1440")
        await set_setting(db, PAYMENT_TIMEOUT_MINUTES_KEY, str(body.payment_timeout_minutes))
    return await get_settings_bundle(operator, db)


_SCHEDULED_JOB_IDS = ("payment_monitor", "spark_scanner", "webhook_retry", "db_cleanup", "accounting_check")


@router.get("/diagnostics")
async def get_diagnostics(operator: User = Depends(require_operator)):
    from app.services.firo_rpc import get_rpc
    from app.core.database import AsyncSessionLocal

    rpc = get_rpc()
    checks: list[dict] = []

    async def _rpc_connected():
        ok = await rpc.ping()
        return ("RPC Connected", ok, None)

    async def _wallet_loaded():
        info = await rpc.get_wallet_info()
        return ("Wallet Loaded", bool(info), None)

    async def _spark_ready():
        try:
            coin_id = await rpc.get_spark_latest_coin_id()
            return ("Spark Ready", coin_id is not None, f"coin id {coin_id}" if coin_id is not None else None)
        except Exception:
            return ("Spark Ready", False, None)

    async def _blockchain_synced():
        try:
            info = await rpc.get_blockchain_info()
            progress = float(info.get("verificationprogress") or 0)
            height = info.get("blocks")
            synced = progress >= 0.999
            detail = f"{progress*100:.1f}% · block {height}" if height is not None else f"{progress*100:.1f}%"
            return ("Blockchain Synced", synced, detail)
        except Exception:
            return ("Blockchain Synced", False, None)

    async def _database_healthy():
        try:
            async with AsyncSessionLocal() as db:
                await db.execute(text("SELECT 1"))
            return ("Database Healthy", True, None)
        except Exception:
            return ("Database Healthy", False, None)

    async def _storage_healthy():
        try:
            usage = shutil.disk_usage(".")
            free_pct = usage.free / usage.total * 100
            healthy = free_pct >= 5.0
            free_gb = usage.free / (1024 ** 3)
            return ("Storage Healthy", healthy, f"{free_gb:.1f} GB free ({free_pct:.0f}%)")
        except Exception:
            return ("Storage Healthy", False, None)

    def _workers_scheduler():
        from app.main import scheduler
        job_ids = {j.id for j in scheduler.get_jobs()}
        missing = [j for j in _SCHEDULED_JOB_IDS if j not in job_ids]
        running = scheduler.running and not missing
        detail = "all jobs registered" if not missing else f"missing: {', '.join(missing)}"
        return ("Workers Running", running, detail)

    results = await asyncio.gather(
        _rpc_connected(), _wallet_loaded(), _spark_ready(),
        _blockchain_synced(), _database_healthy(), _storage_healthy(),
    )
    for name, ok, detail in results:
        checks.append({"name": name, "healthy": ok, "detail": detail})
    wname, wok, wdetail = _workers_scheduler()
    checks.append({"name": wname, "healthy": wok, "detail": wdetail})

    healthy_count = sum(1 for c in checks if c["healthy"])
    score = round(healthy_count / len(checks) * 100) if checks else 0

    from loguru import logger
    logger.info(f"[diagnostics] merchant={operator.id[:8]} score={score} checks={checks}")

    return {"score": score, "checks": checks}
