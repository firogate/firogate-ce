from __future__ import annotations

import base64
import struct
from dataclasses import dataclass, field

FINAL_DEPTH = 6
MAX_CHANGE_HOPS = 2
SPARK_MINT_OPCODE = "d1"

PAYMENT_UNCONFIRMED = "payment_unconfirmed"
NOT_MINTED = "not_minted"
MINT_UNCONFIRMED = "mint_unconfirmed"
MINTED_VERIFIED = "minted_verified"
PARTLY_MINTED = "partly_minted"
MINT_UNVERIFIED = "mint_unverified"
SPENT_WITHOUT_MINT = "spent_without_mint"

STATES = (
    PAYMENT_UNCONFIRMED, NOT_MINTED, MINT_UNCONFIRMED, MINTED_VERIFIED,
    PARTLY_MINTED, MINT_UNVERIFIED, SPENT_WITHOUT_MINT,
)


def _compact_size(n: int) -> bytes:
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + struct.pack("<H", n)
    if n <= 0xFFFFFFFF:
        return b"\xfe" + struct.pack("<I", n)
    return b"\xff" + struct.pack("<Q", n)


def serial_context(vin: list[dict]) -> bytes:
    out = bytearray()
    for i in vin:
        if "txid" not in i:
            raise ValueError("coinbase or non-standard input")
        out += bytes.fromhex(i["txid"])[::-1]
        out += struct.pack("<I", int(i["vout"]))
        out += _compact_size(0)
        out += struct.pack("<I", int(i.get("sequence", 0xFFFFFFFF)))
    return bytes(out)


def sats(value) -> int:
    return int(round(float(value) * 1e8))


def output_sats(o: dict) -> int:
    if "valueSat" in o:
        return int(o["valueSat"])
    return sats(o.get("value", 0))


def mint_outputs(tx: dict) -> list[tuple[int, int, str]]:
    res = []
    for o in tx.get("vout") or []:
        h = (o.get("scriptPubKey") or {}).get("hex") or ""
        if h.startswith(SPARK_MINT_OPCODE) and len(h) > 2:
            res.append((int(o["n"]), output_sats(o), base64.b64encode(bytes.fromhex(h[2:])).decode()))
    return res


def transparent_outputs(tx: dict) -> list[tuple[int, int, str | None]]:
    res = []
    for o in tx.get("vout") or []:
        spk = o.get("scriptPubKey") or {}
        h = spk.get("hex") or ""
        if h[:2] in ("d1", "d2", "d3"):
            continue
        v = output_sats(o)
        if v <= 0:
            continue
        addrs = spk.get("addresses") or ([spk["address"]] if spk.get("address") else [])
        res.append((int(o["n"]), v, addrs[0] if len(addrs) == 1 else None))
    return res


def share(amount: int, part: int, whole: int) -> int:
    if whole <= 0 or amount <= 0 or part <= 0:
        return 0
    return amount * part // whole


@dataclass
class SpendSummary:
    txid: str
    confirmations: int
    input_total: int
    output_total: int
    fee: int
    mint_total: int
    mint_matched: int
    mint_unmatched: int
    coin_tags: list[str]
    transparent: list[tuple[int, int, str | None]]

    @property
    def has_mint(self) -> bool:
        return self.mint_total > 0 or self.mint_matched > 0 or self.mint_unmatched > 0


def summarize_spend(tx: dict, input_total: int, matches: dict[int, tuple[int, str] | None]) -> SpendSummary:
    mints = mint_outputs(tx)
    matched = sum(matches[n][0] for n, _, _ in mints if matches.get(n))
    unmatched = sum(v for n, v, _ in mints if not matches.get(n))
    tags = sorted(matches[n][1] for n, _, _ in mints if matches.get(n))
    out_total = sum(output_sats(o) for o in tx.get("vout") or [])
    return SpendSummary(
        txid=tx["txid"],
        confirmations=int(tx.get("confirmations") or 0),
        input_total=input_total,
        output_total=out_total,
        fee=max(0, input_total - out_total),
        mint_total=sum(v for _, v, _ in mints),
        mint_matched=matched,
        mint_unmatched=unmatched,
        coin_tags=tags,
        transparent=transparent_outputs(tx),
    )


@dataclass
class MintResult:
    spend_txid: str | None = None
    spend_confirmations: int = 0
    first_has_mint: bool = False
    mint_txid: str | None = None
    minted_sats: int = 0
    unmatched_sats: int = 0
    fee_sats: int = 0
    change_sats: int = 0
    change_outpoint: str | None = None
    change_open: bool = False
    hops: int = 0
    min_confirmations: int | None = None
    coin_tags: list[str] = field(default_factory=list)

    def see(self, conf: int) -> None:
        self.min_confirmations = conf if self.min_confirmations is None else min(self.min_confirmations, conf)


def apply_spend(res: MintResult, summary: SpendSummary, own_sats: int, first: bool) -> list[tuple[int, int, str | None]]:
    if first:
        res.spend_txid = summary.txid
        res.spend_confirmations = summary.confirmations
        res.first_has_mint = summary.has_mint
    res.see(summary.confirmations)
    if summary.has_mint and res.mint_txid is None:
        res.mint_txid = summary.txid
    res.minted_sats += share(summary.mint_matched, own_sats, summary.input_total)
    res.unmatched_sats += share(summary.mint_unmatched, own_sats, summary.input_total)
    res.fee_sats += share(summary.fee, own_sats, summary.input_total)
    for t in summary.coin_tags:
        if t not in res.coin_tags:
            res.coin_tags.append(t)
    return [(n, share(v, own_sats, summary.input_total), a) for n, v, a in summary.transparent]


def decide(credit_found: bool, credit_confirmations: int, res: MintResult | None) -> tuple[str, bool]:
    unspent = res is None or res.spend_txid is None
    if not credit_found or (unspent and credit_confirmations < 1):
        return PAYMENT_UNCONFIRMED, False
    if unspent:
        return NOT_MINTED, False
    deep = (res.min_confirmations or 0) >= FINAL_DEPTH
    if not res.first_has_mint:
        return SPENT_WITHOUT_MINT, deep
    if res.minted_sats == 0:
        return MINT_UNVERIFIED, False
    if res.change_sats > 0:
        return PARTLY_MINTED, deep and not res.change_open
    if (res.min_confirmations or 0) < 1:
        return MINT_UNCONFIRMED, False
    return MINTED_VERIFIED, deep
