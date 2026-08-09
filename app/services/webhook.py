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
    """Safely parse metadata_json always returns a dict, never raises."""
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
    # follow_redirects=False so a malicious endpoint can't 302-redirect the
    # webhook to an internal target (SSRF bypass). Responses are not consumed
    # for content, so we don't need to chase redirects anyway.
    if use_tor:
        transport = httpx.AsyncHTTPTransport(proxy=s.tor_socks_url)
        return httpx.AsyncClient(timeout=30, transport=transport, follow_redirects=False)
    return httpx.AsyncClient(timeout=10, follow_redirects=False)


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
        "txid":            payment.txid,
        "confirmations":   payment.confirmations,
        "customer_email":  payment.customer_email,
        "confirmed_at":    datetime.now(timezone.utc).isoformat(),
        "metadata":        _parse_metadata(payment),
    }

    await _send_webhook(db, payment, merchant.webhook_url, secret, base_payload, "payment.confirmed")


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

    # Re-validate at send time the URL was checked when the merchant saved
    # it, but a DNS record can be repointed to an internal/metadata address
    # after that (DNS rebinding) before this delivery fires.
    try:
        from app.core.validators import validate_url as _validate_webhook_url
        url = _validate_webhook_url(url, "webhook_url") or url
    except Exception as e:
        payment.webhook_response = "error: webhook URL points to a disallowed host"
        _schedule_retry(payment, attempts, "disallowed host")
        await db.commit()
        return

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
                "txid":            payment.txid,
                "confirmations":   payment.confirmations,
                "customer_email":  payment.customer_email,
                "confirmed_at":    payment.confirmed_at.isoformat() if payment.confirmed_at else "",
                "metadata":        _parse_metadata(payment),
                "retry_attempt":   payment.webhook_attempts + 1,
            }

            await _send_webhook(db, payment, merchant.webhook_url, secret, base_payload, "payment.confirmed")



async def fire_expired_webhook(payment: Payment):
    """Fire webhook for payment.expired event (fire-and-forget, own DB session)."""
    from app.core.database import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
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
                "event":       "payment.expired",
                "payment_id":  payment.id,
                "order_id":    payment.order_id,
                "amount_firo": payment.amount_firo,
                "status":      "expired",
                "expired_at":  datetime.now(timezone.utc).isoformat(),
                "metadata":    _parse_metadata(payment),
            }

            url     = merchant.webhook_url
            try:
                from app.core.validators import validate_url as _validate_webhook_url
                url = _validate_webhook_url(url, "webhook_url") or url
            except Exception:
                logger.warning(f"fire_expired_webhook: webhook URL points to a disallowed host, skipping")
                return
            payload = build_webhook_payload(base_payload)
            sig     = sign_webhook(payload, secret) if secret else ""
            headers = _build_headers("payment.expired", sig, payload)

            async with _build_client(url) as c:
                r = await c.post(url, json=payload, headers=headers)
            logger.info(f"Expired webhook (payment.expired) → {url[:50]} status={r.status_code}")
    except Exception as e:
        logger.warning(f"fire_expired_webhook failed: {e}")


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
    try:
        from app.core.validators import validate_url as _validate_webhook_url
        url = _validate_webhook_url(url, "webhook_url") or url
    except Exception:
        logger.warning(f"fire_cancellation_webhook: webhook URL points to a disallowed host, skipping")
        return
    payload   = build_webhook_payload(base_payload)
    signature = sign_webhook(payload, secret) if secret else ""
    headers   = _build_headers("payment.cancelled", signature, payload)

    try:
        async with _build_client(url) as c:
            r = await c.post(url, json=payload, headers=headers)
        logger.info(f"Cancellation webhook (payment.cancelled) → {url[:50]} status={r.status_code}")
    except Exception as e:
        _log_webhook_failure(url, str(e))


