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
    admin    = "admin"
    merchant = "merchant"

class PaymentStatus(str, PyEnum):
    pending    = "pending"
    confirming = "confirming"
    confirmed  = "confirmed"
    expired    = "expired"
    failed     = "failed"
    cancelled  = "cancelled"

class WithdrawalType(str, PyEnum):
    transparent = "transparent"
    spark       = "spark"


class WithdrawalStatus(str, PyEnum):

    pending        = "pending"
    queued         = "queued"
    processing     = "processing"

    completed      = "completed"
    failed         = "failed"
    cancelled      = "cancelled"

    manual_review  = "manual_review"
    email_verify_pending = "email_verify_pending"
    approved       = "approved"
    sent           = "sent"
    rejected       = "rejected"
    locked         = "locked"

class PlanName(str, PyEnum):
    free     = "free"
    starter  = "starter"
    pro      = "pro"
    business = "business"


class User(Base):
    __tablename__ = "users"

    id                  = Column(String,  primary_key=True, default=_uuid)
    username            = Column(String(64),  unique=True, nullable=False, index=True)
    email               = Column(String(254), unique=True, nullable=True, index=True)
    hashed_password     = Column(String,  nullable=False)
    role                = Column(Enum(UserRole), default=UserRole.merchant)
    is_active           = Column(Boolean, default=True)

    plan                = Column(Enum(PlanName), default=PlanName.free)
    requests_total      = Column(Integer, default=50)
    requests_used       = Column(Integer, default=0)
    plan_expires_at     = Column(DateTime(timezone=True), nullable=True)

    api_key             = Column(String(64), unique=True, nullable=True, index=True)
    api_key_active      = Column(Boolean, default=True)
    webhook_url         = Column(String(512), nullable=True)
    webhook_secret_enc  = Column(String(512), nullable=True)


    balance_firo        = Column(Float, default=0.0)
    balance_pending     = Column(Float, default=0.0)
    balance_withdrawn   = Column(Float, default=0.0)
    total_earned_firo   = Column(Float, default=0.0)
    total_fees_firo     = Column(Float, default=0.0)

    created_at          = Column(DateTime(timezone=True), default=_now)
    last_login_at       = Column(DateTime(timezone=True), nullable=True)


    totp_secret_enc     = Column(String(512), nullable=True)
    totp_enabled        = Column(Boolean, default=False)
    totp_backup_enc     = Column(Text, nullable=True)


    recovery_codes_enc  = Column(Text, nullable=True)
    full_name           = Column(String(128), nullable=True)


    withdrawal_whitelist_json = Column(Text, nullable=True)

    # ─ Merchant notifications ─
    notify_on_payment   = Column(Boolean, default=True)   # email merchant on confirmed payment
    notify_email        = Column(String(254), nullable=True)  # override email for notifications

    # Trusted addresses: addresses that have had at least one successful withdrawal
    trusted_addresses_json   = Column(Text, nullable=True)


    daily_withdrawal_limit_firo  = Column(Float, default=100.0)
    daily_withdrawal_used_firo   = Column(Float, default=0.0)
    daily_withdrawal_reset_at    = Column(DateTime(timezone=True), nullable=True)
    withdrawal_count_today       = Column(Integer, default=0)
    last_withdrawal_at           = Column(DateTime(timezone=True), nullable=True)
    min_balance_hold_hours       = Column(Integer, default=24)

    # Privacy Mode - True if account created via Tor/onion
    privacy_mode                 = Column(Boolean, default=False)
    created_via_onion            = Column(Boolean, default=False)

    # ─ Firebase auth integration (hybrid) ─
    firebase_uid                 = Column(String(128), unique=True, nullable=True, index=True)
    email_verified               = Column(Boolean, default=False)
    password_changed_at          = Column(DateTime(timezone=True), nullable=True)

    # ─ Merchant branding (shown in checkout + receipt emails) ──
    app_name                     = Column(String(64), nullable=True)
    app_name_locked              = Column(Boolean, default=False, nullable=False)
    app_name_change_allowed      = Column(Boolean, default=False, nullable=False)
    # Brand colors — applied to checkout page (hex values e.g. "#F5C542")
    brand_primary                = Column(String(7), nullable=True)   # buttons, links
    brand_bg                     = Column(String(7), nullable=True)   # page background
    brand_text                   = Column(String(7), nullable=True)   # body text

    # ─ Checkout Theme System ─
    # theme_id: one of the preset keys or "custom"
    theme_id                     = Column(String(32),  nullable=True, default="dark_gold")
    # Custom overrides (all optional — applied on top of preset)
    theme_accent                 = Column(String(7),   nullable=True)   # accent/button color
    theme_bg                     = Column(String(7),   nullable=True)   # background
    theme_surface                = Column(String(7),   nullable=True)   # card/surface color
    theme_text                   = Column(String(7),   nullable=True)   # body text
    theme_radius                 = Column(String(8),   nullable=True)   # border radius (px)
    theme_font                   = Column(String(32),  nullable=True)   # font family key
    theme_button_style           = Column(String(16),  nullable=True)   # "rounded"|"sharp"|"pill"
    theme_checkout_title         = Column(String(80),  nullable=True)   # custom title
    theme_checkout_subtitle      = Column(String(120), nullable=True)   # custom subtitle
    theme_success_msg            = Column(String(200), nullable=True)   # custom success message
    theme_cancel_msg             = Column(String(200), nullable=True)   # custom cancel message

    payments            = relationship("Payment",    back_populates="merchant", cascade="all, delete-orphan", foreign_keys="[Payment.merchant_id]")
    withdrawals         = relationship("Withdrawal", back_populates="merchant", cascade="all, delete-orphan", foreign_keys="[Withdrawal.merchant_id]")
    plan_orders         = relationship("PlanOrder",  back_populates="merchant", cascade="all, delete-orphan", foreign_keys="[PlanOrder.merchant_id]")


