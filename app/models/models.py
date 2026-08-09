import uuid
from datetime import datetime, timezone
from enum import Enum as PyEnum
from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey,
    Integer, String, Text, Enum, UniqueConstraint, Index
)
from sqlalchemy.orm import relationship
from app.core.database import Base


def _now():  return datetime.now(timezone.utc)
def _uuid(): return str(uuid.uuid4())


class UserRole(str, PyEnum):
    operator = "operator"
    merchant = "merchant"

class PaymentStatus(str, PyEnum):
    pending    = "pending"
    confirming = "confirming"
    confirmed  = "confirmed"
    expired    = "expired"
    failed     = "failed"
    cancelled  = "cancelled"



class PlanName(str, PyEnum):
    free     = "free"
    starter  = "starter"
    pro      = "pro"
    business = "business"


class PaymentAuditEvent(str, PyEnum):
    payment_created        = "payment_created"
    payment_detected       = "payment_detected"
    payment_confirmed      = "payment_confirmed"
    payment_expired        = "payment_expired"
    payment_cancelled      = "payment_cancelled"
    merchant_stats_updated = "merchant_stats_updated"
    stats_mismatch_detected = "stats_mismatch_detected"
    stats_verified         = "stats_verified"


class User(Base):
    __tablename__ = "users"

    id                  = Column(String,  primary_key=True, default=_uuid)
    username            = Column(String(64),  unique=True, nullable=False, index=True)
    email               = Column(String(254), unique=True, nullable=True, index=True)
    hashed_password     = Column(String,  nullable=True)
    role                = Column(Enum(UserRole), default=UserRole.merchant)
    is_active           = Column(Boolean, default=True)
    blocked_reason      = Column(Text, nullable=True)
    blocked_at          = Column(DateTime(timezone=True), nullable=True)
    blocked_by          = Column(String(36), nullable=True)
    spark_connect_enabled = Column(Boolean, nullable=True, default=None)
    # Merchant Setup checklist (replaces the old operator-approval request
    # flow). Unlocked by default for both existing and new accounts (see
    # database.py migration + auth.py /register); operators can still lock
    # individual accounts via the panel. Always subject to the global
    # Wallet Connection Access switch (EmergencyControl key
    # "disable_wallet_connections").
    merchant_setup_unlocked = Column(Boolean, default=True, nullable=False)
    setup_learned_basics    = Column(Boolean, default=False, nullable=False)
    has_seen_onboarding     = Column(Boolean, default=False, nullable=False)

    plan                = Column(Enum(PlanName), default=PlanName.free)
    requests_total      = Column(Integer, default=50)
    requests_used       = Column(Integer, default=0)
    plan_expires_at     = Column(DateTime(timezone=True), nullable=True)
    rollover_requests   = Column(Integer, default=0)
    rollover_expires_at = Column(DateTime(timezone=True), nullable=True)
    cycle_start_at      = Column(DateTime(timezone=True), nullable=True)

    api_key             = Column(String(64), unique=True, nullable=True, index=True)
    api_key_active      = Column(Boolean, default=True)
    webhook_url         = Column(String(512), nullable=True)
    webhook_secret_enc  = Column(String(512), nullable=True)

    # Per-merchant payment policy. NULL means "use the instance default" —
    # see app/core/payment_policy.py for resolution order.
    required_confirmations_policy = Column(Integer, nullable=True)
    payment_tolerance_firo        = Column(Float, nullable=True)

    # Merchant business metrics (reporting only, not balances or held funds).
    lifetime_gross_sales_firo   = Column(Float, default=0.0)
    lifetime_received_firo      = Column(Float, default=0.0)
    lifetime_confirmed_payments = Column(Integer, default=0)
    lifetime_completed_orders   = Column(Integer, default=0)

    created_at          = Column(DateTime(timezone=True), default=_now)
    last_login_at       = Column(DateTime(timezone=True), nullable=True)
    # Overwritten on every successful login; kept NULL for privacy-mode/Tor users.
    last_login_ip       = Column(String(64),  nullable=True)
    last_login_device   = Column(String(256), nullable=True)

    totp_secret_enc     = Column(String(512), nullable=True)
    totp_enabled        = Column(Boolean, default=False)
    totp_backup_enc     = Column(Text, nullable=True)
    # Last consumed TOTP time-step (30s window index) blocks replay of an
    # already-used code within the valid_window, even if intercepted/leaked.
    totp_last_step      = Column(Integer, nullable=True)

    recovery_codes_enc  = Column(Text, nullable=True)
    full_name           = Column(String(128), nullable=True)

    notify_on_payment   = Column(Boolean, default=True)
    notify_email        = Column(String(254), nullable=True)
    # Telegram notification channel chat id linked via the bot's /start
    # deep link; may exist for users who did NOT register with Telegram.
    telegram_chat_id    = Column(String(32), nullable=True)
    notify_telegram     = Column(Boolean, default=False)

    trusted_addresses_json   = Column(Text, nullable=True)

    privacy_mode                 = Column(Boolean, default=False)
    created_via_onion            = Column(Boolean, default=False)
    show_market_price            = Column(Boolean, default=False, nullable=False)

    firebase_uid                 = Column(String(128), unique=True, nullable=True, index=True)
    telegram_id                  = Column(String(32),  unique=True, nullable=True, index=True)
    email_verified               = Column(Boolean, default=False)
    password_changed_at          = Column(DateTime(timezone=True), nullable=True)

    wallet_address                = Column(String(128), unique=True, nullable=True, index=True)
    auth_method                   = Column(String(16), default="password")
    account_number_hash           = Column(String(128), nullable=True)
    account_number_lookup         = Column(String(64), unique=True, nullable=True, index=True)
    account_number_enc            = Column(String(512), nullable=True)

    app_name                     = Column(String(64), nullable=True)
    app_name_locked              = Column(Boolean, default=False, nullable=False)
    app_name_change_allowed      = Column(Boolean, default=False, nullable=False)
    app_name_change_count        = Column(Integer, default=0, nullable=False)
    app_name_last_changed_at     = Column(DateTime, nullable=True)
    app_logo                     = Column(Text, nullable=True)
    brand_primary                = Column(String(7), nullable=True)
    brand_bg                     = Column(String(7), nullable=True)
    brand_text                   = Column(String(7), nullable=True)

    checkout_layout              = Column(String(16),  nullable=True, default="stripe")
    theme_id                     = Column(String(32),  nullable=True, default="dark_gold")
    theme_accent                 = Column(String(7),   nullable=True)
    theme_bg                     = Column(String(7),   nullable=True)
    theme_surface                = Column(String(7),   nullable=True)
    theme_text                   = Column(String(7),   nullable=True)
    theme_radius                 = Column(String(8),   nullable=True)
    theme_font                   = Column(String(32),  nullable=True)
    theme_button_style           = Column(String(16),  nullable=True)
    theme_checkout_title         = Column(String(80),  nullable=True)
    theme_checkout_subtitle      = Column(String(120), nullable=True)
    theme_success_msg            = Column(String(200), nullable=True)
    theme_cancel_msg             = Column(String(200), nullable=True)
    theme_qr_position            = Column(String(8),   nullable=True)
    theme_cancel_position        = Column(String(8),   nullable=True)
    theme_bg_image               = Column(Text,        nullable=True)
    theme_bg_overlay             = Column(String(4),   nullable=True)
    theme_v2_colors_json         = Column(Text,        nullable=True)

    payments            = relationship("Payment",    back_populates="merchant", cascade="all, delete-orphan", foreign_keys="[Payment.merchant_id]")


