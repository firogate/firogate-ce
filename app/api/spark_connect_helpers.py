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
