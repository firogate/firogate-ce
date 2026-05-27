"""
SendGrid HTTP mailer (SMTP fully removed).

Email templates (unchanged):
  * email_verification     — after registration        (platform-branded)
  * password_reset         — after forgot-password     (platform-branded)
  * payment_receipt        — after confirmed payment   (MERCHANT-branded, no FiroGate mentions)
  * report_status          — admin updates a report    (platform-branded)
  * app_name_updated       — admin approves app_name   (platform-branded)

All templates include a plain-text fallback. No emojis, SVG-only icons.
Transport: SendGrid v3 Mail Send API over HTTPS via httpx.
"""
from __future__ import annotations

from typing import Tuple, Optional

import httpx
from loguru import logger

from app.core.config import get_settings

APP_NAME_DEFAULT = "FiroGate"
ACCENT = "#F5C542"
BG = "#0b0a0d"
CARD = "#141217"
BORDER = "#2a2731"
TEXT = "#ffffff"
MUTED = "#9ca3af"
GREEN = "#22C55E"

SENDGRID_ENDPOINT = "https://api.sendgrid.com/v3/mail/send"
SENDGRID_TIMEOUT_SECONDS = 15


def _is_configured() -> bool:
    s = get_settings()
    return bool(s.SENDGRID_API_KEY and s.FROM_EMAIL)


# ─ Minimal, mobile-friendly templates ─
# Design rules:
#   - No logo / branding noise. App name only, text.
#   - One headline, one action (button or code), zero extra paragraphs.
#   - No "If the button doesn't work copy this URL" rescue row.
#   - No "If you did not request this" disclaimer row.
#   - Dark neutral palette, single card, 16px padding. Works on 320-wide
#     screens without media queries.

def _wrap(title: str, heading: str, body_html: str, cta_label: str, cta_url: str) -> Tuple[str, str]:
    """Return (html, plain) for a platform email."""
    app_name = (get_settings().APP_NAME or APP_NAME_DEFAULT)
    cta = _cta_row(cta_label, cta_url) if cta_url and cta_label else ""

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title></head>
<body style="margin:0;padding:24px 12px;background:{BG};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:{TEXT}">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
    <tr><td align="center">
      <table role="presentation" width="480" cellspacing="0" cellpadding="0" border="0" style="max-width:480px;width:100%;background:{CARD};border:1px solid {BORDER};border-radius:14px">
        <tr><td style="padding:28px 28px 8px 28px">
          <div style="font-size:12px;font-weight:600;letter-spacing:.08em;color:{MUTED};text-transform:uppercase">{app_name}</div>
          <h1 style="margin:10px 0 14px 0;font-size:20px;line-height:1.3;font-weight:700;color:{TEXT}">{heading}</h1>
        </td></tr>
        <tr><td style="padding:0 28px 22px 28px;font-size:14.5px;line-height:1.55;color:{MUTED}">{body_html}</td></tr>
        {cta}
      </table>
    </td></tr>
  </table>
