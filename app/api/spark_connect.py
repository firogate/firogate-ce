"""
app/api/spark_connect.py

Spark view-key mode connect a Spark FULL VIEW KEY exported from the
merchant's own wallet (e.g. Campfire: Settings > Wallet Settings > Spark
View Key). No spend authority is granted by this key at all it can only
decrypt which coins on the public anonymity set belong to the merchant,
and their amounts. FiroGate never sees a seed, mnemonic, or spend key.

There is no proof-of-ownership challenge here: a view key grants pure
visibility, not control, so submitting the wrong key only means the
merchant themselves won't see their own payments.

Endpoints:
  POST /spark/connect     register the view key
  GET  /spark/status      connection status
  POST /spark/disconnect  remove the connection
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import encrypt_field, decrypt_field
from app.models.models import User
from app.models.spark import SparkWalletConnection
from app.api.payments import get_merchant, require_api_permission
from app.services.sparkmobile import SparkViewKey, SparkViewKeyError
from loguru import logger

router = APIRouter(prefix="/spark", tags=["spark-connect"])


class SparkConnectIn(BaseModel):
    view_key_hex: str
    network:      str = "testnet"   # testnet | mainnet
    label:        str | None = None


@router.post("/connect", status_code=201, dependencies=[Depends(require_api_permission("wallet_connect"))])
async def connect_spark(
    body: SparkConnectIn,
    request: Request,
    merchant: User = Depends(get_merchant),
    db: AsyncSession = Depends(get_db),
):
    view_key_hex = (body.view_key_hex or "").strip()
    if not view_key_hex:
        raise HTTPException(422, "view_key_hex is required")
    if body.network not in ("testnet", "mainnet"):
        raise HTTPException(422, "network must be 'testnet' or 'mainnet'")

    # Validate the key actually deserializes before storing it - catches
    # copy-paste mistakes immediately instead of failing silently later
    # in the scanner.
    try:
        vk = SparkViewKey(view_key_hex, is_testnet=(body.network != "mainnet"))
        vk.get_address(diversifier=1)  # exercise the key end-to-end
    except SparkViewKeyError as e:
        raise HTTPException(422, f"Invalid Spark view key: {e}")

    view_key_enc = encrypt_field(view_key_hex)

    res = await db.execute(
        select(SparkWalletConnection).where(SparkWalletConnection.merchant_id == merchant.id)
    )
    conn = res.scalar_one_or_none()
    if conn is None:
        conn = SparkWalletConnection(
            merchant_id=merchant.id,
            view_key_enc=view_key_enc,
            label=body.label,
            network=body.network,
        )
        db.add(conn)
    else:
        conn.view_key_enc = view_key_enc
        conn.label = body.label or conn.label
        conn.network = body.network
        conn.is_active = True
        conn.next_diversifier = 1

    await db.commit()

    from app.services.payment_engine import invalidate_view_key_cache
    invalidate_view_key_cache(merchant.id)

    logger.info(f"spark view key connected merchant={merchant.id[:8]} network={body.network}")
    return {"connected": True, "network": body.network}


@router.get("/status", dependencies=[Depends(require_api_permission("wallet_connect"))])
async def spark_status(
    merchant: User = Depends(get_merchant),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(SparkWalletConnection).where(SparkWalletConnection.merchant_id == merchant.id)
    )
    conn = res.scalar_one_or_none()
    if conn is None or not conn.is_active:
        return {"connected": False, "enabled": True}
    try:
        suffix = decrypt_field(conn.view_key_enc)[-8:]
    except Exception:
        suffix = None
    return {
        "connected":        True,
        "enabled":          True,
        "network":          conn.network,
        "label":            conn.label,
        "view_key_suffix":  suffix,
        "connected_at":     conn.connected_at.isoformat() if conn.connected_at else None,
        "last_scanned_at":  conn.last_scanned_at.isoformat() if conn.last_scanned_at else None,
    }


@router.post("/disconnect", dependencies=[Depends(require_api_permission("wallet_connect"))])
async def disconnect_spark(
    request: Request,
    merchant: User = Depends(get_merchant),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(SparkWalletConnection).where(SparkWalletConnection.merchant_id == merchant.id)
    )
    conn = res.scalar_one_or_none()
    if conn is None:
        raise HTTPException(404, "No Spark connection found")

    conn.is_active = False
    await db.commit()

    from app.services.payment_engine import invalidate_view_key_cache
    invalidate_view_key_cache(merchant.id)

    return {"disconnected": True}
