import base64
import json
import os
import unittest

from app.services import mint_core as core

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "mint_regtest.json")


def tx(txid, vin, vout, confirmations=0):
    return {"txid": txid, "vin": vin, "vout": vout, "confirmations": confirmations}


def mint_out(n, value_sats, payload="00aa"):
    return {"n": n, "value": value_sats / 1e8, "scriptPubKey": {"hex": "d1" + payload, "type": "sparkmint"}}


def p2pkh_out(n, value_sats, addr="TAddr"):
    return {"n": n, "value": value_sats / 1e8, "scriptPubKey": {"hex": "76a914" + "00" * 20 + "88ac", "addresses": [addr]}}


class SerialContextTest(unittest.TestCase):
    def test_layout(self):
        vin = [{"txid": "11" * 32, "vout": 1, "sequence": 4294967294},
               {"txid": "ab" + "00" * 31, "vout": 0, "sequence": 4294967295}]
        ctx = core.serial_context(vin)
        self.assertEqual(len(ctx), 2 * (32 + 4 + 1 + 4))
        self.assertEqual(ctx[:32], bytes.fromhex("11" * 32))
        self.assertEqual(ctx[32:36], (1).to_bytes(4, "little"))
        self.assertEqual(ctx[36], 0)
        self.assertEqual(ctx[41:73], bytes.fromhex("ab" + "00" * 31)[::-1])

    def test_coinbase_rejected(self):
        with self.assertRaises(ValueError):
            core.serial_context([{"coinbase": "00"}])


