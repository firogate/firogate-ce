from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.payments import require_api_permission
from app.core.config import get_settings
from app.core.database import get_db
from app.models.models import Payment, User, XpubWalletConnection
from app.models.spark_mint import SparkMintInput, SparkMintSettings
from app.models.spark import SparkWalletConnection
from app.services import mint_core as core

router = APIRouter(prefix="/api/spark-mints", tags=["spark-mints"])


class SettingsIn(BaseModel):
    check_enabled: bool


async def _check_enabled(db: AsyncSession, merchant_id: str) -> bool:
    row = await db.get(SparkMintSettings, merchant_id)
    return True if row is None else bool(row.check_enabled)


def _url(txid: str | None) -> str | None:
    return get_settings().tx_explorer_url(txid) if txid else None


def _item(r: SparkMintInput, order_id: str | None = None) -> dict:
    return {
        "payment_id": r.payment_id,
        "order_id": order_id,
        "txid": r.txid,
        "vout": r.vout,
        "txid_url": _url(r.txid),
        "amount_sats": r.sats,
        "confirmations": r.confirmations,
        "state": r.state,
        "final": r.final,
        "spend_txid": r.spend_txid,
        "spend_confirmations": r.spend_confirmations,
        "mint_txid": r.mint_txid,
        "mint_url": _url(r.mint_txid),
        "minted_verified_sats": r.minted_sats,
        "minted_unmatched_sats": r.unmatched_sats,
        "fee_sats": r.fee_sats,
        "change_sats": r.change_sats,
        "detail": r.detail,
        "state_changed_at": r.state_changed_at.isoformat() if r.state_changed_at else None,
    }


@router.get("/status")
async def mint_status(
    merchant: User = Depends(require_api_permission("read_invoice")),
    db: AsyncSession = Depends(get_db),
):
    from app.services.mint_watcher import watcher_state
    xpub = (await db.execute(select(XpubWalletConnection.id).where(
        XpubWalletConnection.merchant_id == merchant.id, XpubWalletConnection.is_active == True))).scalar_one_or_none()
    spark = (await db.execute(select(SparkWalletConnection.id).where(
        SparkWalletConnection.merchant_id == merchant.id, SparkWalletConnection.is_active == True))).scalar_one_or_none()
    rows = (await db.execute(
        select(SparkMintInput.state, func.count(), func.coalesce(func.sum(SparkMintInput.sats), 0),
               func.coalesce(func.sum(SparkMintInput.minted_sats), 0),
               func.coalesce(func.sum(SparkMintInput.fee_sats), 0))
        .where(SparkMintInput.merchant_id == merchant.id)
        .group_by(SparkMintInput.state))).all()
    counts = {s: 0 for s in core.STATES}
    received = minted = fees = 0
    for state, n, s, m, f in rows:
        counts[state] = int(n)
        received += int(s)
        minted += int(m)
        fees += int(f)
    w = watcher_state()
    return {
        "check_enabled": await _check_enabled(db, merchant.id),
        "xpub_connected": xpub is not None,
        "view_key_connected": spark is not None,
        "node_ready": w.get("node_ready"),
        "checked_at": w.get("checked_at"),
        "counts": counts,
        "received_sats": received,
        "minted_verified_sats": minted,
        "fee_sats": fees,
    }


@router.patch("/settings")
async def spark_mint_settings(
    body: SettingsIn,
    merchant: User = Depends(require_api_permission("wallet_connect")),
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(SparkMintSettings, merchant.id)
    if row is None:
        row = SparkMintSettings(merchant_id=merchant.id, check_enabled=body.check_enabled)
        db.add(row)
    else:
        row.check_enabled = body.check_enabled
    await db.commit()
    return {"check_enabled": row.check_enabled}


@router.get("/inputs")
async def mint_coins(
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
    state: str | None = Query(None),
    merchant: User = Depends(require_api_permission("read_invoice")),
    db: AsyncSession = Depends(get_db),
):
    if state is not None and state not in core.STATES:
        raise HTTPException(422, "Unknown state.")
    cond = [SparkMintInput.merchant_id == merchant.id]
    if state:
        cond.append(SparkMintInput.state == state)
    total = (await db.execute(select(func.count()).select_from(SparkMintInput).where(*cond))).scalar_one()
    res = await db.execute(
        select(SparkMintInput, Payment.order_id)
        .join(Payment, Payment.id == SparkMintInput.payment_id)
        .where(*cond)
        .order_by(SparkMintInput.first_seen_at.desc(), SparkMintInput.txid, SparkMintInput.vout)
        .offset(offset).limit(limit))
    return {"total": int(total), "offset": offset, "limit": limit,
            "items": [_item(r, order_id) for r, order_id in res.all()]}


@router.get("/payments/{payment_id}")
async def mint_payment(
    payment_id: str,
    merchant: User = Depends(require_api_permission("read_invoice")),
    db: AsyncSession = Depends(get_db),
):
    p = (await db.execute(select(Payment).where(
        Payment.id == payment_id, Payment.merchant_id == merchant.id))).scalar_one_or_none()
    if p is None:
        raise HTTPException(404, "Payment not found")
    res = await db.execute(select(SparkMintInput).where(SparkMintInput.payment_id == p.id)
                           .order_by(SparkMintInput.first_seen_at, SparkMintInput.txid, SparkMintInput.vout))
    return {
        "transparent": p.transparent_address is not None,
        "check_enabled": await _check_enabled(db, merchant.id),
        "items": [_item(r, p.order_id) for r in res.scalars().all()],
    }
