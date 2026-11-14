"""payment_privacy_inputs and privacy_settings

Revision ID: b7e2c4d91a05
Revises: eaa4693d6b35
Create Date: 2026-11-14

Adds the two tables used by transparent-to-Spark privacy tracking. Both are
new tables, so existing data is untouched. create_tables() also builds them
on boot through Base.metadata.create_all; this revision skips any table that
already exists, so it is safe on databases that booted the new code first.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7e2c4d91a05'
down_revision: Union[str, None] = 'eaa4693d6b35'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has("payment_privacy_inputs"):
        op.create_table(
            "payment_privacy_inputs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("payment_id", sa.String(), sa.ForeignKey("payments.id", ondelete="CASCADE"), nullable=False),
            sa.Column("merchant_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("txid", sa.String(64), nullable=False),
            sa.Column("vout", sa.Integer(), nullable=False),
            sa.Column("address", sa.String(64), nullable=False),
            sa.Column("sats", sa.BigInteger(), nullable=False),
            sa.Column("height", sa.Integer(), nullable=True),
            sa.Column("confirmations", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("credit_found", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("state", sa.String(24), nullable=False, server_default="payment_unconfirmed"),
            sa.Column("final", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("spend_txid", sa.String(64), nullable=True),
            sa.Column("spend_confirmations", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("mint_txid", sa.String(64), nullable=True),
            sa.Column("minted_sats", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("unmatched_sats", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("fee_sats", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("change_sats", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("change_outpoint", sa.String(80), nullable=True),
            sa.Column("change_hops", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("coin_tags", sa.Text(), nullable=True),
            sa.Column("detail", sa.String(200), nullable=True),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("state_changed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("txid", "vout", name="uq_privacy_input_outpoint"),
        )
        op.create_index("ix_payment_privacy_inputs_payment_id", "payment_privacy_inputs", ["payment_id"])
        op.create_index("ix_payment_privacy_inputs_merchant_id", "payment_privacy_inputs", ["merchant_id"])
        op.create_index("ix_payment_privacy_inputs_state", "payment_privacy_inputs", ["state"])
        op.create_index("ix_payment_privacy_inputs_final", "payment_privacy_inputs", ["final"])
        op.create_index("ix_privacy_merchant_state", "payment_privacy_inputs", ["merchant_id", "state"])
    if not _has("privacy_settings"):
        op.create_table(
            "privacy_settings",
            sa.Column("merchant_id", sa.String(), sa.ForeignKey("users.id"), primary_key=True),
            sa.Column("tracking_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    if _has("privacy_settings"):
        op.drop_table("privacy_settings")
    if _has("payment_privacy_inputs"):
        op.drop_table("payment_privacy_inputs")