class Payment(Base):
    __tablename__ = "payments"

    id                      = Column(String, primary_key=True, default=_uuid)
    merchant_id             = Column(String, ForeignKey("users.id"), nullable=False, index=True)

    receiving_address       = Column(String(256), nullable=False, index=True)
    receiving_address_label = Column(String(128), nullable=True)

    amount_firo             = Column(Float, nullable=False)
    amount_received         = Column(Float, nullable=True)

    txid                    = Column(String(64), nullable=True, index=True)
    vout                    = Column(Integer, nullable=True)
    confirmations           = Column(Integer, default=0)
    required_confirmations  = Column(Integer, default=2)
    block_height            = Column(Integer, nullable=True)
    # Chain tip at the moment this invoice claimed its receiving address —
    # the floor a confirming transaction's own block height must meet.
    # Prevents a transaction that predates the invoice (e.g. historical
    # activity on a reused Spark diversifier after a DB wipe) from ever
    # confirming it, independent of the scanner's own group/height cursor.
    start_block_height      = Column(Integer, nullable=True)

    status                  = Column(Enum(PaymentStatus), default=PaymentStatus.pending, index=True)

    order_id                = Column(String(256), nullable=True)
    order_description       = Column(String(512), nullable=True)
    customer_email          = Column(String(254), nullable=True)
    metadata_json           = Column(Text, nullable=True)

    success_url             = Column(String(2048), nullable=True)
    cancel_url              = Column(String(2048), nullable=True)
    collect_email           = Column(Boolean, default=True)
    email_collected_at      = Column(DateTime(timezone=True), nullable=True)

    extend_count            = Column(Integer, default=0)
    created_at              = Column(DateTime(timezone=True), default=_now)
    expires_at              = Column(DateTime(timezone=True), nullable=True)
    confirmed_at            = Column(DateTime(timezone=True), nullable=True)

    webhook_sent            = Column(Boolean, default=False)
    webhook_sent_at         = Column(DateTime(timezone=True), nullable=True)
    webhook_response        = Column(String(512), nullable=True)
    webhook_attempts        = Column(Integer, default=0)
    webhook_next_retry_at   = Column(DateTime(timezone=True), nullable=True)

    manual_txhash           = Column(String(64), nullable=True)
    manual_check_result     = Column(String(64), nullable=True)
    customer_ip             = Column(String(45), nullable=True)

    # Always "spark" now (see app/models/spark.py). receiving_address is
    # derived offline from the merchant's view key plus spark_diversifier;
    # spark_coin_tag is the matched coin's lTagHash, set once a payment is
    # detected (prevents double-crediting the same coin). Column kept for
    # historical rows predating the Spark-only migration.
    address_type            = Column(String(16), default="spark", nullable=False)
    spark_diversifier       = Column(Integer, nullable=True)
    spark_coin_tag          = Column(String(128), nullable=True)
    spark_owner_id          = Column(String, nullable=True)
    spark_coin_tags_json    = Column(Text, nullable=True)

    merchant                = relationship("User", back_populates="payments")

    __table_args__ = (
        UniqueConstraint("txid", "vout", name="uq_payment_utxo"),
        Index("ix_payment_address", "receiving_address"),
    )


