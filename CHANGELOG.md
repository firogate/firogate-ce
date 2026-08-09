# Changelog

Current: **API v1.0** · **App 0.0.7**

---

## [Unreleased] — App 0.0.8

### API
_No API changes._

### App
_Nothing yet._

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

#### Changed
- Plan pricing set by the operator no longer resets to defaults on server restart

---

## [0.0.5] 2026-07 — API v1.0

### API
_No API changes._

### App
#### Added
- Wallet Access Control — public, invite-only, or operator-approval modes
- Scoped API key permissions
- Operator approval workflow for new merchants
- Invite token system for invite-only mode
- Proof-of-ownership for wallet connect
- Emergency kill switches to block wallet connections or payment creation instance-wide
- Notification system with real-time push
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
