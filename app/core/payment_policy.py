"""Per-merchant payment policy: confirmation-count requirement and amount
tolerance. Both are resolved lazily (never snapshotted onto a Payment row
except required_confirmations, which is copied once at invoice creation
time — see app/api/payments.py) so a merchant changing their policy only
affects not-yet-confirmed payments, never rewrites history.

CE only: when a merchant hasn't set their own confirmation policy, this
falls back to the existing operator-set instance-wide SystemConfig default
(app/core/system_settings.py, PATCH /api/panel/settings) rather than
straight to the hardcoded settings default — that operator control stays
exactly as it worked before, just as the fallback layer under per-merchant
policy rather than the only lever.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.system_settings import REQUIRED_CONFIRMATIONS_KEY, get_setting
from app.models.models import Payment, PaymentStatus, User

VALID_CONFIRMATION_POLICIES = (0, 1, 3, 6)


async def resolve_required_confirmations(db: AsyncSession, merchant: User) -> int:
    if merchant.required_confirmations_policy is not None:
        return merchant.required_confirmations_policy
    return int(await get_setting(db, REQUIRED_CONFIRMATIONS_KEY, str(get_settings().REQUIRED_CONFIRMATIONS)))


def resolve_tolerance_firo(merchant: User | None) -> float:
    if merchant is not None and merchant.payment_tolerance_firo is not None:
        return merchant.payment_tolerance_firo
    return get_settings().DEFAULT_PAYMENT_TOLERANCE_FIRO


def display_status(p: Payment, status=None) -> str:
    """Client-facing status label only — never used to drive webhooks,
    confirmations, or any other backend decision. 'Partially paid' is
    derived on the fly from amount_received vs amount_firo rather than
    being a stored PaymentStatus value, so every existing consumer of
    Payment.status (webhooks, CSV export, panel filters) is unaffected.
    Pass `status` to evaluate against an already-overridden status value
    (e.g. _fmt()'s expired/pending corrections) instead of the raw p.status."""
    effective = status if status is not None else p.status
    recv = p.amount_received or 0
    if effective in (PaymentStatus.pending, PaymentStatus.confirming) and 0 < recv < p.amount_firo:
        return "partially_paid"
    return effective.value if hasattr(effective, "value") else str(effective)