class Payment(Base):
    __tablename__ = "payments"

    id                      = Column(String, primary_key=True, default=_uuid)
    merchant_id             = Column(String, ForeignKey("users.id"), nullable=False, index=True)


    receiving_address       = Column(String(256), nullable=False, index=True)
    receiving_address_label = Column(String(128), nullable=True)


    amount_firo             = Column(Float, nullable=False)
    amount_received         = Column(Float, nullable=True)


    platform_fee_pct        = Column(Float, nullable=False, default=1.5)
    platform_fee_firo       = Column(Float, nullable=True)
    merchant_net_firo       = Column(Float, nullable=True)


    txid                    = Column(String(64), nullable=True, index=True)
    vout                    = Column(Integer, nullable=True)
    confirmations           = Column(Integer, default=0)
    required_confirmations  = Column(Integer, default=2)
    block_height            = Column(Integer, nullable=True)

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
    credited_to_balance_at  = Column(DateTime(timezone=True), nullable=True)

    webhook_sent            = Column(Boolean, default=False)
    webhook_sent_at         = Column(DateTime(timezone=True), nullable=True)
    webhook_response        = Column(String(512), nullable=True)
    webhook_attempts        = Column(Integer, default=0)
    webhook_next_retry_at   = Column(DateTime(timezone=True), nullable=True)

    manual_txhash           = Column(String(64), nullable=True)
    manual_check_result     = Column(String(64), nullable=True)
    customer_ip             = Column(String(45), nullable=True)

    merchant                = relationship("User", back_populates="payments")

    __table_args__ = (
        UniqueConstraint("txid", "vout", name="uq_payment_utxo"),
        Index("ix_payment_address", "receiving_address"),
    )


