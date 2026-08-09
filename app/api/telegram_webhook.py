"""
app/api/telegram_webhook.py

    POST /api/telegram/webhook updates pushed by Telegram's servers.

Authenticated by the X-Telegram-Bot-Api-Secret-Token header registered
with setWebhook (constant-time compared). Handles only:

    /start <token>   link this chat to the account that minted the token
    /stop            turn off notifications for the linked account

Everything else is ignored. Always answers 200 so Telegram does not
retry-flood; real errors are logged server-side only.
"""
from __future__ import annotations

import secrets as _secrets

from fastapi import APIRouter, Depends, Request, Response
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.models import User
from app.services import telegram_bot as tg

router = APIRouter(prefix="/api/telegram", tags=["telegram-bot"])


@router.post("/webhook")
async def telegram_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not _secrets.compare_digest(header, tg.webhook_secret()):
        return Response(status_code=403)

    try:
        update = await request.json()
    except Exception:
        return {"ok": True}

    msg = (update or {}).get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    text = (msg.get("text") or "").strip()
    if not chat_id or chat.get("type") != "private" or not text:
        return {"ok": True}

    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        token = parts[1].strip() if len(parts) == 2 else ""
        user_id = tg.consume_connect_token(token)
        if not user_id:
            await tg.send_message(chat_id,
                "This link is invalid or expired. Open your FiroGate dashboard → "
                "Security → Notifications and tap <b>Connect Telegram</b> again.")
            return {"ok": True}
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if not user:
            return {"ok": True}
        # One chat per account; a chat may re-link (moves to the newest account link)
        prev = (await db.execute(select(User).where(User.telegram_chat_id == chat_id))).scalars().all()
        for p in prev:
            if p.id != user.id:
                p.telegram_chat_id = None
                p.notify_telegram = False
                db.add(p)
        user.telegram_chat_id = chat_id
        user.notify_telegram = True
        db.add(user)
        await db.commit()
        try:
            from app.services.event_bus import EventBus, make_event
            import asyncio as _asyncio
            _asyncio.create_task(EventBus.publish_merchant(
                str(user.id), make_event("telegram.connected")))
        except Exception:
            pass
        await tg.send_message(chat_id,
            f"✅ Connected to FiroGate account <b>{tg._esc(user.username)}</b>.\n"
            "You will receive payment notifications here.\n\n"
            "Send /stop anytime to turn them off.")
        logger.info(f"[tg-bot] chat linked to user {user.id[:8]}…")
        return {"ok": True}

    if text.startswith("/stop"):
        user = (await db.execute(select(User).where(User.telegram_chat_id == chat_id))).scalar_one_or_none()
        if user:
            user.notify_telegram = False
            db.add(user)
            await db.commit()
            await tg.send_message(chat_id,
                "🔕 Notifications turned off. You can re-enable them from your "
                "FiroGate dashboard.")
        return {"ok": True}

    return {"ok": True}
