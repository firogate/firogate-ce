"""
app/api/spark_connect_helpers.py

A Spark checkout address is derived offline from the merchant's view key
plus the next unused diversifier, so "getting an address" is just
claiming the next diversifier, atomically.
"""
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Payment
from app.models.spark import SparkWalletConnection
from app.services.sparkmobile import SparkViewKeyError


async def get_next_spark_address(db: AsyncSession, merchant_id: str, payment: Payment) -> str:
    # Fetched before any DB write below so this network round-trip (cache
    # miss can take seconds against a slow/unreachable node) never happens
    # while a SQLite write transaction is open - doing it after db.flush()
    # held the write lock long enough to blow through the busy timeout and
    # starve unrelated writers (e.g. the Spark scanner's own state update).
    from app.services.firo_rpc import get_rpc, FiroRPCError
    try:
        start_block_height = await get_rpc().get_block_count_cached()
    except FiroRPCError:
        start_block_height = None

    stmt = select(SparkWalletConnection).where(
        SparkWalletConnection.merchant_id == merchant_id,
        SparkWalletConnection.is_active == True,
    )
    try:
        stmt = stmt.with_for_update()
    except Exception:
        pass  # SQLite (dev) ignores row locks; Postgres (prod) enforces them
    res = await db.execute(stmt)
    conn = res.scalar_one_or_none()
    if conn is None:
        raise HTTPException(409, "This store isn't set up to accept payments yet.")

    diversifier = conn.next_diversifier
    conn.next_diversifier = diversifier + 1

    from app.services.payment_engine import _get_view_key
    vk = _get_view_key(conn)
    if vk is None:
        raise HTTPException(500, "Payment setup error. Please contact the store.")

    try:
        address = vk.get_address(diversifier=diversifier)
    except SparkViewKeyError:
        raise HTTPException(500, "Payment setup error. Please contact the store.")

    payment.spark_diversifier = diversifier
    payment.spark_owner_id = merchant_id

    # Anchor this invoice to the current chain tip so the scanner can never
    # confirm it with a transaction that predates its creation (e.g. old
    # activity on a reused diversifier after a DB wipe). None (RPC failure)
    # is a defensive fallback, never treated as "no floor" by the scanner.
    payment.start_block_height = start_block_height

    await db.flush()
    return address


async def _active_methods(db: AsyncSession, merchant_id: str):
    from app.models.models import User, XpubWalletConnection
    merchant = (await db.execute(select(User).where(User.id == merchant_id))).scalar_one_or_none()
    spark = (await db.execute(select(SparkWalletConnection.id).where(
        SparkWalletConnection.merchant_id == merchant_id, SparkWalletConnection.is_active == True).limit(1))).scalar_one_or_none()
    xconn = (await db.execute(select(XpubWalletConnection).where(
        XpubWalletConnection.merchant_id == merchant_id, XpubWalletConnection.is_active == True))).scalar_one_or_none()
    spark_on = bool(spark) and (merchant is None or merchant.pay_spark_enabled is not False)
    trans_on = xconn is not None and (merchant is None or merchant.pay_transparent_enabled is not False)
    default = (merchant.pay_default_method if merchant else None) or "spark"
    return spark_on, trans_on, xconn, default


async def merchant_can_receive(db: AsyncSession, merchant_id: str) -> bool:
    spark_on, trans_on, _, _ = await _active_methods(db, merchant_id)
    return spark_on or trans_on


async def _watch_address(address: str) -> None:
    from app.services.firo_rpc import get_rpc
    try:
        await get_rpc().call("importaddress", address, "firogate", False)
    except Exception:
        pass


async def _next_transparent_address(db: AsyncSession, xconn, payment: Payment) -> str:
    from app.core.security import decrypt_field
    from app.core.xpub import Xpub
    try:
        key = Xpub.parse(decrypt_field(xconn.xpub_enc))
    except Exception:
        raise HTTPException(500, "Payment setup error. Please contact the store.")
    index = xconn.next_index or 0
    xconn.next_index = index + 1
    address = key.address_at(index)
    payment.derivation_index = index
    payment.transparent_address = address
    import asyncio
    asyncio.create_task(_watch_address(address))
    return address


async def get_next_receive_address(db: AsyncSession, merchant_id: str, payment: Payment) -> str:
    spark_on, trans_on, xconn, default = await _active_methods(db, merchant_id)
    if not spark_on and not trans_on:
        raise HTTPException(409, "This store isn't set up to accept payments yet.")
    spark_address = None
    if spark_on:
        spark_address = await get_next_spark_address(db, merchant_id, payment)
        payment.spark_address = spark_address
    else:
        from app.services.firo_rpc import get_rpc, FiroRPCError
        try:
            payment.start_block_height = await get_rpc().get_block_count_cached()
        except FiroRPCError:
            payment.start_block_height = None
    transparent_address = None
    if trans_on:
        transparent_address = await _next_transparent_address(db, xconn, payment)
    if transparent_address and (default == "transparent" or not spark_address):
        payment.address_type = "transparent"
        address = transparent_address
    else:
        payment.address_type = "spark"
        address = spark_address
    await db.flush()
    return address


def payment_methods(p: Payment) -> list[dict]:
    out = []
    spark = p.spark_address or (p.receiving_address if (p.address_type or "spark") == "spark" and p.spark_diversifier is not None else None)
    if spark:
        out.append({"method": "spark", "address": spark})
    trans = p.transparent_address or (p.receiving_address if p.address_type == "transparent" else None)
    if trans:
        out.append({"method": "transparent", "address": trans})
    return out