# ─ Payment Links ──
class PaymentLink(Base):
    """Reusable payment links — no API needed. Created from the dashboard."""
    __tablename__ = "payment_links"

    id            = Column(String(36), primary_key=True, default=_uuid)
    merchant_id   = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    slug          = Column(String(32), unique=True, nullable=False, index=True)
    title         = Column(String(128), nullable=False)
    description   = Column(String(512), nullable=True)
    amount_firo   = Column(Float, nullable=True)        # None = customer sets amount
    fixed_amount  = Column(Boolean, default=True)
    collect_email = Column(Boolean, default=True)
    success_url   = Column(String(2048), nullable=True)
    cancel_url    = Column(String(2048), nullable=True)
    is_active     = Column(Boolean, default=True)
    uses_count    = Column(Integer, default=0)
    cancel_count  = Column(Integer, default=0)          # how many buyers cancelled
    max_uses      = Column(Integer, nullable=True)      # None = unlimited
    created_at    = Column(DateTime(timezone=True), default=_now)
    expires_at    = Column(DateTime(timezone=True), nullable=True)
    merchant      = relationship("User")


class Withdrawal(Base):
    __tablename__ = "withdrawals"

    id                  = Column(String, primary_key=True, default=_uuid)
    merchant_id         = Column(String, ForeignKey("users.id"), nullable=False, index=True)

    amount_requested    = Column(Float, nullable=False)
    withdrawal_fee_pct  = Column(Float, nullable=False)
    withdrawal_fee_firo = Column(Float, nullable=False)
    amount_net          = Column(Float, nullable=False)

    destination_address = Column(String(256), nullable=False)

    status              = Column(Enum(WithdrawalStatus), default=WithdrawalStatus.pending, index=True)
    admin_id            = Column(String, ForeignKey("users.id"), nullable=True)
    admin_note          = Column(String(512), nullable=True)
    rejection_reason    = Column(String(512), nullable=True)
    sent_txid           = Column(String(64), nullable=True)


    tier                = Column(String(16), nullable=True)
    risk_score          = Column(Integer, default=0)
    process_after       = Column(DateTime(timezone=True), nullable=True)
    totp_verified       = Column(Boolean, default=False)

    # ─ Email verification code (large-tier withdrawals) ─
    # Hashed alphanumeric 8-char code (never stored in plaintext). Bound to
    # this withdrawal + its merchant_id. Validated in constant time, max
    # 5 attempts, 5-minute lifetime. After 5 bad attempts the withdrawal is
    # locked and a brand-new request is required.
    email_code_hash         = Column(String(128), nullable=True)
    email_code_expires_at   = Column(DateTime(timezone=True), nullable=True)
    email_code_attempts     = Column(Integer, default=0)
    email_code_last_sent_at = Column(DateTime(timezone=True), nullable=True)


    withdrawal_type     = Column(String(16), default="transparent")


    spark_operation_id  = Column(String(128), nullable=True)
    spark_op_status     = Column(String(32), nullable=True)
    spark_op_result     = Column(Text, nullable=True)


    auto_processed      = Column(Boolean, default=False)
    security_checks     = Column(Text, nullable=True)
    processing_error    = Column(String(512), nullable=True)
    attempts            = Column(Integer, default=0)
    ip_address          = Column(String(64), nullable=True)
    balance_locked      = Column(Boolean, default=True)

    created_at          = Column(DateTime(timezone=True), default=_now)
    queued_at           = Column(DateTime(timezone=True), nullable=True)
    approved_at         = Column(DateTime(timezone=True), nullable=True)
    sent_at             = Column(DateTime(timezone=True), nullable=True)
    rejected_at         = Column(DateTime(timezone=True), nullable=True)

    merchant            = relationship("User", back_populates="withdrawals", foreign_keys=[merchant_id])
    admin               = relationship("User", foreign_keys=[admin_id])


# ─ ENTERPRISE ONLY ─
class PlanConfig(Base):
    __tablename__ = "plan_configs"

    id             = Column(String, primary_key=True, default=_uuid)
    plan           = Column(Enum(PlanName), unique=True, nullable=False)
    price_firo     = Column(Float, nullable=False)
    price_usd      = Column(Float, default=0.0)
    requests_quota = Column(Integer, nullable=False)
    duration_days  = Column(Integer, default=30)
    is_active      = Column(Boolean, default=True)
    updated_at     = Column(DateTime(timezone=True), default=_now, onupdate=_now)


