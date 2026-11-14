# Changelog

Current: **API v1.0** · **App 0.1.2**

---

## [0.1.2] 2026-11 App 0.1.2

[Live demo](https://demo.firogate.com) · [Demo source](https://github.com/firogate/demo)

### API
#### Added
- Spark mint tracking: `GET /api/privacy/status`, `GET /api/privacy/inputs`, `GET /api/privacy/payments/{id}` and `PATCH /api/privacy/settings`. Read-only; FiroGate never holds keys or mints. See [PRIVACY_TRACKING.md](PRIVACY_TRACKING.md).

### App
#### Added
- Spark mints page: see when transparent payments are minted to Spark in your own wallet, matched by the exact coins spent and checked with your Spark view key. Needs `addressindex=1` and `txindex=1` on the node.
- Payment page: a separate "Spark mint" section for transparent payments.
- [Live demo](https://demo.firogate.com): try the dashboard, checkout and API with fake FIRO. No install.
- Tips on hover explain pages, cards and settings. On phones, tap the ⓘ.
- Sidebar icons animate once when tapped.
- New landing page with real screenshots of the dashboard and checkout.

#### Changed
- Flat design: no shadows or glow.
- Shorter text: explanations moved into tips.
- The home status page is one simple list.
- Phones: titles and buttons fit, tables turn into cards, and the bottom bar floats.

#### Fixed
- The Changelog page no longer crashes.
- Checkout hides the empty price box when there is no live price, in all 25 styles.
- Pay links no longer get stuck on "Loading price…".
- Payment Activity opens on today, not on 2023.
- The folded sidebar shows the Settings and Account icons.

---

## [0.1.0] 2026-11 App 0.1.0

### API
#### Added
- Transparent payments from a wallet xpub. Connect it with `POST /api/xpub/connect`; each order gets its own Firo address.
- Choose payment methods with `PATCH /api/xpub/methods`: Spark, transparent or both, and which one checkout shows first.
- Payments list their available addresses in `methods`, and customers can switch on the checkout with `POST /api/payments/public/{id}/method`.
- Refunds: `POST /api/payments/{id}/refunds`, `GET /api/payments/{id}/refunds`, `DELETE /api/refunds/{id}`, `GET /api/refunds/summary`. Payments now include `refunded_firo`.
- Recurring payments: `GET/POST /api/recurring`, `PATCH /api/recurring/{id}`, `POST /api/recurring/{id}/send-now`, `DELETE /api/recurring/{id}`.
- Webhooks `payment.refunded` and `recurring.invoice_created`.
- API key permissions `manage_refunds` and `manage_recurring`.

#### Fixed
- API key permissions are now enforced. A key limited to reading could still create invoices or change the wallet.

### App
#### Added
- Payment methods page: turn Spark and transparent payments on or off, and pick what checkout shows first.
- Checkout lets customers switch between Spark and transparent when both are on.
- Recurring page: bill customers every week, month or year with an emailed pay link.
- Record refunds on confirmed payments and see the refunded total in Analytics.
- Diagnostics shows node, server, background jobs and payment health, with a copy report button.
- Full German, Russian and Chinese translations for the dashboard.

#### Changed
- New design: sidebar with sections, page titles with a short purpose line, colored stat cards and the FiroGate teal palette.
- Calmer checkout notice, and no blur effects in the dashboard.

#### Fixed
- The node status no longer says synced when the node is offline.
- The sidebar shows the real app version.
- Monthly recurring dates keep their day of the month.

#### Security
- Only one account can claim the operator role without a verified email, and auto-made usernames never match an operator name.
- Google sign-in no longer joins an existing password account whose email was never verified.
- 2FA is now required on Google, account number and wallet sign-in, not only password sign-in.
- Setting up 2FA can no longer silently turn off 2FA that is already on.
- Receipt emails escape store and product names.

---

## [0.0.9] 2026-09 — App 0.0.9

### API
_No API changes._

### App
#### Fixed
- Change Password: setting a new password identical to your current password is now rejected instead of silently accepted.

#### Security
- Webhook delivery now resolves and validates the destination host once and connects to that exact address, closing a DNS-rebinding path that could otherwise redirect a webhook to an internal service.
- DNS lookups performed during webhook URL validation now run with a bounded timeout off the main request path, so a slow or unresponsive domain can no longer stall the server.
- Tor/onion request detection now requires the request to actually originate from the local Tor proxy, so a spoofed Host header can no longer bypass login lockout, per-account rate limiting, or session binding.

### Security Acknowledgements

- Thanks to independent security researcher sbekk for responsibly
  disclosing security issues and providing detailed reproduction steps
  and recommendations.
---

## [0.0.8] 2026-08 — App 0.0.8

### API
_No API changes._

### App
#### Added
- Account number is now stored encrypted, not just hashed

#### Changed
- Arabic (RTL) layout now fully mirrors the sidebar and dashboard, not just text direction
- Shortened toast/status messages and updated translations
- Removed the "Network" label from the checkout page — no longer shown
- Removed a duplicate "USD" label next to the fiat value on checkout

#### Fixed
- FIRO amounts showing 8 trailing decimal zeros (e.g. "0.05500000") on the checkout page's partially-paid summary
- Webhook URL wasn't validated or saved when left empty, despite showing a success message
- Language/currency switcher popup wasn't closing automatically after a selection
- `database is locked` errors from the Spark payment scanner colliding with other writes

#### Security
- Reduced risk of secrets (RPC credentials, encryption keys, session data) leaking into server logs

---

## [0.0.7] 2026-08 — App 0.0.7

### API
_No API changes._

### App
#### Changed
- Login rate limit raised (20 → 60 requests/min per IP) so shared or NAT'd IPs behind a proxy aren't blocked; brute-force protection is unaffected since it's enforced separately per-account

#### Fixed
- Checkout page could stay on "waiting" after a payment was actually confirmed until the page was refreshed, if confirmation landed at the exact moment the live status connection opened

#### Security
- Wallet sign-in now uses the same rate limiting as every other authentication method
- Hardened webhook URL validation to apply consistently across all webhook event types
- Startup now warns if the database field-encryption salt is left at its default value, so operators can rotate it deliberately

---

## [0.0.6] 2026-07 — API v1.0

### API
_No API changes._

### App
#### Added
- Private Spark payments — connect a Spark view key instead of pushing pre-derived addresses; checkout addresses are derived offline per payment, no address pool to exhaust
- "Private (Spark)" badge on checkout

---

## [0.0.5] 2026-07 — API v1.0

### API
_No API changes._

### App
#### Added
- Scoped API key permissions
- Resend email provider support

#### Changed
- Rate limits raised across the board
- About page comparison section moved to its own page

#### Fixed
- Error when connecting a wallet in public mode
- API key permission panel errors on wildcard keys

---

## [0.0.1] 2026 — API v1.0

### API
#### Added
- `POST /api/payments/create`, payment status polling and tracking
- Webhook delivery with HMAC-SHA256 signatures, timestamp replay protection, and auto-retry
- Multi API key system, revokable

### App
#### Added
- Unique receiving address per payment
- Merchant dashboard with analytics and payment history
- Realtime checkout updates
- Payment Links — shareable no-code payment pages
- Checkout themes with live preview
- Multi-language support including Arabic RTL
- Tor hidden service support
- Docker deployment
- TOTP 2FA
- Firebase Google authentication (optional)

#### Security
- API keys stored as SHA-256 hashes only
- Checkout URLs include HMAC tokens
- PBKDF2 encryption for sensitive database fields
- bcrypt password hashing