class PaymentAuditLog(Base):
    """Immutable audit trail for all payment lifecycle events.
    Rows are INSERT-only never modified after creation."""
    __tablename__ = "payment_audit_logs"

    id          = Column(String(36), primary_key=True, default=_uuid)
    payment_id  = Column(String(36), ForeignKey("payments.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    merchant_id = Column(String(36), nullable=False, index=True)
    event       = Column(Enum(PaymentAuditEvent), nullable=False, index=True)
    amount_firo = Column(Float, nullable=True)
    amount_received = Column(Float, nullable=True)
    txid        = Column(String(64), nullable=True)
    confirmations = Column(Integer, nullable=True)
    detail      = Column(Text, nullable=True)
    created_at  = Column(DateTime(timezone=True), nullable=False, default=_now, index=True)

    __table_args__ = (
        Index("ix_pal_payment_event",   "payment_id",  "event"),
        Index("ix_pal_merchant_time",   "merchant_id", "created_at"),
    )


class SparkCoinCredit(Base):
    """Global record of every Spark coin ever credited to a Payment. The
    unique constraint on coin_tag is the real cross-payment, cross-scan-run
    dedup guarantee — Payment.spark_coin_tag only ever holds the most
    recently credited coin (overwritten on each multi-coin payment), so it
    cannot by itself stop the same coin from being credited to a second,
    unrelated Payment (e.g. after a DB wipe reuses the same diversifier)."""
    __tablename__ = "spark_coin_credits"

    id          = Column(String, primary_key=True, default=_uuid)
    coin_tag    = Column(String(128), nullable=False, unique=True, index=True)
    txid        = Column(String(64), nullable=False, index=True)
    payment_id  = Column(String, ForeignKey("payments.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at  = Column(DateTime(timezone=True), nullable=False, default=_now)


class MerchantStatsCheck(Base):
    """Records periodic accounting verification results.
    Compares in-DB merchant lifetime stats against summed payment records."""
    __tablename__ = "merchant_stats_checks"

    id                         = Column(String(36), primary_key=True, default=_uuid)
    merchant_id                = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"),
                                        nullable=False, index=True)
    checked_at                 = Column(DateTime(timezone=True), nullable=False, default=_now)

    db_gross_sales             = Column(Float, nullable=False)
    db_received                = Column(Float, nullable=False)
    db_confirmed_payments      = Column(Integer, nullable=False)
    db_completed_orders        = Column(Integer, nullable=False)

    actual_gross_sales         = Column(Float, nullable=False)
    actual_received            = Column(Float, nullable=False)
    actual_confirmed_payments  = Column(Integer, nullable=False)
    actual_completed_orders    = Column(Integer, nullable=False)

    has_mismatch               = Column(Boolean, nullable=False, default=False)
    mismatch_detail            = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_msc_merchant_time", "merchant_id", "checked_at"),
    )


class PaymentLink(Base):
    """Reusable payment links, no API needed. Created from the dashboard."""
    __tablename__ = "payment_links"

    id            = Column(String(36), primary_key=True, default=_uuid)
    merchant_id   = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    slug          = Column(String(32), unique=True, nullable=False, index=True)
    title         = Column(String(128), nullable=False)
    description   = Column(String(512), nullable=True)
    amount_firo   = Column(Float, nullable=True)
    fixed_amount  = Column(Boolean, default=True)
    collect_email = Column(Boolean, default=True)
    success_url   = Column(String(2048), nullable=True)
    cancel_url    = Column(String(2048), nullable=True)
    is_active     = Column(Boolean, default=True)
    uses_count    = Column(Integer, default=0)
    cancel_count  = Column(Integer, default=0)
    max_uses      = Column(Integer, nullable=True)
    created_at    = Column(DateTime(timezone=True), default=_now)
    expires_at    = Column(DateTime(timezone=True), nullable=True)
    merchant      = relationship("User")



class LoginAttempt(Base):
    __tablename__ = "login_attempts"

    id         = Column(String, primary_key=True, default=_uuid)
    username   = Column(String(64), nullable=True, index=True)
    ip_address = Column(String(45), nullable=False, index=True)
    success    = Column(Boolean, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now, index=True)

    __table_args__ = (
        Index("ix_login_ip_time",   "ip_address", "created_at"),
        Index("ix_login_user_time", "username",   "created_at"),
    )


class AuthActionPurpose(str, PyEnum):
    verify_email    = "verify_email"
    reset_password  = "reset_password"


class AuthActionToken(Base):
    """Single-use, expiring token for email verification & password reset."""
    __tablename__ = "auth_action_tokens"

    id          = Column(String, primary_key=True, default=_uuid)
    user_id     = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    token_hash  = Column(String(128), unique=True, nullable=False, index=True)
    purpose     = Column(Enum(AuthActionPurpose), nullable=False, index=True)
    expires_at  = Column(DateTime(timezone=True), nullable=False)
    used        = Column(Boolean, default=False, nullable=False)
    used_at     = Column(DateTime(timezone=True), nullable=True)
    created_at  = Column(DateTime(timezone=True), default=_now, index=True)

    __table_args__ = (
        Index("ix_auth_action_user_purpose", "user_id", "purpose"),
    )


class ReportType(str, PyEnum):
    change_app_name = "change_app_name"
    technical       = "technical"
    other           = "other"


class ReportStatus(str, PyEnum):
    pending     = "pending"
    in_progress = "in_progress"
    resolved    = "resolved"
    dismissed   = "dismissed"


REPORT_STATUSES = {s.value for s in ReportStatus}


class Report(Base):
    """User-submitted or public reports (change app name, tech issue, other)."""
    __tablename__ = "reports"

    id                  = Column(String, primary_key=True, default=_uuid)
    user_id             = Column(String, ForeignKey("users.id"), nullable=True, index=True)
    email               = Column(String(254), nullable=True)
    type                = Column(Enum(ReportType), nullable=False, index=True)
    subject             = Column(String(160), nullable=True)
    message             = Column(Text, nullable=False)
    requested_app_name  = Column(String(64), nullable=True)
    status              = Column(String(32), default=ReportStatus.pending.value, nullable=False, index=True)
    operator_notes         = Column(Text, nullable=True)
    ip_address          = Column(String(45), nullable=True)
    user_agent          = Column(String(300), nullable=True)
    created_at          = Column(DateTime(timezone=True), default=_now, index=True)
    reviewed_at         = Column(DateTime(timezone=True), nullable=True)
    reviewed_by         = Column(String, ForeignKey("users.id"), nullable=True)
    notified_at         = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_reports_status_time", "status", "created_at"),
        Index("ix_reports_type_status", "type",   "status"),
    )



class SystemConfig(Base):
    __tablename__ = "system_configs"

    key        = Column(String(128), primary_key=True)
    value      = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)




