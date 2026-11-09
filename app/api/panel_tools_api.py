import asyncio
import os
import platform
import shutil
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.config import get_settings
from app.core.security import verify_access_token
from app.core.system_settings import (
    HIDE_AUTH_PAGES_KEY, REQUIRED_CONFIRMATIONS_KEY, PAYMENT_TIMEOUT_MINUTES_KEY,
    LANDING_HOME_KEY,
    get_setting, set_setting,
)
from app.models.models import User, UserRole, Payment, PaymentStatus

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
    from app.core.operator_access import resolve_operator
    if not await resolve_operator(u, db):
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
        "landing_home": (await get_setting(db, LANDING_HOME_KEY, "0")) == "1",
    }


class SettingsIn(BaseModel):
    hide_auth_pages: bool | None = None
    required_confirmations: int | None = None
    payment_timeout_minutes: int | None = None
    landing_home: bool | None = None


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
    if body.landing_home is not None:
        await set_setting(db, LANDING_HOME_KEY, "1" if body.landing_home else "0")
    return await get_settings_bundle(operator, db)


_PROCESS_STARTED_AT = time.time()
_PROBE_TIMEOUT = 6.0
_JOBS = (
    ("payment_monitor", "Payment monitor"),
    ("spark_scanner", "Spark scanner"),
    ("webhook_retry", "Webhook retries"),
    ("accounting_check", "Accounting check"),
    ("db_cleanup", "Database cleanup"),
    ("recurring_billing", "Recurring payments"),
)
_STUCK_PENDING_GRACE = timedelta(minutes=15)
_STUCK_CONFIRMING_AFTER = timedelta(hours=3)
_STALE_BLOCK_SECONDS = 3600
_STALE_PRICE_SECONDS = 600


def _short_err(e: BaseException) -> str:
    msg = (getattr(e, "message", None) or str(e)).strip() or e.__class__.__name__
    return msg[:160]


async def _timed(coro, timeout: float = _PROBE_TIMEOUT):
    return await asyncio.wait_for(coro, timeout)


async def _nothing():
    return None


async def _node_snapshot() -> dict:
    from app.services.firo_rpc import get_rpc

    rpc = get_rpc()
    node: dict = {"online": False, "expected_chain": "test" if settings.is_testnet else "main"}
    t0 = time.perf_counter()
    try:
        chain = await _timed(rpc.get_blockchain_info())
    except Exception as e:
        node["error"] = _short_err(e)
        return node
    node["online"] = True
    node["latency_ms"] = round((time.perf_counter() - t0) * 1000)
    node["height"] = chain.get("blocks")
    node["headers"] = chain.get("headers")
    node["sync"] = float(chain.get("verificationprogress") or 0)
    node["chain"] = chain.get("chain")
    best = chain.get("bestblockhash")
    net, mem, header, coin = await asyncio.gather(
        _timed(rpc.get_network_info()),
        _timed(rpc.call("getmempoolinfo")),
        _timed(rpc.call("getblockheader", best, True)) if best else _nothing(),
        _timed(rpc.get_spark_latest_coin_id()),
        return_exceptions=True,
    )
    if isinstance(net, dict):
        node["peers"] = net.get("connections")
        node["version"] = (net.get("subversion") or "").strip("/")
    if isinstance(mem, dict):
        node["mempool"] = mem.get("size")
        node["mempool_bytes"] = mem.get("bytes")
    if isinstance(header, dict) and header.get("time"):
        node["last_block_age_s"] = max(0, int(time.time() - int(header["time"])))
    if isinstance(coin, int):
        node["spark_coin_id"] = coin
    elif isinstance(coin, BaseException):
        node["spark_error"] = _short_err(coin)
    return node


def _sqlite_path() -> str | None:
    url = settings.DATABASE_URL
    if ":///" not in url:
        return None
    return url.split(":///", 1)[1].split("?", 1)[0] or None


async def _db_snapshot() -> dict:
    from app.core.database import AsyncSessionLocal

    out: dict = {"ok": False, "kind": "SQLite" if settings.DATABASE_URL.startswith("sqlite") else "PostgreSQL"}
    t0 = time.perf_counter()
    try:
        async with AsyncSessionLocal() as db:
            await _timed(db.execute(text("SELECT 1")))
        out["ok"] = True
        out["latency_ms"] = round((time.perf_counter() - t0) * 1000)
    except Exception as e:
        out["error"] = _short_err(e)
    path = _sqlite_path()
    if path:
        try:
            out["size_bytes"] = os.path.getsize(path)
        except OSError:
            pass
    return out


