# Contributing to FiroGate Community Edition

Thank you for your interest in contributing. FiroGate is open-source and welcomes community improvements.

---

## What You Can Contribute

- Bug fixes
- UI/UX improvements
- New language translations
- Documentation improvements
- Performance improvements
- Security improvements (see [SECURITY.md](SECURITY.md) for reporting vulnerabilities)

---

## Development Setup

### Prerequisites

- Python 3.11+
- Git

### 1. Fork and clone

```bash
git clone https://github.com/firogate/firogate-ce.git
cd firogate
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with development values
```

### 3. Run

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

---

## What You Can Work On

| Area | Files | Notes |
|------|-------|-------|
| UI / Templates | `templates/` | No node required |
| Frontend JS/CSS | `static/` | No node required |
| API routes | `app/api/` | No node required |
| Data models | `app/models/` | No node required |
| Webhooks | `app/services/webhook.py` | No node required |
| Translations | `static/i18n/` | JSON files — see below |
| Documentation | `*.md` | Always welcome |
| Firo RPC | `app/services/firo_rpc.py` | Requires synced Firo Core node |

---

## Workflow

1. Fork the repository
2. Create a branch: `git checkout -b feature/your-feature`
3. Make your changes
4. Test locally
5. Open a Pull Request with a clear description

---

## Rules

- Never commit `.env` or any file containing credentials
- Never commit real API keys, secrets, or wallet data
- Keep PRs focused — one feature or fix per PR
- Write clear commit messages
- All PRs are reviewed before merge

---

## Code Style

- Python: follow PEP 8
- Keep functions small and focused
- Add comments for non-obvious logic
- No print() in production code — use loguru logger

---

## Questions?

Open a GitHub issue or contact: contribute@firogate.com

---

## Adding a New Language

FiroGate supports multiple languages. Adding a new one takes about 30 minutes.

### Current Languages

| Code | Language | Direction |
|------|----------|-----------|
| `en` | English | LTR |
| `ar` | Arabic | RTL |
| `ru` | Russian | LTR |
| `de` | German | LTR |
| `zh` | Chinese (Simplified) | LTR |

### Steps

**1. Create the translation file**

Copy the English file as your base:

```bash
cp static/i18n/en.json static/i18n/fr.json
```

Translate all values (keep the keys in English):

```json
{
  "Cancel": "Annuler",
  "Confirm": "Confirmer",
  "Loading…": "Chargement…"
}
```

> Only translate the **values**, never the keys.

**2. Register the language in `i18n.js`**

Open `static/js/i18n.js` and add your language to the `LANGS` array:

```js
var LANGS = [
  { code: 'en', name: 'English', native: 'English',  flag: '🇬🇧', dir: 'ltr' },
  { code: 'ar', name: 'Arabic',  native: 'العربية',   flag: '🇸🇦', dir: 'rtl' },
  // Add your language here:
  { code: 'fr', name: 'French',  native: 'Français',  flag: '🇫🇷', dir: 'ltr' },
];
```

For RTL languages (Arabic, Hebrew, Persian, Urdu), set `dir: 'rtl'` and add the code to the `RTL` object:

```js
var RTL = { ar: 1, he: 1 };
```

**3. Register on the server**

Open `app/core/i18n.py` and add your language to `LANG_META`:

```python
LANG_META = {
    "en": {"name": "English",  "native": "English",  "flag": "🇬🇧", "dir": "ltr"},
    "ar": {"name": "Arabic",   "native": "العربية",   "flag": "🇸🇦", "dir": "rtl"},
    # Add here:
    "fr": {"name": "French",   "native": "Français",  "flag": "🇫🇷", "dir": "ltr"},
}
```

**4. Test**

Start the server and use the language switcher. Verify:
- All strings translate correctly
- Layout looks correct
- For RTL: text aligns right, buttons flip correctly

**5. Submit a PR**

Include:
- `static/i18n/fr.json` — your translation file
- Updated `static/js/i18n.js` — LANGS array
- Updated `app/core/i18n.py` — LANG_META

Translation PRs are always welcome!

### Translation Tips

- Keep translations natural — don't translate word by word
- Technical terms (`FIRO`, `API`, `HMAC`) stay in English
- Currency amounts and addresses are never translated
- If unsure about a string, leave it in English and note it in your PR
