# Spark mint tracking

FiroGate can show what happened to a transparent payment after it arrived: still transparent, minted to Spark by your own wallet, or spent some other way.

FiroGate stays non-custodial. It never asks for, stores or uses a private key or seed phrase. It never mints, signs or broadcasts anything. You make the mint in your own wallet; FiroGate only reads the chain and checks the result with the Spark view key you already connected.

## What you need

* A transparent payment method (wallet xpub) **and** a Spark view key, both from **the same wallet**. A mint to a different wallet shows as "Minted, no key match".
* Your Firo node with these in `firo.conf` (restart with `-reindex` after adding an index):

```
addressindex=1
txindex=1
```

The Spark mints page shows "Needs addressindex=1 and txindex=1" when the node cannot answer.

## How to mint (Stack Wallet and Campfire)

1. A customer pays an invoice to its transparent address.
2. Wait for at least 1 confirmation. Your wallet needs it before it can mint.
3. Open the wallet and tap **Privatize funds**. The wallet mints all its public funds to its own Spark address, one transaction per address.
4. FiroGate finds the mint by the exact coins it spent and checks the new Spark coin with your view key.

Campfire is built from the Stack Wallet code (`scripts/app_config/configure_campfire.sh` in the Stack Wallet repository), so the steps are the same.

Firo Core can also mint (`mintspark`, `automintspark`, or the desktop "Make Private" prompt), and FiroGate detects those mints. Firo Core cannot export an xpub and refuses `importprivkey` in mnemonic wallets, so it is not a practical wallet for xpub invoices today.

## States

Payment status and mint status are separate. Mint tracking never changes a payment's status, amount, confirmations or webhooks.

| State | Meaning |
|---|---|
| Payment confirming | The customer's payment is not in a block yet. |
| Not minted | The coin is still transparent at the invoice address. |
| Mint seen | A mint spent the coin; the mint is not in a block yet. |
| Minted, key match | A mint spent the coin and your view key matched the new Spark coin. |
| Partly minted | Only part was minted; the rest went to a transparent output. FiroGate follows that change for up to two more transactions when it is a single output. |
| Minted, no key match | A mint spent the coin but your view key did not match its Spark output. Usually a different wallet. |
| Spent, no mint | The coin was spent by a normal transaction. |

A row stops being re-checked once everything it depends on has 6 confirmations. Until then each pass rebuilds it from the chain, so dropped transactions, node restarts and reorgs move it back and forward on their own.

## What a key match proves

It proves the Spark coin was sent to an address of the wallet whose view key you connected, and gives its value and coin tag.

It does **not** prove who holds the spend key, that the coin is still unspent, or that the funds are unlinkable. A mint's amount and its transparent inputs are public on the chain.

## Fees

The fee of a mint is its inputs minus its outputs. When one mint spends coins from several invoices, each coin gets a share in proportion to its value. Shares round down, so they can be a few sats under the real total.

## Validation level

* Tested end to end on a private Firo **regtest** network (Firo Core v0.14.18.2), with Firo Core playing the merchant's wallet: full mint, one mint spending two invoices, partial mint with change followed into a later mint, a mint to another wallet, a dropped mint with a node restart, a reorg, finality, repeated passes and a full rebuild from the chain.
* Stack Wallet and Campfire have **not** been tested live, on regtest or on public testnet. Their behaviour above comes from reading their source code.
* Public testnet has not been tested.

## Known limits

* **Gap limit (not solved).** Stack Wallet and Campfire stop scanning after 50 unused addresses in a row. FiroGate gives every invoice a new address and never reuses one, even when the invoice expires unpaid. After more than 50 unpaid invoices in a row, a later payment may not show in your wallet until you rescan with a larger gap, so it cannot be minted from there. FiroGate does not reuse addresses to work around this, because a late payment to a reused address could be credited to the wrong invoice.
* Late payments to an invoice that already expired are not tracked, the same as for payments.
* Change is only followed after a mint, only when the mint left a single transparent output, and for at most two more transactions.

## API

All endpoints need a signed-in merchant or an API key with the listed permission.

| Method | Path | Permission |
|---|---|---|
| GET | `/api/privacy/status` | `read_invoice` |
| GET | `/api/privacy/inputs?offset=0&limit=25&state=` | `read_invoice` |
| GET | `/api/privacy/payments/{payment_id}` | `read_invoice` |
| PATCH | `/api/privacy/settings` `{"tracking_enabled": true}` | `wallet_connect` |

## Tests

```bash
python -m unittest discover -s tests -t .
```

The live regtest test needs `firod` and `firo-cli` installed:

```bash
FIROGATE_REGTEST=1 python -m unittest tests.integration.test_privacy_regtest -v
```

Add `FIROGATE_RECORD=tests/fixtures/privacy_regtest.json` to refresh the recorded run that the replay test uses in CI.
