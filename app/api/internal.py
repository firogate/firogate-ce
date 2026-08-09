"""Inbound trigger for Firo Core's blocknotify (`blocknotify=<cmd>` in
firo.conf). Spark payments are only observable once mined into a block, so
"new block arrived" is exactly the right signal to accelerate detection —
this is purely additive: the existing 20s-interval scanner job keeps running
unchanged as the fallback if blocknotify is never configured, misfires, or
the node restarts without re-registering the hook.
"""
import asyncio
import secrets

from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.core.config import get_settings

router = APIRouter(prefix="/api/internal", tags=["internal"])

_scan_lock = asyncio.Lock()
_scan_pending = False
_scan_running = False


@router.post("/blocknotify")
async def blocknotify(secret: str, background_tasks: BackgroundTasks):
    settings = get_settings()
    if not settings.BLOCKNOTIFY_SECRET or not secrets.compare_digest(secret, settings.BLOCKNOTIFY_SECRET):
        raise HTTPException(403, "invalid secret")
    background_tasks.add_task(_trigger_scan_debounced)
    return {"accepted": True}


async def _trigger_scan_debounced():
    """Coalesces a burst of blocknotify calls (reorg, multi-block catch-up)
    into at most one extra scan pass after the currently-running one
    finishes, instead of launching concurrent overlapping scans."""
    global _scan_pending, _scan_running
    async with _scan_lock:
        if _scan_running:
            _scan_pending = True
            return
        _scan_running = True
    try:
        from app.services.payment_engine import check_spark_payments
        await check_spark_payments()
        while True:
            async with _scan_lock:
                if not _scan_pending:
                    _scan_running = False
                    return
                _scan_pending = False
            await check_spark_payments()
    except Exception:
        async with _scan_lock:
            _scan_running = False
        raise