class PlanOrder(Base):
    __tablename__ = "plan_orders"

    id               = Column(String, primary_key=True, default=_uuid)
    merchant_id      = Column(String, ForeignKey("users.id"), nullable=False)
    plan             = Column(Enum(PlanName), nullable=False)
    price_firo       = Column(Float, nullable=False)

    receiving_address = Column(String(256), nullable=True)
    txid             = Column(String(64), nullable=True)
    confirmations    = Column(Integer, default=0)
    status           = Column(Enum(PaymentStatus), default=PaymentStatus.pending)

    created_at       = Column(DateTime(timezone=True), default=_now)
    expires_at       = Column(DateTime(timezone=True), nullable=True)
    activated_at     = Column(DateTime(timezone=True), nullable=True)

    merchant         = relationship("User", back_populates="plan_orders")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id          = Column(String, primary_key=True, default=_uuid)
    user_id     = Column(String, nullable=True, index=True)
    action      = Column(String(128), nullable=False, index=True)
    entity_type = Column(String(64),  nullable=True)
    entity_id   = Column(String,      nullable=True, index=True)
    detail      = Column(Text,        nullable=True)
    ip_address  = Column(String(45),  nullable=True)
    created_at  = Column(DateTime(timezone=True), default=_now, index=True)

    __table_args__ = (

        Index("ix_audit_user_time",   "user_id",  "created_at"),

        Index("ix_audit_action_time", "action",   "created_at"),

        Index("ix_audit_entity",      "entity_type", "entity_id"),
    )


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


# Allowed status values (stored as plain string in DB to avoid CHECK-constraint
# migrations when the set changes).
REPORT_STATUSES = {s.value for s in ReportStatus}


class Report(Base):
    """User-submitted or public reports (change app name, tech issue, other)."""
    __tablename__ = "reports"

    id                  = Column(String, primary_key=True, default=_uuid)
    user_id             = Column(String, ForeignKey("users.id"), nullable=True, index=True)
    email               = Column(String(254), nullable=True)     # non-logged reporter
    type                = Column(Enum(ReportType), nullable=False, index=True)
    subject             = Column(String(160), nullable=True)
    message             = Column(Text, nullable=False)
    requested_app_name  = Column(String(64), nullable=True)      # for change_app_name reports
    status              = Column(String(32), default=ReportStatus.pending.value, nullable=False, index=True)
    admin_notes         = Column(Text, nullable=True)
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


class AdminWithdrawalStatus(str, PyEnum):
    pending    = "pending"
    processing = "processing"
    completed  = "completed"
    failed     = "failed"


class AdminWithdrawal(Base):
    # ENTERPRISE ONLY
    __tablename__ = "admin_withdrawals"

    id                  = Column(String, primary_key=True, default=_uuid)
    admin_id            = Column(String, ForeignKey("users.id"), nullable=False, index=True)

    amount_requested    = Column(Float, nullable=False)
    amount_net          = Column(Float, nullable=False)

    destination_address = Column(String(256), nullable=False)
    withdrawal_type     = Column(String(16), default="spark")

    status              = Column(Enum(AdminWithdrawalStatus), default=AdminWithdrawalStatus.pending, index=True)
    sent_txid           = Column(String(64), nullable=True)


    note                = Column(String(512), nullable=True)
    processing_error    = Column(String(512), nullable=True)
    attempts            = Column(Integer, default=0)
    is_auto             = Column(Boolean, default=False)
    security_checks     = Column(Text, nullable=True)


    totp_verified       = Column(Boolean, default=False)

    created_at          = Column(DateTime(timezone=True), default=_now, index=True)
    sent_at             = Column(DateTime(timezone=True), nullable=True)

    admin               = relationship("User", foreign_keys=[admin_id])



# ═══════════════════════════════════════════════════════════════════════════════
# Analytics Aggregation Tables - For fast dashboard queries
# ═══════════════════════════════════════════════════════════════════════════════

class DailyStats(Base):
    """Platform-wide daily aggregated statistics"""
    __tablename__ = "daily_stats"

    id              = Column(String, primary_key=True, default=_uuid)
    date            = Column(String(10), unique=True, nullable=False, index=True)  # YYYY-MM-DD
    total_revenue   = Column(Float, default=0.0)
    transactions_count = Column(Integer, default=0)
    new_users       = Column(Integer, default=0)
    platform_fees   = Column(Float, default=0.0)
    updated_at      = Column(DateTime(timezone=True), default=_now, onupdate=_now)


