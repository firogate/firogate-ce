import json
import httpx
from datetime import datetime, timezone, timedelta
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Payment, User
from app.core.security import sign_webhook, decrypt_field, build_webhook_payload
from app.core.config import get_settings

RETRY_DELAYS = [0, 60, 300, 1800, 7200]

def _parse_metadata(payment: Payment) -> dict:
    """Safely parse metadata_json — always returns a dict, never raises."""
    if not payment.metadata_json:
        return {}
    try:
        parsed = json.loads(payment.metadata_json)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}

MAX_ATTEMPTS = 5


def _build_client(url: str) -> httpx.AsyncClient:
    s = get_settings()
    use_tor = s.should_use_tor_for_url(url)
    if use_tor:
        transport = httpx.AsyncHTTPTransport(proxy=s.tor_socks_url)
        return httpx.AsyncClient(timeout=30, transport=transport)
    return httpx.AsyncClient(timeout=10)


async def fire_webhook(db: AsyncSession, payment: Payment):

    res = await db.execute(select(User).where(User.id == payment.merchant_id))
    merchant = res.scalar_one_or_none()
    if not merchant or not merchant.webhook_url:
        return

    secret = ""
    if merchant.webhook_secret_enc:
        try:
            secret = decrypt_field(merchant.webhook_secret_enc)
        except Exception:
            pass

    base_payload = {
        "event":           "payment.confirmed",
        "payment_id":      payment.id,
        "order_id":        payment.order_id,
        "amount_firo":     payment.amount_firo,
        "amount_received": payment.amount_received,
        "platform_fee":    payment.platform_fee_firo,
        "merchant_net":    payment.merchant_net_firo,
        "txid":            payment.txid,
        "confirmations":   payment.confirmations,
        "customer_email":  payment.customer_email,
        "confirmed_at":    datetime.now(timezone.utc).isoformat(),
        "metadata":        _parse_metadata(payment),
    }

    await _send_webhook(db, payment, merchant.webhook_url, secret, base_payload, "payment.confirmed")


async def fire_withdrawal_webhook(db: AsyncSession, withdrawal, event: str):
    res = await db.execute(select(User).where(User.id == withdrawal.merchant_id))
    merchant = res.scalar_one_or_none()
    if not merchant or not merchant.webhook_url:
        return

    secret = ""
    if merchant.webhook_secret_enc:
        try:
            secret = decrypt_field(merchant.webhook_secret_enc)
        except Exception:
            pass

    base_payload = {
        "event":            event,
        "withdrawal_id":    withdrawal.id,
        "amount_requested": withdrawal.amount_requested,
        "amount_net":       withdrawal.amount_net,
        "fee_firo":         withdrawal.withdrawal_fee_firo,
        "status":           withdrawal.status,
        "sent_txid":        withdrawal.sent_txid,
        "withdrawal_type":  getattr(withdrawal, "withdrawal_type", "transparent"),
    }

    url = merchant.webhook_url
    payload   = build_webhook_payload(base_payload)
    signature = sign_webhook(payload, secret) if secret else ""
    headers   = _build_headers(event, signature, payload)

    try:
        async with _build_client(url) as c:
            r = await c.post(url, json=payload, headers=headers)
        logger.info(f"Withdrawal webhook ({event}) → {url[:50]} status={r.status_code}")
    except Exception as e:
        _log_webhook_failure(url, str(e))


async def _send_webhook(
    db: AsyncSession,
    payment: Payment,
    url: str,
    secret: str,
    base_payload: dict,
    event: str,
):

    payload   = build_webhook_payload(base_payload)
    signature = sign_webhook(payload, secret) if secret else ""
    headers   = _build_headers(event, signature, payload)

    attempts = (payment.webhook_attempts or 0) + 1
    payment.webhook_attempts = attempts

    try:
        async with _build_client(url) as c:
            r = await c.post(url, json=payload, headers=headers)

        payment.webhook_sent     = True
        payment.webhook_sent_at  = datetime.now(timezone.utc)
        payment.webhook_response = f"{r.status_code}"
        payment.webhook_next_retry_at = None

        if r.status_code >= 400:

            payment.webhook_sent = False
            _schedule_retry(payment, attempts, f"HTTP {r.status_code}")
        else:
            logger.info(f"Webhook ✅ → {url[:50]} status={r.status_code} (attempt {attempts})")

    except Exception as e:
        err = str(e)
        payment.webhook_response = f"error: {err[:100]}"
        _schedule_retry(payment, attempts, err)
        _log_webhook_failure(url, err)

    await db.commit()


def _schedule_retry(payment: Payment, attempts: int, reason: str):
    if attempts >= MAX_ATTEMPTS:
        payment.webhook_next_retry_at = None
        logger.warning(
            f"Webhook permanently failed for payment {payment.id[:8]} "
            f"after {attempts} attempts. Last error: {reason[:80]}"
        )
        return

    delay = RETRY_DELAYS[min(attempts, len(RETRY_DELAYS) - 1)]
    payment.webhook_next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
    logger.warning(
        f"Webhook failed (attempt {attempts}/{MAX_ATTEMPTS}) for {payment.id[:8]}, "
        f"retry in {delay}s. Reason: {reason[:60]}"
    )