async def _activity_snapshot() -> dict:
    from app.core.database import AsyncSessionLocal

    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)
    out: dict = {}
    try:
        async with AsyncSessionLocal() as db:
            async def count(*where):
                q = select(func.count()).select_from(Payment)
                if where:
                    q = q.where(*where)
                return int((await _timed(db.execute(q))).scalar() or 0)

            out["payments"] = {
                "pending": await count(Payment.status == PaymentStatus.pending),
                "confirming": await count(Payment.status == PaymentStatus.confirming),
                "stuck": await count(or_(
                    and_(Payment.status == PaymentStatus.pending,
                         Payment.expires_at.is_not(None),
                         Payment.expires_at < now - _STUCK_PENDING_GRACE),
                    and_(Payment.status == PaymentStatus.confirming,
                         Payment.created_at < now - _STUCK_CONFIRMING_AFTER),
                )),
                "created_24h": await count(Payment.created_at >= day_ago),
                "confirmed_24h": await count(Payment.status == PaymentStatus.confirmed,
                                             Payment.confirmed_at >= day_ago),
                "webhook_failed_24h": await count(Payment.webhook_sent.is_(False),
                                                  Payment.webhook_attempts > 0,
                                                  Payment.created_at >= day_ago),
            }
    except Exception as e:
        out["error"] = _short_err(e)
    return out


def _read_meminfo() -> dict:
    vals: dict = {}
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                k, _, rest = line.partition(":")
                if k in ("MemTotal", "MemAvailable"):
                    vals[k] = int(rest.split()[0]) * 1024
    except OSError:
        pass
    return vals


def _process_rss() -> int | None:
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


def _system_snapshot() -> dict:
    from app.core.version import APP_VERSION

    out: dict = {
        "uptime_s": int(time.time() - _PROCESS_STARTED_AT),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "rss_bytes": _process_rss(),
        "app_version": APP_VERSION,
    }
    try:
        du = shutil.disk_usage(os.path.dirname(_sqlite_path() or "") or ".")
        out["disk_total"] = du.total
        out["disk_free"] = du.free
    except OSError:
        pass
    mem = _read_meminfo()
    if mem.get("MemTotal"):
        out["mem_total"] = mem["MemTotal"]
        out["mem_available"] = mem.get("MemAvailable")
    if hasattr(os, "getloadavg"):
        try:
            out["load"] = [round(x, 2) for x in os.getloadavg()]
        except OSError:
            pass
    return out


def _every(trigger) -> str | None:
    iv = getattr(trigger, "interval", None)
    if isinstance(iv, timedelta):
        sec = int(iv.total_seconds())
        if sec < 120:
            return f"{sec}s"
        if sec < 7200:
            return f"{sec // 60} min"
        return f"{sec // 3600} h"
    fields = {f.name: str(f) for f in getattr(trigger, "fields", [])}
    if fields:
        hour, minute = fields.get("hour", "*"), fields.get("minute", "*")
        if hour.startswith("*/"):
            return f"{hour[2:]} h"
        if hour == "*":
            return "1 h"
        if hour.isdigit() and minute.isdigit():
            return f"daily {int(hour):02d}:{int(minute):02d} UTC"
    return None


def _jobs_snapshot() -> dict:
    try:
        from app.main import scheduler
    except Exception as e:
        return {"running": False, "jobs": [], "error": _short_err(e)}
    registered = {j.id: j for j in scheduler.get_jobs()}
    jobs = []
    for jid, label in _JOBS:
        j = registered.get(jid)
        nxt = getattr(j, "next_run_time", None) if j else None
        jobs.append({
            "id": jid,
            "label": label,
            "registered": j is not None,
            "paused": j is not None and nxt is None,
            "next_run": nxt.isoformat() if nxt else None,
            "every": _every(j.trigger) if j else None,
        })
    return {"running": bool(scheduler.running), "jobs": jobs}


async def _price_snapshot() -> dict:
    from app.services import price_service as ps

    cache = getattr(ps, "_cache", None) or {}
    fetched = cache.get("fetched_at") or 0
    if cache.get("price") is None or time.monotonic() - fetched > _STALE_PRICE_SECONDS:
        try:
            await _timed(ps._refresh_cache(), 5.0)
        except Exception:
            pass
    price = cache.get("price")
    fetched = cache.get("fetched_at")
    return {"price": price, "age_s": int(time.monotonic() - fetched) if fetched else None}


async def _tor_ok() -> bool | None:
    if not settings.TOR_ENABLED:
        return None
    try:
        _, writer = await _timed(asyncio.open_connection("127.0.0.1", settings.TOR_SOCKS_PORT), 2.0)
        writer.close()
        return True
    except Exception:
        return False