</body></html>"""

    plain = f"{app_name}\n\n{heading}\n\n" + (f"{cta_label}: {cta_url}\n" if cta_url else "")
    return html, plain


def _cta_row(cta_label: str, cta_url: str) -> str:
    return (
        f'<tr><td style="padding:0 28px 28px 28px">'
        f'<a href="{cta_url}" style="display:inline-block;background:{ACCENT};color:#000;'
        f'text-decoration:none;font-weight:700;font-size:15px;padding:12px 22px;border-radius:10px">'
        f'{cta_label}</a>'
        f'</td></tr>'
    )


def _wrap_merchant(
    merchant_name: str,
    title: str,
    heading: str,
    body_html: str,
) -> Tuple[str, str]:
    """Light receipt template branded with merchant name only. No FiroGate refs."""
    safe_merchant = (merchant_name or "").strip() or "Store"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title></head>
<body style="margin:0;padding:24px 12px;background:#f6f7f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#111">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
    <tr><td align="center">
      <table role="presentation" width="480" cellspacing="0" cellpadding="0" border="0" style="max-width:480px;width:100%;background:#ffffff;border:1px solid #e5e7eb;border-radius:12px">
        <tr><td style="padding:24px 24px 6px 24px">
          <div style="font-size:12px;font-weight:600;letter-spacing:.08em;color:#6b7280;text-transform:uppercase">{safe_merchant}</div>
          <h1 style="margin:8px 0 14px 0;font-size:20px;line-height:1.3;font-weight:700;color:#111">{heading}</h1>
        </td></tr>
        <tr><td style="padding:0 24px 24px 24px;font-size:14px;line-height:1.55;color:#444">{body_html}</td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""

    plain = f"{safe_merchant}\n\n{heading}\n"
    return html, plain


# ─ SendGrid HTTP transport ─
async def send_email(
    to: str,
    subject: str,
    html: str,
    text: Optional[str] = None,
    from_name: Optional[str] = None,
) -> bool:
    """
    Single entry point used by every helper below. Sends via SendGrid v3.
    Returns True on 202 Accepted, False on any other outcome. Never raises.
    The API key is loaded from env only — never logged.
    """
    if not _is_configured():
        logger.error("[mailer] SENDGRID_API_KEY or FROM_EMAIL not set — email NOT sent")
        return False

    s = get_settings()
    sender_name = from_name or s.FROM_NAME or (s.APP_NAME or APP_NAME_DEFAULT)

    payload: dict = {
        "personalizations": [
            {"to": [{"email": to}], "subject": subject}
        ],
        "from": {"email": s.FROM_EMAIL, "name": sender_name},
        "reply_to": {"email": s.FROM_EMAIL, "name": sender_name},
        "content": [
            {"type": "text/plain", "value": text or " "},
            {"type": "text/html", "value": html},
        ],
        "mail_settings": {"sandbox_mode": {"enable": False}},
        "tracking_settings": {
            "click_tracking": {"enable": False, "enable_text": False},
            "open_tracking": {"enable": False},
        },
    }

    headers = {
        "Authorization": f"Bearer {s.SENDGRID_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=SENDGRID_TIMEOUT_SECONDS) as client:
            resp = await client.post(SENDGRID_ENDPOINT, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        logger.error(f"[mailer] network error sending to {to}: {exc}")
        return False

    if resp.status_code == 202:
        logger.info(f"[mailer] sent {subject!r} to {to}")
        return True

    body_snippet = (resp.text or "")[:400]
    logger.error(
        f"[mailer] SendGrid rejected send to {to}: "
        f"status={resp.status_code} body={body_snippet}"
    )
    return False


async def _send(to_email: str, subject: str, html: str, plain: str, from_name: Optional[str] = None) -> bool:
    """Back-compat shim for the many helpers below that already call _send()."""
    return await send_email(to_email, subject, html, text=plain, from_name=from_name)


# ─ Platform emails (verification / reset) ─
async def send_verification_email(to_email: str, verify_url: str) -> bool:
    app_name = (get_settings().APP_NAME or APP_NAME_DEFAULT)
    html, plain = _wrap(
        title=f"Verify your email — {app_name}",
        heading="Verify your email",
        body_html="<p style='margin:0'>Tap the button below to activate your account.</p>",
        cta_label="Verify Email",
        cta_url=verify_url,
    )
    return await _send(to_email, f"{app_name} — Verify your email", html, plain)


async def send_password_reset_email(to_email: str, reset_url: str) -> bool:
    app_name = (get_settings().APP_NAME or APP_NAME_DEFAULT)
    html, plain = _wrap(
        title=f"Reset your password — {app_name}",
        heading="Reset your password",
        body_html="<p style='margin:0'>Tap the button below to set a new password.</p>",
        cta_label="Reset Password",
        cta_url=reset_url,
    )
    return await _send(to_email, f"{app_name} — Reset your password", html, plain)


async def send_withdrawal_verify_email(
    to_email: str,
    code: str,
    amount_firo: float,
    destination: str,
    ttl_minutes: int = 5,
) -> bool:
    """
    Send a one-time alphanumeric code for a large-tier withdrawal. The
    plaintext code is shown exactly once — only in this email — and is
    never logged anywhere on the platform.
    """
    app_name = (get_settings().APP_NAME or APP_NAME_DEFAULT)
    safe_code = (code or "").strip()
    # Mask the destination address — show first 8 + last 6 chars.
    dest = (destination or "").strip()
    if len(dest) > 18:
        masked_dest = f"{dest[:8]}…{dest[-6:]}"
    else:
        masked_dest = dest
    amt_str = f"{float(amount_firo or 0):.8f}".rstrip("0").rstrip(".")
    if "." not in amt_str:
        amt_str += ".00"

    body_html = (
        f"<p style='margin:0 0 14px 0'>Withdrawal of <strong style='color:{TEXT}'>{amt_str} FIRO</strong> "
        f"to <code style='background:#0d0b05;padding:2px 7px;border-radius:6px;font-family:monospace;color:{ACCENT}'>{masked_dest}</code></p>"
        f"<div style='margin:4px 0 14px 0;padding:16px;background:#0d0b05;border:1px solid {ACCENT};border-radius:10px;"
        f"font-family:'JetBrains Mono',Menlo,monospace;font-size:26px;letter-spacing:.28em;color:{ACCENT};font-weight:800;text-align:center'>"
        f"{safe_code}</div>"
        f"<p style='margin:0;font-size:13px;color:{MUTED}'>Expires in {ttl_minutes} minute{'s' if ttl_minutes != 1 else ''}.</p>"
    )
    html, plain = _wrap(
        title=f"Withdrawal verification code — {app_name}",
        heading="Confirm your withdrawal",
        body_html=body_html,
        cta_label="",
        cta_url="",
    )
    # Strip any CTA row — codes don't get a button.
    return await _send(
        to_email,
        f"{app_name} — Your withdrawal code: {safe_code}",
        html,
        plain,
    )


# ─ Merchant-branded payment receipt ─
def _fmt_firo(v) -> str:
    try:
        n = float(v or 0)
    except Exception:
        return "0.00"
    if n == 0:
        return "0.00"
    if n >= 0.01:
        return f"{n:.2f}"
    if n >= 0.0001:
        return f"{n:.4f}"
    return f"{n:.8f}"


async def send_payment_receipt_email(
    to_email: str,
    merchant_name: str,
    *,
    order_id: str | None,
    order_description: str | None,
    amount_received: float | None,
    amount_firo: float | None,
    txid: str | None,
    confirmed_at_iso: str | None,
    is_testnet: bool,
    explorer_url: str | None,
) -> bool:
    """
    Stripe-style invoice / payment-confirmation email.

    Table-only layout, inline styles, no background images, no external CSS,
    mobile-friendly at 320px. Branded entirely with merchant_name — the
    platform (FiroGate) is never mentioned.
    """
    safe_merchant = (merchant_name or "").strip() or "Store"
    amount = _fmt_firo(amount_received if amount_received else amount_firo)
    product = (order_description or order_id or "Payment").strip()
    currency = "tFIRO" if is_testnet else "FIRO"

    when = ""
    if confirmed_at_iso:
        when = confirmed_at_iso.replace("T", " ").split(".")[0] + " UTC"

    short_order = (order_id or "").strip()
    short_txid = ""
    if txid:
        short_txid = f"{txid[:10]}…{txid[-8:]}" if len(txid) > 22 else txid

    # ─ Row helper ─
    def row(label: str, value: str, mono: bool = False) -> str:
        font = "font-family:SFMono-Regular,Menlo,Consolas,monospace;" if mono else ""
        return (
            f'<tr>'
            f'<td style="padding:10px 0;font-size:13px;color:#6b7280;width:45%;vertical-align:top">{label}</td>'
            f'<td style="padding:10px 0;font-size:14px;color:#111827;text-align:right;font-weight:500;{font}word-break:break-all">{value}</td>'
            f'</tr>'
        )

    detail_rows = ""
    detail_rows += row("Product", product)
    if short_order:
        detail_rows += row("Order ID", short_order, mono=True)
    if short_txid and explorer_url:
        link = (
            f'<a href="{explorer_url}" style="color:#2563eb;text-decoration:none;font-family:SFMono-Regular,Menlo,monospace">'
            f'{short_txid}</a>'
        )
        detail_rows += row("Transaction", link)
    elif short_txid:
        detail_rows += row("Transaction", short_txid, mono=True)
    if when:
        detail_rows += row("Date", when)
    detail_rows += row("Customer", to_email)

    cta_row = ""
    if explorer_url:
        cta_row = (
            f'<tr><td align="center" style="padding:20px 32px 8px 32px">'
            f'<a href="{explorer_url}" '
            f'style="display:inline-block;background:#111827;color:#ffffff;text-decoration:none;'
            f'font-size:14px;font-weight:600;padding:11px 22px;border-radius:8px">View details</a>'
            f'</td></tr>'
        )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{safe_merchant} — Payment confirmation</title></head>
<body style="margin:0;padding:24px 12px;background:#f4f5f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#111827">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
    <tr><td align="center">
      <table role="presentation" width="520" cellspacing="0" cellpadding="0" border="0" style="max-width:520px;width:100%">

        <!-- Header -->
        <tr><td align="center" style="padding:0 8px 18px 8px">
          <div style="font-size:16px;font-weight:700;color:#111827;letter-spacing:-.01em">{safe_merchant}</div>
          <div style="font-size:12px;color:#6b7280;margin-top:2px">Payment confirmation</div>
        </td></tr>

        <!-- Main card -->
        <tr><td style="background:#ffffff;border:1px solid #e5e7eb;border-radius:12px">
          <!-- Amount hero -->
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
            <tr><td align="center" style="padding:32px 32px 10px 32px">
              <div style="font-size:13px;color:#6b7280;font-weight:500">Amount paid</div>
              <div style="margin-top:6px;font-size:32px;font-weight:700;color:#111827;letter-spacing:-.02em;font-family:SFMono-Regular,Menlo,Consolas,monospace">
                {amount} <span style="font-size:18px;color:#6b7280;font-weight:600">{currency}</span>
              </div>
              <div style="margin-top:14px">
                <span style="display:inline-block;padding:4px 10px;background:#ecfdf5;color:#047857;border-radius:999px;font-size:12px;font-weight:600;letter-spacing:.02em">Paid</span>
              </div>
            </td></tr>

            <!-- Divider -->
            <tr><td style="padding:20px 32px 0 32px">
              <div style="border-top:1px solid #f0f1f4"></div>
            </td></tr>

            <!-- Details table -->
            <tr><td style="padding:6px 32px 24px 32px">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
                {detail_rows}
              </table>
            </td></tr>

            {cta_row}
          </table>
        </td></tr>

        <!-- Footer -->
        <tr><td align="center" style="padding:20px 8px 4px 8px;font-size:12px;color:#9ca3af;line-height:1.5">
          Receipt from {safe_merchant}
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""

    plain = (
        f"{safe_merchant} — Payment confirmation\n\n"
        f"Amount paid: {amount} {currency}\n"
        f"Status: Paid\n"
        f"Product: {product}\n"
        + (f"Order ID: {short_order}\n" if short_order else "")
        + (f"Transaction: {txid}\n" if txid else "")
        + (f"Date: {when}\n" if when else "")
        + f"Customer: {to_email}\n"
        + (f"\nView details: {explorer_url}\n" if explorer_url else "")
    )

    subject = f"{safe_merchant} — Payment confirmation"
    return await _send(to_email, subject, html, plain, from_name=safe_merchant)


# ─ Report status emails (platform-branded) ──
async def send_report_status_email(
    to_email: str,
    *,
    status: str,              # "in_progress" | "resolved" | "dismissed"
    report_type: str,
    subject_line: str | None,
    admin_notes: str | None,
    report_id: str,
) -> bool:
    """Notify the reporter about a status change on their report."""
    app_name = (get_settings().APP_NAME or APP_NAME_DEFAULT)

    friendly = {
        "in_progress": "Your report is being reviewed",
        "resolved":    "Your report has been resolved",
        "dismissed":   "Your report has been closed",
    }.get(status, "Update on your report")

    intro = {
        "in_progress": "Your report is being reviewed.",
        "resolved":    "Your report has been resolved.",
        "dismissed":   "Your report has been closed.",
    }.get(status, "Your report has an update.")

    extra = ""
    if admin_notes:
        esc = (admin_notes[:1500]).replace("<", "&lt;").replace(">", "&gt;")
        extra = (
            f'<p style="margin:14px 0 4px 0;color:{TEXT};font-weight:600">Message from our team</p>'
            f'<div style="padding:12px 14px;background:rgba(255,255,255,.03);border:1px solid {BORDER};'
            f'border-radius:10px;color:{MUTED};line-height:1.55;white-space:pre-wrap">{esc}</div>'
        )

    ref = f'<p style="margin:14px 0 0 0;font-size:12.5px;color:{MUTED}">Report reference: <code style="color:{ACCENT}">{report_id}</code></p>'
    body_html = f'<p style="margin:0 0 6px 0">{intro}</p>{extra}{ref}'

    html, plain = _wrap(
        title=f"{friendly} — {app_name}",
        heading=friendly,
        body_html=body_html,
        cta_label="",
        cta_url="",
    )
    return await _send(to_email, f"{app_name} — {friendly}", html, plain)


async def send_app_name_updated_email(to_email: str, new_app_name: str) -> bool:
    """Notify user that their app-name change was approved and applied."""
    app_name = (get_settings().APP_NAME or APP_NAME_DEFAULT)
    safe = (new_app_name or "").strip() or "your new name"

    body_html = (
        f'<p style="margin:0 0 10px 0">Your app name is now:</p>'
        f'<p style="margin:0;font-size:18px;font-weight:700;color:{TEXT}">{safe}</p>'
    )

    html, plain = _wrap(
        title=f"Your app name has been updated — {app_name}",
        heading="App name updated",
        body_html=body_html,
        cta_label="",
        cta_url="",
    )
    return await _send(to_email, f"{app_name} — App name updated", html, plain)


# ─ Merchant payment notification ─
async def send_merchant_payment_notification(
    to_email: str,
    *,
    merchant_name: str,
    amount_firo: float,
    net_firo: float,
    fee_firo: float,
    order_id: str | None,
    order_description: str | None,
    customer_email: str | None,
    payment_id: str,
    txid: str | None,
    dashboard_url: str,
) -> bool:
    """
    Notify the merchant when a payment is confirmed.
    Dark-themed, branded with platform name.
    """
    app_name = (get_settings().APP_NAME or APP_NAME_DEFAULT)
    safe_merchant = (merchant_name or "").strip() or "Store"

    amt_str = f"{float(amount_firo or 0):.8f}".rstrip("0").rstrip(".")
    net_str = f"{float(net_firo or 0):.8f}".rstrip("0").rstrip(".")
    fee_str = f"{float(fee_firo or 0):.8f}".rstrip("0").rstrip(".")

    short_pid = payment_id[:8] if payment_id else "—"
    short_txid = ""
    if txid:
        short_txid = f"{txid[:10]}…{txid[-8:]}" if len(txid) > 22 else txid

    def drow(label: str, val: str, color: str = MUTED) -> str:
        return (
            f'<tr>'
            f'<td style="padding:7px 0;font-size:13px;color:{MUTED};width:40%">{label}</td>'
            f'<td style="padding:7px 0;font-size:13.5px;color:{color};text-align:right;'
            f'font-family:SFMono-Regular,Menlo,monospace;word-break:break-all">{val}</td>'
            f'</tr>'
        )

    rows = drow("Amount received", f"{amt_str} FIRO", TEXT)
    rows += drow("Platform fee", f"−{fee_str} FIRO")
    rows += drow("Your net", f"{net_str} FIRO", GREEN)
    if order_id:
        rows += drow("Order ID", order_id)
    if order_description:
        rows += (
            f'<tr><td colspan="2" style="padding:7px 0;font-size:13px;color:{MUTED}">'
            f'Product: {order_description[:80]}</td></tr>'
        )
    if customer_email:
        rows += drow("Customer", customer_email)
    if short_txid:
        rows += drow("TXID", short_txid)

    body_html = (
        f'<p style="margin:0 0 16px 0;font-size:15px;color:{TEXT}">'
        f'A new payment of <strong style="color:{ACCENT}">{amt_str} FIRO</strong> '
        f'has been confirmed.</p>'
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" '
        f'style="border-top:1px solid {BORDER}">{rows}</table>'
    )

    html, plain = _wrap(
        title=f"Payment confirmed — {app_name}",
        heading="Payment confirmed",
        body_html=body_html,
        cta_label="View in Dashboard",
        cta_url=dashboard_url,
    )
    subject = f"{app_name} — Payment confirmed · {amt_str} FIRO"
    return await _send(to_email, subject, html, plain)
