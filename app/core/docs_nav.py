DOCS_NAV = [
    ("Get started", [
        ("introduction", "Introduction"),
        ("quickstart", "Quickstart"),
        ("authentication", "Authentication"),
        ("errors", "Errors"),
        ("self-hosting", "Self-hosting"),
    ]),
    ("Payments", [
        ("create-payment", "Create a payment"),
        ("get-payment", "Get a payment"),
        ("list-payments", "List payments"),
        ("reports", "Summary and export"),
        ("checkout-status", "Checkout status"),
        ("cancel-payment", "Cancel a payment"),
        ("metadata", "Metadata"),
    ]),
    ("Webhooks", [
        ("webhooks", "Events"),
        ("webhook-setup", "Set up your endpoint"),
        ("verify-webhook", "Verify signatures"),
    ]),
    ("Wallet and money", [
        ("payment-methods", "Payment methods"),
        ("refunds", "Refunds"),
        ("recurring", "Recurring payments"),
        ("spark-mints", "Spark mints"),
    ]),
    ("Code examples", [
        ("curl", "cURL"),
        ("python", "Python"),
        ("javascript", "JavaScript"),
        ("php", "PHP"),
        ("go", "Go"),
        ("java", "Java"),
        ("csharp", "C#"),
        ("ruby", "Ruby"),
        ("rust", "Rust"),
    ]),
    ("Guides", [
        ("marketplace", "Marketplace"),
        ("telegram-bot", "Telegram bot"),
        ("tor-onion", "Tor and onion"),
    ]),
]

DOCS_HOME = "introduction"

DOCS_PAGES = {slug: title for _, items in DOCS_NAV for slug, title in items}

DOCS_LEGACY = {
    "overview": "introduction",
    "installation": "self-hosting",
    "auth": "authentication",
    "webhook": "webhooks",
    "webhook-guide": "webhook-setup",
}
