"""
app/services/telegram_bot.py

Telegram Bot API helper notification channel (separate from OIDC login).

Connect flow (works for EVERY account, not just Telegram-login users):
  1. Dashboard requests a connect link → single-use token, 10-min TTL.
  2. User opens https://t.me/<bot>?start=<token> and presses Start.
  3. Telegram delivers "/start <token>" to our webhook; we verify the
     webhook secret header, consume the token, and link that chat id to
     the FiroGate account that requested it. Nobody can link someone
     else's account: the token is random, single-use, expiring, and only
     ever shown to the logged-in owner over the authenticated API.

Security:
  * Bot token lives in .env only; never sent to any client.
  * Webhook is authenticated via X-Telegram-Bot-Api-Secret-Token an
    HMAC derived from SECRET_KEY, registered with setWebhook. Requests
    without the exact header are dropped.
  * Connect tokens: 32-byte urlsafe, single-use, 600 s TTL, stored
    server-side only.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

import httpx
from loguru import logger

from app.core.config import get_settings

API_BASE = "https://api.telegram.org"
CONNECT_TTL = 600

# token -> (user_id, created_ts)
_connect_tokens: dict[str, tuple[str, float]] = {}


def _evict() -> None:
    cutoff = time.time() - CONNECT_TTL
    for k in [k for k, (_, ts) in _connect_tokens.items() if ts < cutoff]:
        _connect_tokens.pop(k, None)
    while len(_connect_tokens) > 10_000:
        _connect_tokens.pop(next(iter(_connect_tokens)), None)


def new_connect_token(user_id: str) -> str:
    _evict()
    token = secrets.token_urlsafe(24)
    _connect_tokens[token] = (str(user_id), time.time())
    return token


def consume_connect_token(token: str) -> str | None:
    _evict()
    entry = _connect_tokens.pop(token or "", None)
    return entry[0] if entry else None


def webhook_secret() -> str:
    s = get_settings()
    return hmac.new(s.SECRET_KEY.encode(), b"tg-webhook-v1", hashlib.sha256).hexdigest()


def connect_link(token: str) -> str:
    s = get_settings()
    return f"https://t.me/{s.TELEGRAM_BOT_USERNAME}?start={token}"


async def tg_api(method: str, payload: dict) -> dict | None:
    s = get_settings()
    if not s.TELEGRAM_BOT_TOKEN:
        return None
    url = f"{API_BASE}/bot{s.TELEGRAM_BOT_TOKEN}/{method}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json=payload)
        data = r.json()
        if not data.get("ok"):
            logger.warning(f"[tg-bot] {method} failed: {str(data)[:200]}")
            return None
        return data.get("result")
    except Exception as e:
        logger.warning(f"[tg-bot] {method} error: {e}")
        return None


async def send_message(chat_id: str, text: str) -> bool:
    res = await tg_api("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })
    return res is not None


async def setup_webhook() -> None:
    """Best-effort webhook registration on startup (idempotent)."""
    s = get_settings()
    if not (s.telegram_bot_enabled and s.BASE_URL.startswith("https")):
        return
    url = s.BASE_URL.rstrip("/") + "/api/telegram/webhook"
    res = await tg_api("setWebhook", {
        "url": url,
        "secret_token": webhook_secret(),
        "allowed_updates": ["message"],
        "drop_pending_updates": True,
    })
    if res is not None:
        logger.info(f"[tg-bot] webhook registered: {url}")


def _esc(v) -> str:
    return (str(v) if v is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def send_payment_notification(
    chat_id: str, *,
    merchant_name: str,
    amount_firo: float,
    order_id: str | None,
    order_description: str | None,
    customer_email: str | None,
    txid: str | None,
) -> bool:
    amt = f"{float(amount_firo or 0):.8f}".rstrip("0").rstrip(".")
    lines = [
        f"✅ <b>Payment confirmed</b> {_esc(merchant_name)}",
        "",
        f"<b>Amount:</b> {_esc(amt)} FIRO",
    ]
    if order_id:          lines.append(f"<b>Order:</b> {_esc(order_id)}")
    if order_description: lines.append(f"<b>Product:</b> {_esc(order_description[:80])}")
    if customer_email:    lines.append(f"<b>Customer:</b> {_esc(customer_email)}")
    if txid:              lines.append(f"<b>Txid:</b> <code>{_esc(txid)}</code>")
    return await send_message(chat_id, "\n".join(lines))
