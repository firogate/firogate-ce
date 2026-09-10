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

```
-----BEGIN PGP PUBLIC KEY BLOCK-----

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
eA==
=fV6S
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