class DailyStats(Base):
    """Platform-wide daily aggregated statistics"""
    __tablename__ = "daily_stats"

    id                 = Column(String, primary_key=True, default=_uuid)
    date               = Column(String(10), unique=True, nullable=False, index=True)
    total_volume_firo  = Column(Float, default=0.0)
    transactions_count = Column(Integer, default=0)
    new_users          = Column(Integer, default=0)
    updated_at         = Column(DateTime(timezone=True), default=_now, onupdate=_now)


class UserDailyStats(Base):
    """Per-merchant daily aggregated statistics"""
    __tablename__ = "user_daily_stats"

    id                  = Column(String, primary_key=True, default=_uuid)
    user_id             = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    date                = Column(String(10), nullable=False, index=True)
    gross_sales_firo    = Column(Float, default=0.0)
    received_firo       = Column(Float, default=0.0)
    orders_count        = Column(Integer, default=0)
    successful_payments = Column(Integer, default=0)
    failed_payments     = Column(Integer, default=0)
    updated_at          = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("user_id", "date", name="uq_user_daily_stats"),
        Index("ix_user_daily_stats_user_date", "user_id", "date"),
    )


class ApiKey(Base):
    """One row per API key. Raw key is NEVER stored, only the SHA-256 hash."""
    __tablename__ = "api_keys"

    id          = Column(String(36),  primary_key=True, default=_uuid)
    merchant_id = Column(String(36),  nullable=False, index=True)
    name        = Column(String(64),  nullable=False, default="Default")
    prefix      = Column(String(20),  nullable=False)
    key_hash    = Column(String(64),  nullable=False, unique=True, index=True)
    status      = Column(String(16),  nullable=False, default="active")
    created_at  = Column(DateTime(timezone=True), nullable=False, default=_now)
    last_used   = Column(DateTime(timezone=True), nullable=True)
    revoked_at  = Column(DateTime(timezone=True), nullable=True)
    scopes      = Column(String(512), nullable=True, default="*")


