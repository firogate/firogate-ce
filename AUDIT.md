# Security Audit

This document summarizes FiroGate's security model and trust boundaries. It is intended for merchants, auditors, and security researchers.

---

# Overview

FiroGate is a self-hosted payment gateway for Firo Spark payments.

Its purpose is simple:
- Verify incoming payments.
- Notify merchant systems.
- Never take custody of merchant funds.
Funds always move directly between the customer and the merchant's own Spark wallet.

---

# Security Model

FiroGate is designed around a non-custodial architecture.

It can:
- Create payment requests.
- Verify Spark payments.
- Send signed webhooks.
- Store configuration securely.

It cannot:
- Spend merchant funds.
- Hold merchant funds.
- Generate private keys.
- Recover wallets.
---

# Wallet Connection

Merchants connect their wallet using a Spark View Key. The View Key provides read-only visibility into incoming Spark payments. It cannot authorize transactions or spend funds. Sensitive wallet information is encrypted while stored.

---

# Payment Verification

For every payment:
1. A unique Spark receiving address is created.
2. The customer pays directly to the merchant.
3. FiroGate verifies the payment.
4. The merchant receives a signed webhook notification.

FiroGate is never part of the money flow.

---

# Confirmation Model

FiroGate settles a payment using exactly one signal: **block confirmation depth** how many blocks have been mined on top of the block containing the payment. Nothing else is authoritative.

- **Confirmation Policy** — how many confirmations an invoice needs before it counts as complete. Configurable per merchant (0 / 1 / 3 / 6, labeled Instant / Fast / Standard / High Security in the dashboard). This is the only setting that changes what "confirmed" means.
- **`blocknotify`** wakes the payment scanner the instant a new block arrives, instead of waiting for the next ~20s poll. This only reduces how long it takes FiroGate to *notice* a confirming block; it never changes how confirmations are counted or how many are required.
- **ChainLocks** Firo's masternode-quorum mechanism that finalizes a block against reorganization. FiroGate does not check ChainLocks directly, because it doesn't need to: they're enforced by Firo Core's own consensus rules, which is exactly why counting ordinary block confirmations is a safe way to determine finality here.
- **InstantLock deliberately not used.** Firo's InstantSend can, in principle, lock a Spark transaction before it's mined. In practice this depends on a masternode quorum completing a vote in time, which is outside any merchant's or FiroGate's control, and (as of the Firo Core version this was evaluated against) a missed vote has no retry a perfectly legitimate payment can simply never receive an InstantLock. Because it isn't a reliable signal, FiroGate never uses InstantLock to gate or accelerate settlement, and its absence never delays a payment.

---

# API Security

API keys are:
- Randomly generated
- Stored as irreversible hashes
- Scope-based
- Individually revocable
---

# Authentication

Merchant accounts support:
- Secure password hashing
- Optional Two-Factor Authentication
- Secure session management
---

# Operator Controls

FiroGate CE is built for a single operator running their own instance the operator and the merchant are the same account. Administrative functions (node health, webhook diagnostics, system settings) require an authenticated operator account, set via the `OPERATOR_EMAILS` environment variable.

---

# Audit Logging

Every payment's full lifecycle (created, confirmed, expired, cancelled, webhook delivery attempts) is recorded in an append-only, payment-scoped audit log.

---


# What Should Be Verified

A security review should confirm that:
- Merchant funds cannot be spent by FiroGate.
- Private keys are never requested or stored.
- View Keys remain read-only.
- Payment verification behaves correctly.
- API authentication is enforced.
- Sensitive data is encrypted while stored.
- Payment audit logs accurately record each payment's lifecycle.

---

# Security Philosophy

FiroGate follows one principle:
> Verify payments without taking custody.
Merchant funds always remain under merchant control.
---

# Reporting Security Issues

If you discover a security issue, please report it responsibly.

**team@firogate.com**