class OutputsTest(unittest.TestCase):
    def test_mint_and_transparent_split(self):
        t = tx("aa" * 32, [], [mint_out(0, 100), p2pkh_out(1, 50), {"n": 2, "value": 0,
               "scriptPubKey": {"hex": "6a00"}}])
        self.assertEqual([(n, v) for n, v, _ in core.mint_outputs(t)], [(0, 100)])
        self.assertEqual(core.mint_outputs(t)[0][2], base64.b64encode(bytes.fromhex("00aa")).decode())
        self.assertEqual(core.transparent_outputs(t), [(1, 50, "TAddr")])

    def test_share_rounds_down(self):
        self.assertEqual(core.share(7760, 200_000_000, 300_000_000), 5173)
        self.assertEqual(core.share(7760, 60_000_000, 300_000_000), 1552)
        self.assertEqual(core.share(7760, 40_000_000, 300_000_000), 1552 * 40 // 60)
        self.assertEqual(core.share(10, 0, 100), 0)
        self.assertEqual(core.share(10, 5, 0), 0)


class SpendTest(unittest.TestCase):
    def test_multi_invoice_mint_shares(self):
        t = tx("bb" * 32, [], [mint_out(0, 299_992_240)], confirmations=1)
        s = core.summarize_spend(t, 300_000_000, {0: (299_992_240, "tag1")})
        self.assertEqual(s.fee, 7760)
        totals = {"fee": 0, "minted": 0}
        for part in (200_000_000, 60_000_000, 40_000_000):
            tr = core.MintResult()
            core.apply_spend(tr, s, part, first=True)
            totals["fee"] += tr.fee_sats
            totals["minted"] += tr.minted_sats
        self.assertLessEqual(7760 - totals["fee"], 3)
        self.assertLessEqual(299_992_240 - totals["minted"], 3)

    def test_unmatched_mint_is_not_counted_as_minted(self):
        t = tx("cc" * 32, [], [mint_out(0, 79_995_200)], confirmations=3)
        s = core.summarize_spend(t, 80_000_000, {0: None})
        tr = core.MintResult()
        core.apply_spend(tr, s, 80_000_000, first=True)
        self.assertEqual(tr.minted_sats, 0)
        self.assertEqual(tr.unmatched_sats, 79_995_200)
        self.assertEqual(core.decide(True, 5, tr), (core.MINT_UNVERIFIED, False))

    def test_partial_mint_keeps_change(self):
        t = tx("dd" * 32, [], [p2pkh_out(0, 199_994_860), mint_out(1, 100_000_000)], confirmations=2)
        s = core.summarize_spend(t, 300_000_000, {1: (100_000_000, "t")})
        tr = core.MintResult()
        outs = core.apply_spend(tr, s, 300_000_000, first=True)
        tr.change_sats = sum(v for _, v, _ in outs)
        tr.change_open = True
        self.assertEqual(tr.fee_sats, 5140)
        self.assertEqual(core.decide(True, 3, tr), (core.PARTLY_MINTED, False))
        tr.change_open = False
        tr.min_confirmations = core.FINAL_DEPTH
        self.assertEqual(core.decide(True, 9, tr), (core.PARTLY_MINTED, True))


class DecideTest(unittest.TestCase):
    def test_states(self):
        self.assertEqual(core.decide(False, 0, None), (core.PAYMENT_UNCONFIRMED, False))
        self.assertEqual(core.decide(True, 0, core.MintResult()), (core.PAYMENT_UNCONFIRMED, False))
        self.assertEqual(core.decide(True, 1, core.MintResult()), (core.NOT_MINTED, False))
        other = core.MintResult(spend_txid="x", first_has_mint=False, min_confirmations=2)
        self.assertEqual(core.decide(True, 3, other), (core.SPENT_WITHOUT_MINT, False))
        other.min_confirmations = core.FINAL_DEPTH
        self.assertEqual(core.decide(True, 9, other), (core.SPENT_WITHOUT_MINT, True))
        pending = core.MintResult(spend_txid="x", first_has_mint=True, minted_sats=5, min_confirmations=0)
        self.assertEqual(core.decide(True, 3, pending), (core.MINT_UNCONFIRMED, False))
        pending.min_confirmations = 1
        self.assertEqual(core.decide(True, 3, pending), (core.MINTED_VERIFIED, False))
        pending.min_confirmations = core.FINAL_DEPTH
        self.assertEqual(core.decide(True, 9, pending), (core.MINTED_VERIFIED, True))

    def test_missing_credit_reverts(self):
        tr = core.MintResult(spend_txid="x", first_has_mint=True, minted_sats=5, min_confirmations=9)
        self.assertEqual(core.decide(False, 0, tr), (core.PAYMENT_UNCONFIRMED, False))


@unittest.skipUnless(os.path.exists(FIXTURE), "fixture not recorded")
class ViewKeyFixtureTest(unittest.TestCase):

    def test_identify_with_rebuilt_context(self):
        from app.services.sparkmobile import SparkViewKey
        data = json.load(open(FIXTURE))
        vk = SparkViewKey(data["view_key"], is_testnet=True)
        txs = {}
        for step in data["steps"]:
            for key, res in step["calls"].items():
                method, params = json.loads(key)
                if method == "getrawtransaction" and isinstance(res, dict) and "vout" in res:
                    txs[res["txid"]] = res
        expect = data["steps"][-1]["expect"]
        mine = {r["mint_txid"] for label in ("inv0", "inv1", "inv2", "inv4") for r in expect[label]}
        other = {r["mint_txid"] for r in expect["inv5"]}
        self.assertTrue(mine and other)
        for txid in mine | other:
            t = txs[txid]
            ctx = base64.b64encode(core.serial_context(t["vin"])).decode()
            for n, value, coin in core.mint_outputs(t):
                found = vk.identify_coin(coin, ctx)
                if txid in mine:
                    self.assertIsNotNone(found, txid)
                    self.assertEqual(int(found.value), value)
                else:
                    self.assertIsNone(found, txid)
            if txid in mine:
                wrong = base64.b64encode(core.serial_context(list(reversed(t["vin"]))) + b"\x00").decode()
                for n, value, coin in core.mint_outputs(t):
                    self.assertIsNone(vk.identify_coin(coin, wrong))


if __name__ == "__main__":
    unittest.main()
