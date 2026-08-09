from __future__ import annotations

from typing import Tuple, Optional

import httpx
from loguru import logger

from app.core.config import get_settings

APP_NAME_DEFAULT = "FiroGate"
ACCENT = "#B82334"
TEXT = "#111111"
MUTED = "#666666"
BORDER = "#dddddd"

RESEND_ENDPOINT = "https://api.resend.com/emails"
RESEND_TIMEOUT_SECONDS = 15


def _fmt_firo(v: float | None) -> str:
    if not v:
        return "0.00"
    n = float(v)
    if n >= 0.01:
        return f"{n:.2f}"
    if n >= 0.0001:
        return f"{n:.4f}"
    return f"{n:.8f}"


def _is_configured() -> bool:
    s = get_settings()
    return bool(s.RESEND_API_KEY and s.FROM_EMAIL)


def _wrap(title: str, heading: str, body_html: str, cta_label: str = "", cta_url: str = "") -> Tuple[str, str]:
    app_name = (get_settings().APP_NAME or APP_NAME_DEFAULT)
    cta = f'<p style="margin:16px 0 0 0"><a href="{cta_url}" style="color:{ACCENT}">{cta_label}</a></p>' if cta_url and cta_label else ""

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title></head>
<body style="margin:0;padding:20px;background:#ffffff;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:{TEXT};font-size:14px;line-height:1.6">
  <div style="font-size:12px;color:{MUTED};text-transform:uppercase;letter-spacing:.04em">{app_name}</div>
  <h1 style="margin:8px 0 14px 0;font-size:18px;font-weight:700;color:{TEXT}">{heading}</h1>
  <div>{body_html}</div>
  {cta}
</body></html>"""

    plain = f"{app_name}\n\n{heading}\n\n" + (f"{cta_label}: {cta_url}\n" if cta_url else "")
    return html, plain


async def send_email(
    to: str,
    subject: str,
    html: str,
    text: Optional[str] = None,
    from_name: Optional[str] = None,
) -> bool:
    if not _is_configured():
        logger.error("[mailer] RESEND_API_KEY or FROM_EMAIL not set email NOT sent")
        return False

    s = get_settings()
    sender_name = from_name or s.FROM_NAME or (s.APP_NAME or APP_NAME_DEFAULT)

    payload: dict = {
        "from": f"{sender_name} <{s.FROM_EMAIL}>",
        "to": [to],
        "subject": subject,
        "html": html,
        "text": text or " ",
    }

    headers = {
        "Authorization": f"Bearer {s.RESEND_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=RESEND_TIMEOUT_SECONDS) as client:
            resp = await client.post(RESEND_ENDPOINT, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        logger.error(f"[mailer] network error sending to {to}: {exc}")
        return False

    if resp.status_code in (200, 201):
        logger.info(f"[mailer] sent {subject!r} to {to}")
        return True

    body_snippet = (resp.text or "")[:400]
    logger.error(
        f"[mailer] Resend rejected send to {to}: "
        f"status={resp.status_code} body={body_snippet}"
    )
    return False


async def _send(to_email: str, subject: str, html: str, plain: str, from_name: Optional[str] = None) -> bool:
    return await send_email(to_email, subject, html, text=plain, from_name=from_name)


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
    """Payment receipt plain text lines, no card, no dashboard link."""
    safe_merchant = (merchant_name or "").strip() or "Store"
    amount = _fmt_firo(amount_received if amount_received else amount_firo)
    product = (order_description or order_id or "Payment").strip()
    currency = "tFIRO" if is_testnet else "FIRO"

    when = ""
    if confirmed_at_iso:
        when = confirmed_at_iso.replace("T", " ").split(".")[0] + " UTC"

    short_order = (order_id or "").strip()

    lines_html = [f"<p style='margin:0 0 4px 0'><strong>Amount paid:</strong> {amount} {currency}</p>"]
    lines_html.append(f"<p style='margin:0 0 4px 0'><strong>Product:</strong> {product}</p>")
    if short_order:
        lines_html.append(f"<p style='margin:0 0 4px 0'><strong>Order ID:</strong> {short_order}</p>")
    if txid:
        if explorer_url:
            lines_html.append(
                f"<p style='margin:0 0 4px 0'><strong>Transaction:</strong> "
                f"<a href=\"{explorer_url}\" style=\"color:{ACCENT}\">{txid}</a></p>"
            )
        else:
            lines_html.append(f"<p style='margin:0 0 4px 0'><strong>Transaction:</strong> {txid}</p>")
    if when:
        lines_html.append(f"<p style='margin:0 0 4px 0'><strong>Date:</strong> {when}</p>")

    body_html = (
        f"<p style='margin:0 0 12px 0'>Your payment to {safe_merchant} has been confirmed.</p>"
        + "".join(lines_html)
    )

    html, plain = _wrap(
        title=f"{safe_merchant} Payment confirmation",
        heading="Payment confirmed",
        body_html=body_html,
    )

    plain = (
        f"{safe_merchant} Payment confirmation\n\n"
        f"Your payment to {safe_merchant} has been confirmed.\n\n"
        f"Amount paid: {amount} {currency}\n"
        f"Product: {product}\n"
        + (f"Order ID: {short_order}\n" if short_order else "")
        + (f"Transaction: {txid}\n" if txid else "")
        + (f"Date: {when}\n" if when else "")
    )

    subject = f"{safe_merchant} Payment confirmation"
    return await _send(to_email, subject, html, plain, from_name=safe_merchant)
