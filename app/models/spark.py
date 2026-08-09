from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, String
)
from app.models.models import Base, _now, _uuid


class SparkWalletConnection(Base):
    __tablename__ = "spark_wallet_connections"

    id                   = Column(String, primary_key=True, default=_uuid)
    merchant_id          = Column(String, ForeignKey("users.id"), nullable=False, index=True, unique=True)

    view_key_enc          = Column(String(512), nullable=False)

    label                = Column(String(64), nullable=True)
    network              = Column(String(16), default="testnet")  # testnet | mainnet

    next_diversifier     = Column(Integer, default=1, nullable=False)
    last_scanned_coin_id = Column(Integer, default=0, nullable=False)

    connected_at         = Column(DateTime(timezone=True), default=_now)
    last_scanned_at       = Column(DateTime(timezone=True), nullable=True)
    is_active            = Column(Boolean, default=True)


class SparkScanState(Base):
    __tablename__ = "spark_scan_state"

    id                = Column(Integer, primary_key=True, default=1)
    coin_group_id     = Column(Integer, default=0, nullable=False)
    last_block_hash   = Column(String(128), nullable=True)
    updated_at        = Column(DateTime(timezone=True), default=_now)
