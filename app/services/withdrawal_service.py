"""
app/services/withdrawal_service.py

Production-ready withdrawal system with Spark (z-address) support.

Architecture:
  ┌─┐
  │  DB is the SINGLE source of truth for user balances.    │
  │  UTXO is NEVER used for balance checks.                 │
  │  Balance is locked in DB immediately on request.        │
  └─┘

Two withdrawal paths:
  🔵 Transparent (t-address) → sendtoaddress → txid → completed
  🟣 Spark (z/sp1 address)   → z_sendmany   → op_id → poll → completed

Flow:
  1. Request arrives → pre-flight DB checks → balance locked
  2. Worker picks up after delay
  3. Transparent: RPC send → txid → completed
  4. Spark: RPC z_sendmany → op_id stored → async poll → completed
  5. Temp errors → retry with backoff (max 3 attempts)
  6. Hard errors → refund balance → failed
  7. Large amounts → under_review (no auto-send)
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Tuple
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import (
    User, Withdrawal, WithdrawalStatus,
    Payment, PaymentStatus, AuditLog,
)
from app.core.config import get_settings
from app.services.firo_rpc import get_rpc, FiroRPCError

settings = get_settings()

MAX_RETRIES    = 3
RETRY_DELAYS   = [60, 120, 180]   # seconds: attempt 1, 2, 3


# ─ Timezone helper ─

def _tz(dt):
    """Ensure timezone-aware UTC. SQLite returns naive datetimes."""
    if dt is None: return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ─ Error types ─

class WithdrawalTempError(Exception):
    """Temporary — safe to retry (node offline, low balance, etc.)."""

class WithdrawalHardError(Exception):
    """Permanent — do NOT retry (bad address, daily limit, etc.)."""


class WithdrawalSoftError(Exception):
    """
    Request-is-valid-but-needs-user-action (e.g. 2FA not enabled yet). The
    API layer maps specific string codes to structured HTTP responses.
    """


# ─ Tier classification ──

def classify_tier(amount: float, risk_score: int) -> str:
    """
    Tier selection:
      - "email"  : large withdrawals → email-code verification (≤5 min).
      - "soft"   : medium withdrawals → TOTP (2FA) required.
      - "auto"   : small withdrawals → no extra step.
    High risk (>=80) always requires email verification.
    """
    if amount > settings.SOFT_TIER_MAX_FIRO:
        return "email"
    if risk_score >= 80:
        return "email"
    if amount > settings.AUTO_TIER_MAX_FIRO:
        return "soft"
    return "auto"


# ─ Risk scoring ─

async def calculate_risk_score(merchant: User, amount: float, db: AsyncSession) -> int:
    score = 0
    if not merchant.totp_enabled:
        score += 20
    daily_limit = merchant.daily_withdrawal_limit_firo or settings.MAX_DAILY_WITHDRAWAL_FIRO
    pct = amount / daily_limit if daily_limit > 0 else 1.0
    if pct > 0.9:
        score += 25
    elif pct > 0.7:
        score += 10
    count = merchant.withdrawal_count_today or 0
    if count >= 3:
        score += 30
    elif count >= 2:
        score += 15
    if merchant.last_withdrawal_at:
        last = _tz(merchant.last_withdrawal_at)
        diff_min = (datetime.now(timezone.utc) - last).total_seconds() / 60
        if diff_min < settings.WITHDRAWAL_COOLDOWN_MIN:
            score += 25
    return min(score, 100)


# ─ Pre-flight validations ─

async def _check_daily_limit(merchant: User, amount: float, db: AsyncSession):
    now = datetime.now(timezone.utc)
    reset = _tz(merchant.daily_withdrawal_reset_at)
    if not reset or reset < now:
        merchant.daily_withdrawal_used_firo = 0.0
        merchant.withdrawal_count_today = 0
        merchant.daily_withdrawal_reset_at = now + timedelta(days=1)
        db.add(merchant)
        await db.flush()
    daily_limit = merchant.daily_withdrawal_limit_firo or settings.MAX_DAILY_WITHDRAWAL_FIRO
    used = merchant.daily_withdrawal_used_firo or 0.0
    remaining = daily_limit - used
    if amount > remaining:
        raise WithdrawalHardError(
            f"Daily limit exceeded — {remaining:.4f} FIRO remaining (limit: {daily_limit:.2f})"
        )


async def _check_velocity(merchant: User):
    max_count = settings.MAX_WITHDRAWALS_PER_DAY
    if (merchant.withdrawal_count_today or 0) >= max_count:
        raise WithdrawalHardError(f"Max {max_count} withdrawals/day reached")


async def _check_cooldown(merchant: User):
    if not merchant.last_withdrawal_at:
        return
    elapsed_min = (
        datetime.now(timezone.utc) - _tz(merchant.last_withdrawal_at)
    ).total_seconds() / 60
    if elapsed_min < settings.WITHDRAWAL_COOLDOWN_MIN:
        wait = int(settings.WITHDRAWAL_COOLDOWN_MIN - elapsed_min) + 1
        raise WithdrawalHardError(f"Please wait {wait} more minute(s) between withdrawals")


async def _check_db_balance(merchant: User, amount: float):
    """
    Verify DB balance is sufficient.
    DB is the SINGLE source of truth — never check UTXO.
    """
    available = merchant.balance_firo or 0.0
    if available < amount:
        raise WithdrawalHardError(
            f"Insufficient balance — DB shows {available:.8f} FIRO, "
            f"requested {amount:.8f} FIRO"
        )


async def _check_no_active_withdrawal(merchant: User, db: AsyncSession):
    """Prevent concurrent withdrawals — one at a time per user.
    Uses .limit(1).scalars().first() to safely handle multiple rows."""
    res = await db.execute(
        select(Withdrawal).where(
            Withdrawal.merchant_id == merchant.id,
            Withdrawal.status.in_([
                WithdrawalStatus.pending,
                WithdrawalStatus.processing,
                WithdrawalStatus.approved,
                WithdrawalStatus.queued,
            ]),
        ).limit(1)
    )
    if res.scalars().first():
        raise WithdrawalHardError(
            "You already have a withdrawal in progress. "
            "Wait for it to complete before requesting another."
        )


async def _check_hold_period(merchant: User, amount: float, db: AsyncSession):
    """
    Verify funds have been held long enough before withdrawal.
    DB balance = source of truth.

    Logic:
    - Look at confirmed payments with credited_to_balance_at set
    - Sum those past the hold cutoff = "mature" funds
    - If merchant has balance but no payment records (e.g. admin-credited),
      treat full balance as mature (no hold restriction)
    - If MIN_BALANCE_HOLD_HOURS = 0, skip check entirely
    """
    hold_hours = settings.MIN_BALANCE_HOLD_HOURS
    if hold_hours == 0:
        return  # hold period disabled

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hold_hours)
    res = await db.execute(
        select(Payment).where(
            Payment.merchant_id == merchant.id,
            Payment.status == PaymentStatus.confirmed,
            Payment.credited_to_balance_at.isnot(None),
        )
    )
    payments = res.scalars().all()

    if not payments:
        # No payment records with credited_to_balance_at.
        # Balance was set via admin or migration — no hold restriction.
        return

    mature = sum(
        (p.merchant_net_firo or 0.0)
        for p in payments
        if p.credited_to_balance_at and _tz(p.credited_to_balance_at) <= cutoff
    )
    available = min(mature, merchant.balance_firo or 0.0)

    if amount > available:
        # Calculate when enough funds will be available
        next_release_payments = sorted(
            [p for p in payments
             if p.credited_to_balance_at and _tz(p.credited_to_balance_at) > cutoff],
            key=lambda p: p.credited_to_balance_at
        )
        next_msg = ""
        if next_release_payments:
            next_at = _tz(next_release_payments[0].credited_to_balance_at) + timedelta(hours=hold_hours)
            next_msg = f" Next available: {next_at.strftime('%Y-%m-%d %H:%M UTC')}"

        raise WithdrawalHardError(
            f"Funds not yet mature — {available:.4f} FIRO available "
            f"(hold period: {hold_hours}h after confirmation).{next_msg}"
        )


async def _check_node_transparent_balance(amount_net: float):
    """Check node wallet has enough transparent balance. Temp error if not."""
    try:
        balance = await get_rpc().get_balance()
        if balance < amount_net:
            raise WithdrawalTempError(
                f"Node transparent balance low: {balance:.4f} FIRO available, "
                f"need {amount_net:.4f}. Will retry automatically."
            )
    except FiroRPCError as e:
        raise WithdrawalTempError(f"Node unreachable: {e.message}")
    except WithdrawalTempError:
        raise
    except Exception as e:
        raise WithdrawalTempError(f"Node check failed: {e}")


# NOTE: No pre-check for Spark balance.
# Spark is a private balance stored in wallet.dat — the node cannot expose it
# externally. We simply attempt spendspark and handle failure as a temp error.


# ─ Detect address type ──

def _detect_withdrawal_type(address: str) -> str:
    """
    Determine withdrawal type from destination address.
    Spark: sm... (mainnet) or st... (testnet), ~144 chars
    Transparent: all others
    """
    rpc = get_rpc()
    if rpc.is_spark_address(address):
        return "spark"
    return "transparent"


# ─ Step 1: Create withdrawal request ─

async def create_withdrawal_request(
    db: AsyncSession,
    merchant: User,
    amount: float,
    addr: str,
    ip: str | None,
    fee_pct: float,
    extra_risk: int = 0,
    addr_trusted: bool = True,
) -> Withdrawal:
    """
    Validate, assign tier, LOCK DB BALANCE ATOMICALLY, create record.
    Balance is deducted from DB immediately to prevent double-spend.
    Does NOT send — worker handles that after delay.

    extra_risk: additional risk points from untrusted address / IP anomaly detection
    addr_trusted: whether the destination address has been used before
    """
    from app.core.fees import calc_net as _cn
    fee_firo, net_firo = _cn(amount)

    # Detect address type
    wd_type = _detect_withdrawal_type(addr)

    # Validate Spark is enabled if requesting Spark withdrawal
    if wd_type == "spark" and not settings.SPARK_ENABLED:
        raise WithdrawalHardError(
            "Spark withdrawals are not enabled on this gateway. "
            "Use a transparent (t-address) instead."
        )

    # ─ Pre-flight hard checks (before touching DB) ─
    await _check_db_balance(merchant, amount)        # DB balance check — NOT UTXO
    await _check_daily_limit(merchant, amount, db)
    await _check_velocity(merchant)
    await _check_cooldown(merchant)
    await _check_no_active_withdrawal(merchant, db)

    risk = await calculate_risk_score(merchant, amount, db)
    risk = min(risk + extra_risk, 100)  # Add extra risk from untrusted addr / IP anomaly

    tier = classify_tier(amount, risk)

    # ─ Soft tier requires 2FA. If merchant has no TOTP, reject with a
    # structured error so the UI can prompt them to enable 2FA. Previously
    # we auto-escalated to admin review; that no longer exists.
    if tier == "soft" and not merchant.totp_enabled:
        raise WithdrawalSoftError("2fa_required_to_enable")

    # Untrusted address: force at least "soft" tier (require 2FA) if user has 2FA enabled
    if not addr_trusted and tier == "auto" and merchant.totp_enabled:
        tier = "soft"
        logger.info(f"Withdrawal to untrusted address → elevated to soft tier (requires 2FA)")

    process_after = datetime.now(timezone.utc) + timedelta(
        seconds=settings.WITHDRAWAL_DELAY_SECONDS
    )

    # NEW STATUS MAP:
    #   "auto"  → pending            (worker will send after delay)
    #   "soft"  → pending            (waits for TOTP verification)
    #   "email" → email_verify_pending  (waits for email code)
    if tier == "email":
        initial_status = WithdrawalStatus.email_verify_pending
    else:
        initial_status = WithdrawalStatus.pending

    # ─ ATOMIC: deduct balance + create record ──
    # Lock merchant row to prevent concurrent modification
    res = await db.execute(
        select(User).where(User.id == merchant.id).with_for_update()
    )
    locked_merchant = res.scalar_one_or_none()
    if not locked_merchant:
        raise WithdrawalHardError("Merchant not found")

    # Final balance check with locked row
    if (locked_merchant.balance_firo or 0.0) < amount:
        raise WithdrawalHardError(
            f"Insufficient balance: {locked_merchant.balance_firo:.8f} FIRO available"
        )

    # Deduct immediately — prevents any concurrent withdrawal from double-spending
    locked_merchant.balance_firo      = round((locked_merchant.balance_firo or 0) - amount, 8)
    locked_merchant.balance_withdrawn = round((locked_merchant.balance_withdrawn or 0) + amount, 8)
    db.add(locked_merchant)

    w = Withdrawal(
        merchant_id=locked_merchant.id,
        amount_requested=amount,
        withdrawal_fee_pct=fee_pct,
        withdrawal_fee_firo=fee_firo,
        amount_net=net_firo,
        destination_address=addr,
        withdrawal_type=wd_type,
        status=initial_status,
        tier=tier,
        risk_score=risk,
        process_after=process_after,
        ip_address=ip,
        attempts=0,
        balance_locked=True,
    )
    db.add(w)

    db.add(AuditLog(
        user_id=locked_merchant.id,
        action="withdrawal.created",
        entity_id=w.id,
        detail=(
            f"type={wd_type} amount={amount:.8f} net={net_firo:.8f} "
            f"tier={tier} risk={risk} status={initial_status} "
            f"to={addr[:20]} ip={ip}"
        ),
        ip_address=ip,
    ))

    await db.flush()
    await db.commit()
    await db.refresh(w)

    # ─ Email-tier: generate alphanumeric code, hash it, send to user ─
    if w.tier == "email":
        try:
            await _issue_email_verification_code(db, w, locked_merchant)
        except Exception as e:
            logger.error(f"Failed to issue email code for withdrawal {w.id[:8]}: {e}")

    logger.info(
        f"Withdrawal {w.id[:8]}: type={wd_type} tier={tier} "
        f"risk={risk} amount={amount:.4f} → {initial_status}"
    )
    return w


# ─ Email-tier verification helpers ─

# Character set: 36 symbols (digits + uppercase), skipping the visually
# ambiguous O/0/I/1. Total = 32 → 32^8 ≈ 1.09 * 10^12 possible codes.
# Brute force with 5 attempts/5min is astronomically unlikely to succeed.
_EMAIL_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 32 chars
_EMAIL_CODE_LEN      = 8
_EMAIL_CODE_TTL_SEC  = 300   # 5 minutes
_EMAIL_CODE_MAX_TRIES = 5
_EMAIL_CODE_RESEND_COOLDOWN = 60  # seconds


def _generate_email_code() -> str:
    import secrets
    return "".join(secrets.choice(_EMAIL_CODE_ALPHABET) for _ in range(_EMAIL_CODE_LEN))


def _hash_email_code(code: str, withdrawal_id: str, merchant_id: str) -> str:
    """
    Salted SHA-256 hash bound to this withdrawal + merchant so a leaked
    hash from one row cannot be pre-computed and reused elsewhere.
    """
    import hashlib
    salt = (settings.SECRET_KEY or "")[:32]
    material = f"{salt}|{merchant_id}|{withdrawal_id}|{code}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _verify_email_code_constant_time(code: str, stored_hash: str, withdrawal_id: str, merchant_id: str) -> bool:
    """
    Constant-time comparison to defeat hash-timing attacks. Computes the
    hash of the provided code with the same binding fields and compares
    byte-by-byte using hmac.compare_digest.
    """
    import hmac
    if not code or not stored_hash:
        return False
    want = _hash_email_code(code, withdrawal_id, merchant_id)
    return hmac.compare_digest(want, stored_hash)


async def _issue_email_verification_code(
    db: AsyncSession,
    withdrawal: Withdrawal,
    merchant: User,
) -> str:
    """
    Generate a fresh code, hash it, attach to the withdrawal row, reset
    attempts, and send the email. Returns the plaintext code (only used by
    the resend path for logging; never returned to the client).
    """
    code = _generate_email_code()
    now = datetime.now(timezone.utc)
    withdrawal.email_code_hash         = _hash_email_code(code, withdrawal.id, merchant.id)
    withdrawal.email_code_expires_at   = now + timedelta(seconds=_EMAIL_CODE_TTL_SEC)
    withdrawal.email_code_attempts     = 0
    withdrawal.email_code_last_sent_at = now
    db.add(withdrawal)
    await db.commit()
    await db.refresh(withdrawal)

    # Send email (best-effort — SMTP failures are logged but don't break the flow)
    try:
        from app.services.mailer import send_withdrawal_verify_email
        if merchant.email:
            await send_withdrawal_verify_email(
                to_email=merchant.email,
                code=code,
                amount_firo=withdrawal.amount_requested,
                destination=withdrawal.destination_address,
                ttl_minutes=_EMAIL_CODE_TTL_SEC // 60,
            )
    except Exception as e:
        logger.error(f"[withdrawal] mailer failed for {withdrawal.id[:8]}: {e}")

    return code


async def verify_withdrawal_email_code(
    db: AsyncSession,
    withdrawal: Withdrawal,
    merchant: User,
    code: str,
) -> tuple[bool, str]:
    """
    Verify a user-supplied email code against the withdrawal.
    Enforces expiry, max-attempts lock, and constant-time hash comparison.
    On success, status → approved (worker will send).
    """
    # Guard: only email-tier, still pending verification, not already locked
    if withdrawal.tier != "email":
        return False, "This withdrawal does not require email verification."
    if withdrawal.status == WithdrawalStatus.locked:
        return False, "This withdrawal is locked. Please create a new request."
    if withdrawal.status != WithdrawalStatus.email_verify_pending:
        return False, "This withdrawal has already been processed."
    if not withdrawal.email_code_hash or not withdrawal.email_code_expires_at:
        return False, "No active code. Please request a new one."

    # Expired?
    now = datetime.now(timezone.utc)
    exp = withdrawal.email_code_expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if now >= exp:
        # Invalidate expired code so it cannot be replayed.
        withdrawal.email_code_hash = None
        db.add(withdrawal)
        await db.commit()
        return False, "Code expired. Please request a new one."

    # Max attempts reached?
    if (withdrawal.email_code_attempts or 0) >= _EMAIL_CODE_MAX_TRIES:
        withdrawal.status = WithdrawalStatus.locked
        withdrawal.email_code_hash = None
        db.add(withdrawal)
        await db.commit()
        return False, "Too many wrong attempts. Withdrawal locked — please start a new request."

    # Constant-time comparison.
    clean_code = (code or "").strip().upper()
    ok = _verify_email_code_constant_time(clean_code, withdrawal.email_code_hash, withdrawal.id, merchant.id)

    if not ok:
        withdrawal.email_code_attempts = (withdrawal.email_code_attempts or 0) + 1
        remaining = _EMAIL_CODE_MAX_TRIES - withdrawal.email_code_attempts
        if remaining <= 0:
            withdrawal.status = WithdrawalStatus.locked
            withdrawal.email_code_hash = None
            db.add(withdrawal)
            await db.commit()
            return False, "Too many wrong attempts. Withdrawal locked — please start a new request."
        db.add(withdrawal)
        await db.commit()
        return False, f"Incorrect code. {remaining} attempt(s) left before the withdrawal is locked."

    # ─ Success ──
    # Mark verified, promote to pending so the worker picks it up, and
    # invalidate the hash so the same code cannot be reused.
    withdrawal.status           = WithdrawalStatus.pending
    withdrawal.email_code_hash  = None
    withdrawal.email_code_expires_at = None
    withdrawal.process_after    = datetime.now(timezone.utc) + timedelta(
        seconds=min(settings.WITHDRAWAL_DELAY_SECONDS, 60)
    )
    db.add(withdrawal)
    db.add(AuditLog(
        user_id=merchant.id,
        action="withdrawal.email_verified",
        entity_id=withdrawal.id,
        detail=f"tier=email amount={withdrawal.amount_requested:.8f} status → pending",
        ip_address=withdrawal.ip_address,
    ))
    await db.commit()
    await db.refresh(withdrawal)
    return True, "Email verified. Your withdrawal is now queued."


async def resend_withdrawal_email_code(
    db: AsyncSession,
    withdrawal: Withdrawal,
    merchant: User,
) -> tuple[bool, str]:
    """
    Issue a brand-new code and invalidate the old one. Rate-limited to
    once every 60s. Only allowed while status is still email_verify_pending.
    """
    if withdrawal.tier != "email":
        return False, "This withdrawal does not require email verification."
    if withdrawal.status != WithdrawalStatus.email_verify_pending:
        return False, "Withdrawal no longer waiting for a code."
    now = datetime.now(timezone.utc)
    last = withdrawal.email_code_last_sent_at
    if last:
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        delta = (now - last).total_seconds()
        if delta < _EMAIL_CODE_RESEND_COOLDOWN:
            wait = int(_EMAIL_CODE_RESEND_COOLDOWN - delta)
            return False, f"Please wait {wait}s before requesting a new code."

    try:
        await _issue_email_verification_code(db, withdrawal, merchant)
    except Exception as e:
        logger.error(f"[withdrawal] resend failed {withdrawal.id[:8]}: {e}")
        return False, "Could not send email. Try again shortly."
    return True, "A fresh code has been sent."


# ─ Step 2: Background worker ─

async def process_queued_withdrawals():
    """
    Runs every 15s.
    Handles:
      - Pending withdrawals past delay window
      - Spark operation polling (async z_sendmany tracking)
    """
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)

        # Transparent + Spark: pending past delay
        res = await db.execute(
            select(Withdrawal).where(
                Withdrawal.status.in_([
                    WithdrawalStatus.pending,
                    WithdrawalStatus.processing,
                ]),
                Withdrawal.process_after <= now,
                Withdrawal.tier.in_(["auto", "soft"]),
            ).with_for_update(skip_locked=True)
        )
        due = res.scalars().all()

        if due:
            logger.info(f"Withdrawal worker: {len(due)} to process")

        for w in due:
            await _process_single(db, w)


async def _process_single(db: AsyncSession, w: Withdrawal):
    """Process one withdrawal — transparent or Spark."""
    now = datetime.now(timezone.utc)

    res = await db.execute(
        select(User).where(User.id == w.merchant_id).with_for_update()
    )
    merchant = res.scalar_one_or_none()
    if not merchant:
        await _hard_fail(db, w, None, "Merchant not found — cannot process")
        return

    w.status  = WithdrawalStatus.processing
    w.attempts = (w.attempts or 0) + 1
    db.add(w)
    await db.flush()

    checks = {"type": w.withdrawal_type, "tier": w.tier, "attempt": w.attempts, "checks": []}

    try:
        # ─ Soft-tier: wait for 2FA ──
        if w.tier == "soft" and not w.totp_verified:
            w.status = WithdrawalStatus.pending
            w.processing_error = "Awaiting 2FA verification"
            w.process_after = now + timedelta(minutes=5)
            db.add(w)
            await db.flush()
            await db.commit()
            return

        # ─ Re-check hard limits ─
        await _check_daily_limit(merchant, w.amount_requested, db)
        checks["checks"].append({"check": "daily_limit", "passed": True})

        # Hold period only applies to transparent withdrawals.
        # Spark withdrawals use private balance — no hold period needed.
        if w.withdrawal_type != "spark":
            await _check_hold_period(merchant, w.amount_requested, db)
            checks["checks"].append({"check": "hold_period", "passed": True})
        else:
            checks["checks"].append({"check": "hold_period", "skipped": "spark"})

        # ─ Route by withdrawal type ─
        if w.withdrawal_type == "spark":
            await _send_spark(db, w, merchant, checks, now)
        else:
            await _send_transparent(db, w, merchant, checks, now)

    except WithdrawalTempError as e:
        await _handle_temp_error(db, w, merchant, str(e), checks, now)

    except WithdrawalHardError as e:
        await _hard_fail(db, w, merchant, str(e), checks)

    except Exception as e:
        # Unknown error — treat as temp, retry
        await _handle_temp_error(db, w, merchant, f"Unexpected: {e}", checks, now)
        logger.error(f"Withdrawal {w.id[:8]} unexpected error: {e}", exc_info=True)


# ─ Transparent send ─

async def _send_transparent(
    db: AsyncSession, w: Withdrawal, merchant: User,
    checks: dict, now: datetime
):
    """Execute transparent withdrawal via sendtoaddress."""
    # Check node balance before sending
    await _check_node_transparent_balance(w.amount_net)
    checks["checks"].append({"check": "node_balance", "passed": True})

    # Validate address
    if not await get_rpc().validate_address_rpc(w.destination_address):
        raise WithdrawalHardError(f"Invalid address: {w.destination_address[:30]}")

    # Send
    try:
        txid = await get_rpc().send_to_address(
            address=w.destination_address,
            amount=w.amount_net,
            comment=f"LavaPay-{w.id[:8]}",
        )
    except FiroRPCError as e:
        if e.code == -6:
            raise WithdrawalTempError("Node wallet has insufficient transparent funds")
        if e.code == -5:
            raise WithdrawalHardError(f"Invalid address: {e.message}")
        if e.code == -1:
            raise WithdrawalTempError(f"Node offline: {e.message}")
        raise WithdrawalTempError(f"RPC error ({e.code}): {e.message}")

    checks["checks"].append({"check": "rpc_send", "txid": txid, "passed": True})
    await _complete_withdrawal(db, w, merchant, txid, checks, now)


# ─ Spark send ─

async def _send_spark(
    db: AsyncSession, w: Withdrawal, merchant: User,
    checks: dict, now: datetime
):
    """
    Send from Spark private balance using spendspark.

    Flow:
      1. Run automintspark IMMEDIATELY — shields any pending transparent balance
         into the private Spark pool before attempting to spend from it.
         This is the KEY fix: Spark withdrawals must NOT wait for the random
         shield engine; they shield on-demand right before sending.
      2. Attempt spendspark.
      3. If -6 (insufficient Spark balance) after shielding, schedule retry —
         newly shielded funds may need 1 block confirmation before spending.
    """
    dest = w.destination_address
    if not dest or len(dest) < 25:
        raise WithdrawalHardError(f"Invalid destination address: {dest[:40]}")
    checks["checks"].append({"check": "address_format", "passed": True})

    # ─ Step 1: Shield transparent balance → Spark immediately ─
    # Every Spark withdrawal triggers automintspark first so the private pool
    # is always topped up before spendspark runs.
    logger.info(
        f"[withdrawal] Pre-shielding transparent balance before Spark send "
        f"(withdrawal={w.id[:8]})"
    )
    try:
        shield_result = await get_rpc().auto_mint_spark()
        if shield_result:
            logger.info(
                f"[withdrawal] automintspark OK before spendspark: "
                f"{str(shield_result)[:60]}"
            )
            checks["checks"].append({
                "check": "pre_shield", "passed": True,
                "result": str(shield_result)[:40]
            })
        else:
            logger.debug("[withdrawal] automintspark: nothing new to shield (balance already private)")
            checks["checks"].append({"check": "pre_shield", "passed": True, "note": "nothing_to_shield"})
    except Exception as shield_err:
        # Non-fatal — Spark pool may already have enough funds.
        logger.warning(
            f"[withdrawal] pre-shield warning (non-fatal): {shield_err} — "
            f"attempting spendspark anyway"
        )
        checks["checks"].append({"check": "pre_shield", "passed": False, "error": str(shield_err)[:80]})

    # ─ Step 2: Attempt spendspark ──
    logger.info(
        f"spendspark attempt: {w.id[:8]} → {dest[:20]}… "
        f"amount={w.amount_net:.8f}"
    )
    try:
        txid = await get_rpc().spark_send(
            to_address=dest,
            amount=w.amount_net,
            subtract_fee=False,
        )
    except FiroRPCError as e:
        if e.code == -6:
            # Insufficient Spark balance even after shielding.
            # Newly shielded funds need 1 block confirmation before spending.
            raise WithdrawalTempError(
                "Spark balance not yet spendable — freshly shielded funds need "
                "1 block confirmation. Will retry automatically."
            )
        if e.code == -5:
            raise WithdrawalHardError(f"Invalid address rejected by node: {e.message}")
        if e.code == -4:
            raise WithdrawalHardError(
                "Wallet unlock failed — check WALLET_PASSPHRASE in your .env file "
                "and restart the server."
            )
        if e.code == -1:
            raise WithdrawalTempError(f"Node unreachable: {e.message}")
        if e.code in (-8, -32600, -32700):
            raise WithdrawalHardError(
                "Cannot send Spark to this address. Exchange addresses only accept "
                "transparent (sendtoaddress) transfers. Use a personal wallet address."
            )
        raise WithdrawalTempError(f"spendspark RPC error ({e.code}): {e.message}")

    if not txid:
        raise WithdrawalTempError("spendspark returned empty txid — will retry")

    checks["checks"].append({"check": "spendspark", "txid": txid, "passed": True})
    await _complete_withdrawal(db, w, merchant, txid, checks, now)
    logger.info(f"✅ Spark withdrawal {w.id[:8]} completed: txid={txid[:16]}")

# Note: spendspark is synchronous — no polling needed.
# When spendspark returns a txid, the transaction is confirmed.
# The _complete_withdrawal() call in _send_spark() handles everything.


# ─ Completion ─

async def _complete_withdrawal(
    db: AsyncSession, w: Withdrawal, merchant: User | None,
    txid: str, checks: dict, now: datetime
):
    """Mark withdrawal as completed. Update merchant daily tracking."""
    w.status        = WithdrawalStatus.completed
    w.sent_txid     = txid
    w.sent_at       = now
    w.approved_at   = now
    w.auto_processed = True
    w.processing_error = None
    w.security_checks  = json.dumps(checks)
    db.add(w)

    if merchant:
        merchant.daily_withdrawal_used_firo = (
            (merchant.daily_withdrawal_used_firo or 0) + w.amount_requested
        )
        merchant.withdrawal_count_today = (merchant.withdrawal_count_today or 0) + 1
        merchant.last_withdrawal_at = now

        # Mark destination as trusted address for this user
        import json as _tj
        _trusted_raw = getattr(merchant, 'trusted_addresses_json', None)
        try:
            _trusted = _tj.loads(_trusted_raw) if _trusted_raw else []
        except Exception:
            _trusted = []
        if w.destination_address and w.destination_address not in _trusted:
            _trusted.append(w.destination_address)
            if len(_trusted) > 50:
                _trusted = _trusted[-50:]
            merchant.trusted_addresses_json = _tj.dumps(_trusted)
            logger.debug(f"Address {w.destination_address[:20]}… marked as trusted for user {merchant.id[:8]}")

        db.add(merchant)

    db.add(AuditLog(
        user_id=merchant.id if merchant else w.merchant_id,
        action="withdrawal.completed",
        entity_id=w.id,
        detail=(
            f"type={w.withdrawal_type} txid={txid} "
            f"amount={w.amount_net:.8f} attempts={w.attempts}"
        ),
    ))
    await db.flush()
    await db.commit()

    try:
        from app.services.webhook import fire_withdrawal_webhook
        await fire_withdrawal_webhook(db, w, "withdrawal.completed")
    except Exception:
        pass


# ─ Temp error handling (retry) ──

async def _handle_temp_error(
    db: AsyncSession, w: Withdrawal, merchant: User | None,
    error: str, checks: dict, now: datetime
):
    checks["temp_error"] = error
    attempt = w.attempts or 0

    if attempt < MAX_RETRIES:
        delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
        w.status        = WithdrawalStatus.pending
        w.process_after = now + timedelta(seconds=delay)
        w.processing_error = f"Retry {attempt}/{MAX_RETRIES}: {error}"
        w.security_checks  = json.dumps(checks)
        db.add(w)
        db.add(AuditLog(
            user_id=merchant.id if merchant else w.merchant_id,
            action="withdrawal.retry_scheduled",
            entity_id=w.id,
            detail=f"attempt={attempt} delay={delay}s reason={error[:100]}",
        ))
        await db.flush()
        await db.commit()
        logger.warning(
            f"⏳ Withdrawal {w.id[:8]}: temp error (attempt {attempt}/{MAX_RETRIES}), "
            f"retry in {delay}s — {error}"
        )
    else:
        # Max retries → under review
        w.status = WithdrawalStatus.manual_review
        w.processing_error = (
            f"Under review — paused after {MAX_RETRIES} attempts. "
            f"Last error: {error}"
        )
        w.security_checks = json.dumps(checks)
        db.add(w)
        db.add(AuditLog(
            user_id=merchant.id if merchant else w.merchant_id,
            action="withdrawal.under_review",
            entity_id=w.id,
            detail=f"max_retries={MAX_RETRIES} last_error={error[:100]}",
        ))
        await db.flush()
        await db.commit()
        logger.warning(f"🔵 Withdrawal {w.id[:8]} → under review after {MAX_RETRIES} retries")


# ─ Hard failure (refund) ─

async def _hard_fail(
    db: AsyncSession, w: Withdrawal, merchant: User | None,
    error: str, checks: dict = None
):
    """
    Hard failure — refund locked balance to DB.
    Balance_locked flag ensures we only refund once.
    """
    if checks:
        checks["hard_error"] = error

    w.status = WithdrawalStatus.failed
    w.processing_error = error
    if checks:
        w.security_checks = json.dumps(checks)
    db.add(w)

    # Refund only if balance was actually locked
    if w.balance_locked and merchant:
        merchant.balance_firo      = round((merchant.balance_firo or 0) + w.amount_requested, 8)
        merchant.balance_withdrawn = round((merchant.balance_withdrawn or 0) - w.amount_requested, 8)
        db.add(merchant)
        w.balance_locked = False
        db.add(w)
        logger.info(
            f"Withdrawal {w.id[:8]}: refunded {w.amount_requested:.4f} FIRO to DB balance"
        )

    db.add(AuditLog(
        user_id=merchant.id if merchant else w.merchant_id,
        action="withdrawal.failed_hard",
        entity_id=w.id,
        detail=error[:200],
    ))
    await db.flush()
    await db.commit()
    logger.error(f"❌ Withdrawal {w.id[:8]} HARD FAIL: {error}")


# ─ TOTP verification ─

async def verify_withdrawal_totp(
    db: AsyncSession,
    withdrawal: Withdrawal,
    merchant: User,
    totp_code: str,
) -> Tuple[bool, str]:
    if withdrawal.merchant_id != merchant.id:
        return False, "Withdrawal not found"
    if withdrawal.status not in (WithdrawalStatus.pending,):
        return False, f"Cannot verify — status is '{withdrawal.status}'"
    if withdrawal.tier != "soft":
        return False, "2FA not required for this withdrawal"
    if not merchant.totp_enabled or not merchant.totp_secret_enc:
        return False, "2FA not enabled on your account"

    from app.core.totp import verify_totp_code, decrypt_totp_secret
    from app.core.security import get_fernet

    secret = decrypt_totp_secret(merchant.totp_secret_enc, get_fernet())
    if not secret:
        return False, "2FA secret error — contact support"
    if not verify_totp_code(secret, totp_code):
        return False, "Invalid 2FA code"

    withdrawal.totp_verified = True
    withdrawal.process_after = datetime.now(timezone.utc) + timedelta(seconds=10)
    db.add(withdrawal)
    db.add(AuditLog(
        user_id=merchant.id, action="withdrawal.totp_verified",
        entity_id=withdrawal.id,
        detail="2FA verified — queued for processing in 10s",
    ))
    await db.flush()
    await db.commit()
    return True, "2FA verified — withdrawal will be sent shortly"
