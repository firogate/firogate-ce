# Security Policy

Thank you for helping keep FiroGate secure.

## Reporting a Vulnerability

Please **do not** open public GitHub issues for security reports.

Contact:

**Email:** team@firogate.com

Suggested subject:

```
[SECURITY] Brief description
```

Please include:

- Description of the issue
- Steps to reproduce
- Potential impact
- Suggested fix (optional)

We acknowledge reports as quickly as possible and work to resolve verified security issues in a timely manner.

---

## Supported Versions

| Version | Status |
|---------|--------|
| Latest Release | ✅ Supported |
| Older Releases | ⚠️ Supported, but you should update via `git pull` |

Always use the latest stable release.

---

## Security Recommendations

When deploying FiroGate:

- Keep your server up to date.
- Use HTTPS in production.
- Do not expose internal services directly to the Internet.
- Generate unique secrets for every deployment.
- Never commit `.env` files.
- Restrict filesystem permissions for configuration files.
- Back up your database and configuration securely.

---

## Responsible Disclosure

Please allow us reasonable time to investigate and resolve reported vulnerabilities before making them public.

We appreciate responsible disclosure will work with researchers to understand and resolve valid security issues.

Researchers may be credited in future security acknowledgements or advisories with their consent.

## Security Research & Rewards

FiroGate does not currently operate a paid bug bounty programme.

Security research contributions are voluntary. We greatly appreciate researchers who choose to spend their time reviewing FiroGate, developing PoCs, and responsibly reporting security issues.

---

## Scope

Examples of issues we are interested in:

- Authentication or authorization bypass
- Injection vulnerabilities
- Sensitive data exposure
- Access control issues
- Cryptographic implementation flaws
- Remote code execution
- Denial of service caused by software defects

Out of scope:

- Social engineering
- Physical access attacks
- Third-party software vulnerabilities
- Server misconfiguration outside FiroGate itself
## Testing Guidelines

Please keep security testing limited to systems you own or are explicitly authorised to test.

Do not access, modify, delete, or expose other users' data.

Do not perform destructive testing or intentionally disrupt production services.

For sensitive research, PoCs, or communications, please use the [PGP public key](https://raw.githubusercontent.com/firogate/firogate-ce/main/pgp/team_public_key.asc).