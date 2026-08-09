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
| Older Releases | ❌ Unsupported |

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

We appreciate responsible disclosure and may credit researchers who report valid security issues.

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