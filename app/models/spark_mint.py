"""
Transparent-to-Spark privacy tracking.

One row per transparent coin (txid:vout) received at an invoice address.
Rows only describe what happened to the coin after it arrived. They never
change the payment's status, amount or confirmations, and FiroGate never
holds a key that could move the coin.
"""
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)

from app.models.models import Base, _now, _uuid


class PaymentPrivacyInput(Base):
    __tablename__ = "payment_privacy_inputs"

    id                  = Column(String(36), primary_key=True, default=_uuid)
    payment_id          = Column(String, ForeignKey("payments.id", ondelete="CASCADE"), nullable=False, index=True)
    merchant_id         = Column(String, ForeignKey("users.id"), nullable=False, index=True)

    txid                = Column(String(64), nullable=False)
    vout                = Column(Integer, nullable=False)
    address             = Column(String(64), nullable=False)
    sats                = Column(BigInteger, nullable=False)
    height              = Column(Integer, nullable=True)
    confirmations       = Column(Integer, nullable=False, default=0)
    credit_found        = Column(Boolean, nullable=False, default=True)

    state               = Column(String(24), nullable=False, default="payment_unconfirmed", index=True)
    final               = Column(Boolean, nullable=False, default=False, index=True)

    spend_txid          = Column(String(64), nullable=True)
    spend_confirmations = Column(Integer, nullable=False, default=0)
    mint_txid           = Column(String(64), nullable=True)
    minted_sats         = Column(BigInteger, nullable=False, default=0)
    unmatched_sats      = Column(BigInteger, nullable=False, default=0)
    fee_sats            = Column(BigInteger, nullable=False, default=0)
    change_sats         = Column(BigInteger, nullable=False, default=0)
    change_outpoint     = Column(String(80), nullable=True)
    change_hops         = Column(Integer, nullable=False, default=0)
    coin_tags           = Column(Text, nullable=True)
    detail              = Column(String(200), nullable=True)

    first_seen_at       = Column(DateTime(timezone=True), default=_now)
    state_changed_at    = Column(DateTime(timezone=True), default=_now)
    checked_at          = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("txid", "vout", name="uq_privacy_input_outpoint"),
        Index("ix_privacy_merchant_state", "merchant_id", "state"),
    )


class PrivacySettings(Base):
    __tablename__ = "privacy_settings"

    merchant_id      = Column(String, ForeignKey("users.id"), primary_key=True)
    tracking_enabled = Column(Boolean, nullable=False, default=True)
    updated_at       = Column(DateTime(timezone=True), default=_now, onupdate=_now)
