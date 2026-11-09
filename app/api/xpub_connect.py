from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.payments import require_api_permission
from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import decrypt_field, encrypt_field
from app.core.xpub import Xpub, XpubError
from app.models.models import User, XpubWalletConnection
from app.models.spark import SparkWalletConnection

router = APIRouter(prefix="/api/xpub", tags=["xpub"])


class XpubIn(BaseModel):
    xpub: str


class MethodsIn(BaseModel):
    spark_enabled: bool | None = None
    transparent_enabled: bool | None = None
    default_method: str | None = None


async def _conn(db: AsyncSession, merchant_id: str) -> XpubWalletConnection | None:
    res = await db.execute(select(XpubWalletConnection).where(XpubWalletConnection.merchant_id == merchant_id))
    return res.scalar_one_or_none()


async def _has_spark(db: AsyncSession, merchant_id: str) -> bool:
    res = await db.execute(select(SparkWalletConnection.id).where(
        SparkWalletConnection.merchant_id == merchant_id, SparkWalletConnection.is_active == True).limit(1))
    return res.scalar_one_or_none() is not None


async def _status(db: AsyncSession, merchant: User) -> dict:
    c = await _conn(db, merchant.id)
    spark = await _has_spark(db, merchant.id)
    out = {
        "connected": bool(c and c.is_active),
        "spark_connected": spark,
        "spark_enabled": merchant.pay_spark_enabled is not False,
        "transparent_enabled": merchant.pay_transparent_enabled is not False,
        "default_method": merchant.pay_default_method or "spark",
    }
    if c and c.is_active:
        out.update({
            "network": c.network,
            "first_address": c.first_address,
            "addresses_used": c.next_index or 0,
            "connected_at": c.connected_at.isoformat() if c.connected_at else None,
        })
    return out


@router.get("/status")
async def xpub_status(
    merchant: User = Depends(require_api_permission("wallet_connect")),
    db: AsyncSession = Depends(get_db),
):
    return await _status(db, merchant)


@router.post("/preview")
async def xpub_preview(
    body: XpubIn,
    merchant: User = Depends(require_api_permission("wallet_connect")),
):
    try:
        key = Xpub.parse(body.xpub)
    except XpubError as e:
        raise HTTPException(422, str(e))
    expected = "testnet" if get_settings().is_testnet else "mainnet"
    if key.network != expected:
        raise HTTPException(422, f"This is a {key.network} key, but this server runs on {expected}.")
    return {"network": key.network, "depth": key.depth, "addresses": [key.address_at(i) for i in range(3)]}


@router.post("/connect", status_code=201)
async def xpub_connect(
    body: XpubIn,
    merchant: User = Depends(require_api_permission("wallet_connect")),
    db: AsyncSession = Depends(get_db),
):
    try:
        key = Xpub.parse(body.xpub)
    except XpubError as e:
        raise HTTPException(422, str(e))
    expected = "testnet" if get_settings().is_testnet else "mainnet"
    if key.network != expected:
        raise HTTPException(422, f"This is a {key.network} key, but this server runs on {expected}.")
    first = key.address_at(0)
    clean = body.xpub.strip()
    c = await _conn(db, merchant.id)
    if c is None:
        c = XpubWalletConnection(merchant_id=merchant.id, xpub_enc=encrypt_field(clean), network=key.network)
        db.add(c)
    else:
        same = False
        try:
            same = decrypt_field(c.xpub_enc) == clean
        except Exception:
            pass
        if not same:
            c.next_index = 0
        c.xpub_enc = encrypt_field(clean)
        c.network = key.network
    c.first_address = first
    c.is_active = True
    c.connected_at = datetime.now(timezone.utc)
    await db.commit()
    return await _status(db, merchant)


@router.patch("/methods")
async def update_methods(
    body: MethodsIn,
    merchant: User = Depends(require_api_permission("wallet_connect")),
    db: AsyncSession = Depends(get_db),
):
    if body.default_method is not None and body.default_method not in ("spark", "transparent"):
        raise HTTPException(422, "Default must be spark or transparent.")
    spark_on = merchant.pay_spark_enabled is not False if body.spark_enabled is None else body.spark_enabled
    trans_on = merchant.pay_transparent_enabled is not False if body.transparent_enabled is None else body.transparent_enabled
    c = await _conn(db, merchant.id)
    has_spark = await _has_spark(db, merchant.id)
    has_xpub = bool(c and c.is_active)
    if not ((spark_on and has_spark) or (trans_on and has_xpub)):
        raise HTTPException(409, "Keep at least one connected method on.")
    merchant.pay_spark_enabled = spark_on
    merchant.pay_transparent_enabled = trans_on
    if body.default_method is not None:
        merchant.pay_default_method = body.default_method
    db.add(merchant)
    await db.commit()
    await db.refresh(merchant)
    return await _status(db, merchant)


@router.post("/disconnect")
async def xpub_disconnect(
    merchant: User = Depends(require_api_permission("wallet_connect")),
    db: AsyncSession = Depends(get_db),
):
    c = await _conn(db, merchant.id)
    if not c or not c.is_active:
        raise HTTPException(404, "No xpub connected.")
    c.is_active = False
    await db.commit()
    return await _status(db, merchant)
