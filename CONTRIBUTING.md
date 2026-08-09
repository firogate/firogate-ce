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

# Before Submitting

Please verify that:

- Your code builds successfully.
- Existing functionality continues to work.
- No credentials or secrets are included.
- `.env` is not committed.
- Formatting is consistent.

---

Thank you for helping improve FiroGate.