class UserDailyStats(Base):
    """Per-merchant daily aggregated statistics"""
    __tablename__ = "user_daily_stats"

    id              = Column(String, primary_key=True, default=_uuid)
    user_id         = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    date            = Column(String(10), nullable=False, index=True)  # YYYY-MM-DD
    revenue         = Column(Float, default=0.0)
    orders_count    = Column(Integer, default=0)
    successful_payments = Column(Integer, default=0)
    failed_payments = Column(Integer, default=0)
    updated_at      = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("user_id", "date", name="uq_user_daily_stats"),
        Index("ix_user_daily_stats_user_date", "user_id", "date"),
    )


# ─ Privacy Routing ─
# Single-node Spark routing: all hops executed via the gateway's own Firo node.
# Fresh Spark addresses are generated per chain (getnewsparkaddress).
# No external nodes, no pool wallet management table.

class HopStatus(str, PyEnum):
    pending = "pending"
    done    = "done"
    failed  = "failed"


class RoutingChain(Base):
    """
    One routing chain per automintspark event.
    Expired automatically after ROUTING_CHAIN_EXPIRE_HOURS.
    """
    # ENTERPRISE ONLY
    __tablename__ = "routing_chains"

    id           = Column(String(36), primary_key=True, default=_uuid)
    total_hops   = Column(Integer, nullable=False)
    status       = Column(String(20), default="active")   # active|done|failed
    created_at   = Column(DateTime(timezone=True), default=_now)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    expires_at   = Column(DateTime(timezone=True), nullable=False)

    hops = relationship("RoutingHop", back_populates="chain",
                        order_by="RoutingHop.hop_num",
                        cascade="all, delete-orphan")


class RoutingHop(Base):
    """
    Single hop within a routing chain.
    dest_address — freshly generated Spark address for this hop.
    All hops are executed by the same node (single-node routing).
    """
    # ENTERPRISE ONLY
    __tablename__ = "routing_hops"

    id           = Column(String(36), primary_key=True, default=_uuid)
    chain_id     = Column(String(36), ForeignKey("routing_chains.id",
                           ondelete="CASCADE"), nullable=False, index=True)
    hop_num      = Column(Integer,  nullable=False)
    dest_address = Column(String(200), nullable=False)
    txid         = Column(String(100), nullable=True)
    status       = Column(Enum(HopStatus, name="hop_status"),
                          default=HopStatus.pending, nullable=False)
    locked       = Column(Boolean, default=False, nullable=False)
    attempts     = Column(Integer, default=0)
    error_msg    = Column(String(256), nullable=True)
    run_after    = Column(DateTime(timezone=True), nullable=False)
    created_at   = Column(DateTime(timezone=True), default=_now)
    expires_at   = Column(DateTime(timezone=True), nullable=False)

    chain = relationship("RoutingChain", back_populates="hops")



# ─ API Keys (multi-key per merchant) ─
class ApiKey(Base):
    """
    One row per API key. Merchants can have multiple keys simultaneously.
    Raw key is NEVER stored — only the SHA-256 hash.
    The prefix (fg_live_AbCdXxXx) is stored for display.
    """
    __tablename__ = "api_keys"

    id          = Column(String(36),  primary_key=True, default=_uuid)
    merchant_id = Column(String(36),  nullable=False, index=True)
    name        = Column(String(64),  nullable=False, default="Default")
    prefix      = Column(String(20),  nullable=False)           # fg_live_AbCd… (shown in UI)
    key_hash    = Column(String(64),  nullable=False, unique=True, index=True)  # SHA-256
    status      = Column(String(16),  nullable=False, default="active")
    created_at  = Column(DateTime(timezone=True), nullable=False, default=_now)
    last_used   = Column(DateTime(timezone=True), nullable=True)
    revoked_at  = Column(DateTime(timezone=True), nullable=True)
    scopes      = Column(String(512), nullable=True, default="*")  # future: permission scopes
