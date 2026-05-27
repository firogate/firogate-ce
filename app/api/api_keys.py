"""
Multi-key API Key management.

Security model:
  - Raw key shown ONCE (on creation) — never stored
  - Only SHA-256 hash stored in DB
  - Key format: fg_live_{16-char-random}_{16-char-random}
  - Prefix (first 16 chars) stored for display
  - Auth: existing X-API-Key header updated to check api_keys table

Endpoints:
  GET    /api/keys          → list all keys (no secrets)
  POST   /api/keys          → create new key (returns raw key ONCE)
  DELETE /api/keys/{key_id} → revoke key
  PATCH  /api/keys/{key_id} → rename key
"""

import hashlib, secrets, string
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.models import ApiKey, User
from app.api.users import get_current_user

router = APIRouter(prefix="/api/keys", tags=["api-keys"])

MAX_KEYS_PER_MERCHANT = 10


# ─ Helpers ──
def _generate_raw_key() -> str:
    """Generate fg_live_{32 random alphanum chars}"""
    alphabet = string.ascii_letters + string.digits
    random_part = ''.join(secrets.choice(alphabet) for _ in range(32))
    return f"fg_live_{random_part}"


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _prefix_from_key(raw: str) -> str:
    """Store first 20 chars for display: fg_live_AbCdEfGh…"""
    return raw[:20] + "…"


def _fmt(k: ApiKey) -> dict:
    return {
        "id":         k.id,
        "name":       k.name,
        "prefix":     k.prefix,
        "status":     k.status,
        "scopes":     k.scopes or "*",
        "created_at": k.created_at.isoformat() if k.created_at else None,
        "last_used":  k.last_used.isoformat() if k.last_used else None,
        "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None,
    }


# ─ List keys ─
@router.get("")
async def list_keys(
    user: User = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(ApiKey)
        .where(ApiKey.merchant_id == user.id)
        .order_by(ApiKey.created_at.desc())
    )
    keys = res.scalars().all()
    return {"keys": [_fmt(k) for k in keys]}


# ─ Create key ─
class CreateKeyBody(BaseModel):
    name: str = "Default"

@router.post("", status_code=201)
async def create_key(
    body: CreateKeyBody,
    user: User = Depends(get_current_user),
    db:   AsyncSession = Depends(get_db),
):
    # Cap per merchant
    count_res = await db.execute(
        select(ApiKey).where(
            ApiKey.merchant_id == user.id,
            ApiKey.status == "active",
        )
    )
    if len(count_res.scalars().all()) >= MAX_KEYS_PER_MERCHANT:
        raise HTTPException(400, f"Maximum {MAX_KEYS_PER_MERCHANT} active keys allowed")

    # Sanitize name
    name = (body.name or "Default").strip()[:64]
    if not name:
        name = "Default"

    # Generate key
    raw_key = _generate_raw_key()
    key_hash = _hash_key(raw_key)
    prefix   = _prefix_from_key(raw_key)

    key = ApiKey(
        merchant_id = str(user.id),
        name        = name,
        prefix      = prefix,
        key_hash    = key_hash,
        status      = "active",
    )
    db.add(key)
    await db.commit()
    await db.refresh(key)

    return {
        "id":      key.id,
        "name":    key.name,
        "prefix":  key.prefix,
        "status":  key.status,
        "created_at": key.created_at.isoformat(),
        # Raw key returned ONCE — never available again
        "key": raw_key,
        "warning": "This key will only be shown once. Copy it now.",
    }


# ─ Rename key ─
class RenameKeyBody(BaseModel):
    name: str

@router.patch("/{key_id}")
async def rename_key(
    key_id: str,
    body:   RenameKeyBody,
    user:   User = Depends(get_current_user),
    db:     AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.merchant_id == user.id)
    )
    key = res.scalar_one_or_none()
    if not key:
        raise HTTPException(404)
    key.name = (body.name or "").strip()[:64] or "Untitled"
    db.add(key)
    await db.commit()
    return {"ok": True, "name": key.name}


# ─ Revoke key ─
@router.delete("/{key_id}", status_code=200)
async def revoke_key(
    key_id: str,
    user:   User = Depends(get_current_user),
    db:     AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.merchant_id == user.id)
    )
    key = res.scalar_one_or_none()
    if not key:
        raise HTTPException(404)
    if key.status == "revoked":
        raise HTTPException(400, "Key already revoked")

    key.status     = "revoked"
    key.revoked_at = datetime.now(timezone.utc)
    db.add(key)
    await db.commit()
    return {"ok": True, "id": key_id, "status": "revoked"}


# ─ Auth helper — used by payments.py ─
async def get_merchant_by_api_key(raw_key: str, db: AsyncSession) -> User | None:
    """
    Look up merchant by API key.
    Checks both new api_keys table AND legacy user.api_key field.
    Updates last_used timestamp.
    """
    if not raw_key or len(raw_key) < 10:
        return None

    # 1. Check new multi-key table
    key_hash = _hash_key(raw_key)
    res = await db.execute(
        select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.status == "active")
    )
    api_key_row = res.scalar_one_or_none()

    if api_key_row:
        # Update last_used (non-blocking)
        api_key_row.last_used = datetime.now(timezone.utc)
        db.add(api_key_row)
        # Get merchant
        user_res = await db.execute(
            select(User).where(User.id == api_key_row.merchant_id)
        )
        user = user_res.scalar_one_or_none()
        try:
            await db.commit()
        except Exception:
            pass
        return user

    # 2. Legacy fallback — old single api_key field on User
    legacy_res = await db.execute(
        select(User).where(User.api_key == raw_key, User.api_key_active == True)
    )
    return legacy_res.scalar_one_or_none()
