# Contributing to FiroGate

Thank you for your interest in contributing to FiroGate. We welcome contributions that improve the project, whether it's fixing bugs, improving the user experience, enhancing documentation, or adding new features.

---

# Ways to Contribute

You can help by contributing:

- Bug fixes
- Performance improvements
- UI / UX improvements
- Documentation
- Translations
- Tests
- Security improvements (please see SECURITY.md for responsible disclosure)

---

# Development Setup

See [BUILD.md](BUILD.md) for install and configuration steps. For local development, run:

```bash
docker compose up -d --build
docker compose logs -f
```

After changing code, rebuild and restart to pick it up:

```bash
docker compose up -d --build
```

---

# Contribution Workflow

1. Fork the repository.
2. Create a new branch.

```bash
git checkout -b feature/my-feature
```

3. Make your changes.
4. Test locally.
5. Commit your work.
6. Open a Pull Request.

Please include a clear description of what your contribution changes and why.

---

# Pull Request Guidelines

Please keep Pull Requests:

- Small and focused
- Easy to review
- Related to a single feature or fix

Large PRs that change many unrelated things are difficult to review and may be asked to split into multiple submissions.

---

# Code Style

Please follow these general guidelines:

- Follow PEP 8 for Python
- Keep functions small and readable
- Prefer simple solutions
- Write comments only when they improve understanding
- Remove debugging code before submitting

---

# Translations

New translations are always welcome.

General guidelines:

- Translate values only.
- Never rename translation keys.
- Keep technical terms such as FIRO, Spark, API, JSON, RPC, HMAC, UUID, etc. unchanged.
- Test your translation before submitting.

---

# Security

If you discover a security issue, **do not** open a public GitHub issue.
Please follow the instructions in:

```
SECURITY.md
```

---
for contACT 

```-----BEGIN PGP PUBLIC KEY BLOCK-----
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
8D+aOdm2ocXv=mo6m
-----END PGP PUBLIC KEY BLOCK-----
```
                                    

# Before Submitting

Please verify that:

- Your code builds successfully.
- Existing functionality continues to work.
- No credentials or secrets are included.
- `.env` is not committed.
- Formatting is consistent.

---

Thank you for helping improve FiroGate.
