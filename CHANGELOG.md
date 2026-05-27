# Changelog — FiroGate Community Edition

All notable changes to the Community Edition are documented here.

---

## [1.0.0] — 2026

### Added
- Initial public release of FiroGate Community Edition
- REST API for payment creation and status tracking
- HD Wallet engine — unique address per payment
- Merchant dashboard with analytics, withdrawals, payment history
- SSE realtime checkout updates — no polling
- Webhook delivery with HMAC-SHA256 signatures and auto-retry
- Multi API key system — SHA-256 hashed, revokable
- Payment Links — no-code shareable payment pages
- 7 checkout themes with live preview
- 9 language support including Arabic RTL
- Tor hidden service support
- Docker and docker-compose deployment
- 2FA TOTP for merchant accounts
- Withdrawal address whitelist
- Per-IP rate limiting
- Cloudflare Turnstile bot protection (optional)
- Firebase Google authentication (optional)

### Security
- API keys stored as SHA-256 hashes only — never raw
- Checkout URLs include HMAC tokens to prevent unauthorized access
- Webhook payloads include replay-attack protection via timestamp
- PBKDF2 encryption for sensitive database fields
- bcrypt password hashing

---

## Future

Community contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).