def _fmt_age(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h"


def _build_checks(node, db, act, sysinfo, jobs, price, tor_ok) -> list[dict]:
    checks: list[dict] = []

    def add(key, name, level, detail=None, group="core"):
        checks.append({"key": key, "name": name, "level": level, "healthy": level == "ok",
                       "detail": detail, "group": group})

    online = node.get("online")
    add("node", "Firo node", "ok" if online else "bad",
        f"{node.get('latency_ms')} ms" if online else "Can't reach the node")
    if not online:
        add("sync", "Blockchain sync", "bad", "Node offline")
    else:
        pct = node.get("sync", 0) * 100
        age = node.get("last_block_age_s")
        if node.get("sync", 0) < 0.999:
            add("sync", "Blockchain sync", "warn", f"Syncing {pct:.1f}%")
        elif age is not None and age > _STALE_BLOCK_SECONDS:
            add("sync", "Blockchain sync", "warn", f"No new block for {_fmt_age(age)}")
        else:
            add("sync", "Blockchain sync", "ok", f"Block {node.get('height'):,}" if node.get("height") else "Synced")
        chain = node.get("chain")
        if chain:
            is_test = chain != "main"
            if is_test != (node["expected_chain"] == "test"):
                add("chain", "Network", "bad", f"Node is {chain}, app expects {node['expected_chain']}")
            else:
                add("chain", "Network", "ok", "Testnet" if is_test else "Mainnet")
        peers = node.get("peers")
        if peers is not None:
            add("peers", "Node peers", "bad" if peers == 0 else ("warn" if peers < 3 else "ok"), f"{peers} connected")
    if online:
        ok_spark = node.get("spark_coin_id") is not None
        add("spark", "Spark", "ok" if ok_spark else "bad",
            f"Coin group {node['spark_coin_id']}" if ok_spark else (node.get("spark_error") or "Not ready"))
    else:
        add("spark", "Spark", "bad", "Node offline")
    add("db", "Database", "ok" if db.get("ok") else "bad",
        f"{db.get('kind')} · {db.get('latency_ms')} ms" if db.get("ok") else (db.get("error") or "Unreachable"))
    total, free = sysinfo.get("disk_total"), sysinfo.get("disk_free")
    if total:
        pct = free / total * 100
        add("disk", "Disk space", "ok" if pct >= 10 else ("warn" if pct >= 5 else "bad"),
            f"{free / 1024 ** 3:.1f} GB free ({pct:.0f}%)")
    mt, ma = sysinfo.get("mem_total"), sysinfo.get("mem_available")
    if mt and ma is not None:
        pct = ma / mt * 100
        add("memory", "Memory", "ok" if pct >= 10 else ("warn" if pct >= 5 else "bad"),
            f"{ma / 1024 ** 3:.1f} GB free of {mt / 1024 ** 3:.1f} GB")
    job_list = jobs.get("jobs") or []
    missing = [j["label"] for j in job_list if not j["registered"] or j["paused"]]
    if not jobs.get("running"):
        add("jobs", "Background jobs", "bad", "Scheduler stopped")
    elif missing:
        add("jobs", "Background jobs", "bad", "Not running: " + ", ".join(missing))
    else:
        add("jobs", "Background jobs", "ok", f"{len(job_list)} of {len(job_list)} running")
    if price.get("price") is None:
        add("price", "Price feed", "warn", "No price")
    elif (price.get("age_s") or 0) > _STALE_PRICE_SECONDS:
        add("price", "Price feed", "warn", f"${price['price']:.4f} · {_fmt_age(price['age_s'])} old")
    else:
        add("price", "Price feed", "ok", f"${price['price']:.4f}")
    pay = act.get("payments") or {}
    if "stuck" in pay:
        add("stuck", "Stuck payments", "ok" if pay["stuck"] == 0 else "warn",
            "None" if pay["stuck"] == 0 else f"{pay['stuck']} need a look", group="payments")
        failed = pay.get("webhook_failed_24h", 0)
        add("webhooks", "Webhooks (24 h)", "ok" if failed == 0 else "warn",
            "All delivered" if failed == 0 else f"{failed} failed", group="payments")
    tg = settings.telegram_bot_enabled
    add("telegram", "Telegram alerts", "ok" if tg else "off", "On" if tg else "Off", group="services")
    if tor_ok is None:
        add("tor", "Tor", "off", "Off", group="services")
    else:
        add("tor", "Tor", "ok" if tor_ok else "bad", "Proxy up" if tor_ok else "Proxy down", group="services")
    return checks


@router.get("/diagnostics")
async def get_diagnostics(operator: User = Depends(require_operator)):
    t0 = time.perf_counter()
    node, db, act, price, tor_ok = await asyncio.gather(
        _node_snapshot(), _db_snapshot(), _activity_snapshot(), _price_snapshot(), _tor_ok(),
    )
    sysinfo = _system_snapshot()
    jobs = _jobs_snapshot()
    checks = _build_checks(node, db, act, sysinfo, jobs, price, tor_ok)
    weights = {"ok": 1.0, "warn": 0.5, "bad": 0.0}
    scored = [weights[c["level"]] for c in checks if c["level"] in weights]
    score = round(sum(scored) / len(scored) * 100) if scored else 0
    levels = {c["level"] for c in checks}
    status = "problem" if "bad" in levels else ("attention" if "warn" in levels else "healthy")
    return {
        "score": score,
        "status": status,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "took_ms": round((time.perf_counter() - t0) * 1000),
        "checks": checks,
        "node": node,
        "database": db,
        "activity": act,
        "system": sysinfo,
        "jobs": jobs,
        "price": price,
    }
