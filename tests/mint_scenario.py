from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import delete, select

from app.core.database import AsyncSessionLocal, create_tables
from app.core.security import encrypt_field
from app.models.models import Payment, PaymentStatus, User, XpubWalletConnection
from app.models.spark_mint import SparkMintInput
from app.models.spark import SparkWalletConnection

FIELDS = ("state", "final", "credit_found", "minted_sats", "unmatched_sats", "fee_sats", "change_sats",
          "spend_txid", "mint_txid", "change_hops", "confirmations")


async def reset_db() -> None:
    await create_tables()
    async with AsyncSessionLocal() as db:
        for model in (SparkMintInput, Payment, XpubWalletConnection, SparkWalletConnection, User):
            await db.execute(delete(model))
        await db.commit()


async def setup_merchant(tpub: str, view_key_hex: str, payments: list[dict]) -> str:
    async with AsyncSessionLocal() as db:
        user = User(username="mint-test", email="mint-test@example.invalid")
        db.add(user)
        await db.flush()
        db.add(XpubWalletConnection(merchant_id=user.id, xpub_enc=encrypt_field(tpub), network="testnet",
                                    next_index=len(payments)))
        db.add(SparkWalletConnection(merchant_id=user.id, view_key_enc=encrypt_field(view_key_hex), network="testnet"))
        for p in payments:
            db.add(Payment(
                id=p["id"], merchant_id=user.id, receiving_address=p["address"], amount_firo=p["amount"],
                order_id=p["label"], status=PaymentStatus.pending, required_confirmations=1,
                derivation_index=p["index"], transparent_address=p["address"], address_type="transparent",
                start_block_height=p["start_height"],
                created_at=datetime.fromisoformat(p["created_at"]),
            ))
        await db.commit()
        return user.id


async def set_payment_progress(progress: dict) -> None:
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(Payment))
        for p in res.scalars().all():
            g = progress.get(p.order_id)
            if g:
                p.status = PaymentStatus(g["status"])
                p.transparent_received = g["transparent_received"]
        await db.commit()


async def payment_progress() -> dict:
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(Payment))
        return {p.order_id: {"status": p.status.value, "transparent_received": p.transparent_received or 0.0}
                for p in res.scalars().all()}


async def snapshot() -> dict:
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(SparkMintInput, Payment.order_id)
                               .join(Payment, Payment.id == SparkMintInput.payment_id))
        out: dict[str, list[dict]] = {}
        for row, label in res.all():
            item = {"txid": row.txid, "vout": row.vout, "sats": row.sats}
            item.update({f: getattr(row, f) for f in FIELDS})
            out.setdefault(label, []).append(item)
        for v in out.values():
            v.sort(key=lambda i: (i["txid"], i["vout"]))
        return json.loads(json.dumps(out))


async def drop_rows() -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(SparkMintInput))
        await db.commit()


def states(snap: dict) -> dict:
    return {label: [r["state"] for r in rows] for label, rows in sorted(snap.items())}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