def _build_headers(event: str, signature: str, payload: dict) -> dict:
    return {
        "Content-Type":        "application/json",
        "X-FiroGate-Event":     event,
        "X-FiroGate-Signature": signature,
        "X-FiroGate-Nonce":     payload["nonce"],
        "X-FiroGate-Timestamp": str(payload["timestamp"]),
        "User-Agent":          "FiroGate/1.0",
    }


def _log_webhook_failure(url: str, error: str):
    s = get_settings()
    is_onion = ".onion" in url
    if is_onion and not s.TOR_ENABLED:
        logger.warning(
            f"Webhook FAILED (.onion but TOR_ENABLED=false): {error}\n"
            f"  → Set TOR_ENABLED=true in .env"
        )
    elif not is_onion and s.TOR_ENABLED and not s.TOR_ALL_TRAFFIC:
        logger.warning(
            f"Webhook FAILED (clearnet, TOR_ENABLED=true, TOR_ALL_TRAFFIC=false): {error}\n"
            f"  → Set TOR_ALL_TRAFFIC=true or use .onion webhook URL"
        )
    else:
        logger.warning(f"Webhook FAILED → {url[:50]}: {error}")


async def retry_failed_webhooks():
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        from app.models.models import PaymentStatus
        from sqlalchemy import and_

        now = datetime.now(timezone.utc)
        res = await db.execute(
            select(Payment).where(
                and_(
                    Payment.webhook_sent == False,
                    Payment.webhook_next_retry_at.isnot(None),
                    Payment.webhook_next_retry_at <= now,
                    Payment.status == PaymentStatus.confirmed,
                    Payment.webhook_attempts < MAX_ATTEMPTS,
                )
            ).limit(20)
        )
        due = res.scalars().all()
        if not due:
            return

        logger.info(f"Webhook retry: {len(due)} pending")

        for payment in due:
            merchant_res = await db.execute(
                select(User).where(User.id == payment.merchant_id)
            )
            merchant = merchant_res.scalar_one_or_none()
            if not merchant or not merchant.webhook_url:
                continue

            secret = ""
            if merchant.webhook_secret_enc:
                try:
                    secret = decrypt_field(merchant.webhook_secret_enc)
                except Exception:
                    pass

            base_payload = {
                "event":           "payment.confirmed",
                "payment_id":      payment.id,
                "order_id":        payment.order_id,
                "amount_firo":     payment.amount_firo,
                "amount_received": payment.amount_received,
                "platform_fee":    payment.platform_fee_firo,
                "merchant_net":    payment.merchant_net_firo,
                "txid":            payment.txid,
                "confirmations":   payment.confirmations,
                "customer_email":  payment.customer_email,
                "confirmed_at":    payment.confirmed_at.isoformat() if payment.confirmed_at else "",
                "metadata":        _parse_metadata(payment),
                "retry_attempt":   payment.webhook_attempts + 1,
            }

            await _send_webhook(db, payment, merchant.webhook_url, secret, base_payload, "payment.confirmed")



async def fire_cancellation_webhook(db: AsyncSession, payment: Payment, cancelled_at: datetime):
    """Fire webhook for payment.cancelled event."""
    res = await db.execute(select(User).where(User.id == payment.merchant_id))
    merchant = res.scalar_one_or_none()
    if not merchant or not merchant.webhook_url:
        return

    secret = ""
    if merchant.webhook_secret_enc:
        try:
            secret = decrypt_field(merchant.webhook_secret_enc)
        except Exception:
            pass

    base_payload = {
        "event":        "payment.cancelled",
        "payment_id":   payment.id,
        "order_id":     payment.order_id,
        "amount_firo":  payment.amount_firo,
        "status":       "cancelled",
        "cancelled_at": cancelled_at.isoformat(),
        "metadata":     _parse_metadata(payment),
    }

    url = merchant.webhook_url
    payload   = build_webhook_payload(base_payload)
    signature = sign_webhook(payload, secret) if secret else ""
    headers   = _build_headers("payment.cancelled", signature, payload)

    try:
        async with _build_client(url) as c:
            r = await c.post(url, json=payload, headers=headers)
        logger.info(f"Cancellation webhook (payment.cancelled) → {url[:50]} status={r.status_code}")
    except Exception as e:
        _log_webhook_failure(url, str(e))


async def fire_plan_activated_webhook(db: AsyncSession, payment: Payment, merchant: User, activated_at: datetime):
    """Fire webhook for plan.activated event."""
    if not merchant.webhook_url:
        return

    plan_name = _parse_metadata(payment).get("plan", "")

    secret = ""
    if merchant.webhook_secret_enc:
        try:
            secret = decrypt_field(merchant.webhook_secret_enc)
        except Exception:
            pass

    base_payload = {
        "event":        "plan.activated",
        "payment_id":   payment.id,
        "order_id":     payment.order_id,
        "plan":         plan_name,
        "amount_firo":  payment.amount_firo,
        "txid":         payment.txid,
        "confirmations": payment.confirmations,
        "activated_at": activated_at.isoformat(),
        "metadata":     _parse_metadata(payment),
    }

    url = merchant.webhook_url
    payload   = build_webhook_payload(base_payload)
    signature = sign_webhook(payload, secret) if secret else ""
    headers   = _build_headers("plan.activated", signature, payload)

    try:
        async with _build_client(url) as c:
            r = await c.post(url, json=payload, headers=headers)
        logger.info(f"Plan activation webhook (plan.activated) → {url[:50]} status={r.status_code}")
    except Exception as e:
        _log_webhook_failure(url, str(e))