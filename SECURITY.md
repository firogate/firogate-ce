# Security Policy — FiroGate Community Edition

## Reporting a Vulnerability

If you discover a security vulnerability, **do NOT open a public GitHub issue.**

Please report it responsibly:

**Email:** security@firogate.com  
**Subject:** `[SECURITY] Brief description`

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Your suggested fix (optional)

We will acknowledge your report within **48 hours** and aim to release a fix within **7 days** for critical issues.

We appreciate responsible disclosure and will credit researchers who report valid vulnerabilities.

---

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest  | ✅ Yes    |
| Older   | ❌ No     |

Always run the latest release.

---

## Security Architecture

### API Keys
- Raw keys are **never stored** — only SHA-256 hashes
- Keys are shown **once** on creation and cannot be retrieved again
- Keys can be revoked instantly from the dashboard
- Format: `fg_live_` prefix for easy identification

### Webhook Signatures
- Every webhook is signed with **HMAC-SHA256**
- Signature is included in the `X-FiroGate-Signature` header
- Includes a timestamp to prevent replay attacks (5-minute window)
- Always verify signatures on your server before processing events

### Checkout Tokens
- Every checkout URL includes an **HMAC security token**
- Prevents unauthorized access to payment status even with a guessed payment ID
- Tokens are single-use scoped to the payment

### Encryption at Rest
- Sensitive fields (webhook secrets) are encrypted using **PBKDF2-SHA256**
- Encryption key is set via `FIELD_ENCRYPTION_KEY` in `.env`
- If this key is lost, encrypted data cannot be recovered — back it up

### Authentication
- Passwords are hashed with **bcrypt**
- Optional **TOTP 2FA** for merchant accounts
- Per-IP rate limiting on all auth endpoints
- Account lockout after repeated failed attempts

### Rate Limiting
- Per-IP rate limiting on all API endpoints
- SSE connection limits per IP
- Configurable via `.env`

### Withdrawal Security
- Optional **address whitelist** — restrict withdrawals to trusted addresses only
- Minimum hold period before funds can be withdrawn
- Daily withdrawal limits

---

## Hardening Recommendations

For production deployments:

```env
# Require strong secrets
SECRET_KEY=<64-char random hex>
FIELD_ENCRYPTION_KEY=<32-byte base64>

# Restrict admin access
PANEL_ALLOWED_IPS=your.server.ip
PANEL_REQUIRE_2FA=true

# Rate limiting
TRUST_PROXY_HEADERS=true  # only if behind nginx/Cloudflare
```

**nginx:** always use HTTPS. Never expose port 8000 directly.

**Firewall:** only expose ports 80 and 443. Block 8000 externally.

**Backups:** regularly back up `data/gateway.db` and `.env`.

---

## Known Security Considerations

- FiroGate holds funds in the gateway wallet until merchants request withdrawal — this is custodial in nature for hosted deployments
- In self-hosted mode, the operator controls the server and the wallet
- Never run FiroGate with `DEBUG=true` in production
- Never commit `.env` to version control

---

## Scope

Security reports are accepted for:
- Authentication and authorization flaws
- Cryptographic weaknesses
- Injection vulnerabilities (SQL, XSS, SSTI)
- Webhook signature bypass
- Checkout token bypass
- Sensitive data exposure

Out of scope:
- Issues requiring physical access to the server
- Social engineering attacks
- Vulnerabilities in third-party dependencies (report to them directly)
