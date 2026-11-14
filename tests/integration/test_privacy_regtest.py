"""
Live regtest test for transparent-to-Spark privacy tracking.

Runs only when FIROGATE_REGTEST=1 and firod is installed:

    FIROGATE_REGTEST=1 python -m unittest tests.integration.test_privacy_regtest -v

Set FIROGATE_RECORD=tests/fixtures/privacy_regtest.json to also record every
RPC response, which tests/test_privacy_replay.py replays without a node.

Node B plays the merchant's wallet with a usemnemonic=0 Firo Core wallet
(Firo Core refuses importprivkey in mnemonic wallets). Stack Wallet and
Campfire are not driven by this test.
"""
import json
import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from tests.integration import regtest
from tests import privacy_scenario as sc

if regtest.available():
    from app.core.xpub import Xpub
    from app.services import privacy_core as core
    from app.services.firo_rpc import get_rpc
    from app.services.privacy_watcher import run_pass
    from app.services.xpub_watcher import check_transparent_payments
    from app.core.database import AsyncSessionLocal


class RecordingRPC:
    def __init__(self, inner):
        self.inner = inner
        self.calls: dict[str, object] = {}

    async def call(self, method, *params):
        key = json.dumps([method, list(params)], sort_keys=True)
        try:
            res = await self.inner.call(method, *params)
        except Exception as e:
            self.calls[key] = {"__error__": str(e)[:200]}
            raise
        self.calls[key] = res
        return res


@unittest.skipUnless(regtest.available(), "set FIROGATE_REGTEST=1 with firod installed")
class PrivacyRegtestTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.net = regtest.Regtest(base_port=int(os.environ.get("FIROGATE_REGTEST_BASE_PORT", "28930")))
        self.net.__enter__()
        self.steps = []

    async def asyncTearDown(self):
        self.net.__exit__(None, None, None)

    async def step(self, name: str, reset_rows: bool = False) -> dict:
        if reset_rows:
            await sc.drop_rows()
        rec = RecordingRPC(get_rpc())
        async with AsyncSessionLocal() as db:
            await run_pass(rec, db)
        snap = await sc.snapshot()
        self.steps.append({"name": name, "reset_rows": reset_rows, "progress": await sc.payment_progress(),
                           "calls": rec.calls, "expect": snap})
        return snap

    def st(self, snap, label):
        return [r["state"] for r in snap.get(label, [])]

    async def test_full_lifecycle(self):
        a, b = self.net.a, self.net.b
        seed = regtest.MerchantSeed()
        key = Xpub.parse(seed.tpub)
        b.rpc("getnewsparkaddress")
        merchant_spark = b.rpc("getsparkdefaultaddress")[0]
        view_key = b.rpc("dumpsparkviewkey")
        other_spark = a.rpc("getnewsparkaddress")[0]

        tip = a.rpc("getblockcount")
        created = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(microsecond=0).isoformat()
        payments = [{"id": str(uuid.uuid4()), "label": f"inv{i}", "index": i, "address": key.address_at(i),
                     "amount": amt, "start_height": tip, "created_at": created}
                    for i, amt in enumerate([1.0, 2.0, 1.0, 3.0, 1.5, 0.8])]
        for i, p in enumerate(payments):
            b.rpc("importprivkey", seed.wif(i), p["label"], False)
            self.assertFalse(a.rpc("validateaddress", p["address"])["ismine"])
            self.assertTrue(b.rpc("validateaddress", p["address"])["ismine"])

        await sc.reset_db()
        await sc.setup_merchant(seed.tpub, view_key, payments)
        addr = {p["label"]: p["address"] for p in payments}

        paid = [a.rpc("sendtoaddress", addr[l], v) for l, v in
                [("inv0", 1.0), ("inv1", 2.0), ("inv2", 0.6), ("inv2", 0.4), ("inv3", 3.0), ("inv4", 1.5), ("inv5", 0.8)]]
        self.net.wait_mempool(paid)
        s = await self.step("paid_in_mempool")
        self.assertEqual(self.st(s, "inv2"), [core.PAYMENT_UNCONFIRMED] * 2)

        self.net.mine(1)
        await check_transparent_payments()
        s = await self.step("paid_confirmed")
        for label in addr:
            self.assertTrue(all(x == core.NOT_MINTED for x in self.st(s, label)), (label, self.st(s, label)))

        m1 = b.rpc("mintspark", {merchant_spark: {"amount": 1.0, "memo": ""}}, True, [addr["inv0"]])
        m2 = b.rpc("mintspark", {merchant_spark: {"amount": 3.0, "memo": ""}}, True, [addr["inv1"], addr["inv2"]])
        m3 = b.rpc("mintspark", {merchant_spark: {"amount": 1.0, "memo": ""}}, False, [addr["inv3"]])
        m4 = b.rpc("mintspark", {other_spark: {"amount": 0.8, "memo": ""}}, True, [addr["inv5"]])
        self.assertEqual(len(m2), 1, "one mintspark call over two invoices makes one transaction")
        self.net.wait_mempool(m1 + m2 + m3 + m4)
        s = await self.step("mints_in_mempool")
        self.assertEqual(self.st(s, "inv0"), [core.MINT_UNCONFIRMED])
        self.assertEqual(self.st(s, "inv1") + self.st(s, "inv2"), [core.MINT_UNCONFIRMED] * 3)
        self.assertEqual(self.st(s, "inv3"), [core.PARTLY_MINTED])
        self.assertEqual(self.st(s, "inv5"), [core.MINT_UNVERIFIED])
        self.assertEqual(self.st(s, "inv4"), [core.NOT_MINTED])

        self.net.mine(1)
        s = await self.step("mints_confirmed")
        self.assertEqual(self.st(s, "inv0"), [core.MINTED_VERIFIED])
        self.assertEqual(self.st(s, "inv1") + self.st(s, "inv2"), [core.MINTED_VERIFIED] * 3)
        self.assertEqual(self.st(s, "inv3"), [core.PARTLY_MINTED])
        self.assertEqual(self.st(s, "inv5"), [core.MINT_UNVERIFIED])
        self.assertEqual(s["inv5"][0]["minted_sats"], 0)
        self.assertGreater(s["inv5"][0]["unmatched_sats"], 0)

        tx2 = a.rpc("getrawtransaction", m2[0], 1)
        ins = sum(core.sats(i["value"]) for i in tx2["vin"])
        outs = sum(core.output_sats(o) for o in tx2["vout"])
        rows = s["inv1"] + s["inv2"]
        self.assertTrue(all(r["mint_txid"] == m2[0] for r in rows))
        self.assertLessEqual(abs(sum(r["fee_sats"] for r in rows) - (ins - outs)), len(rows))
        self.assertLessEqual(abs(sum(r["minted_sats"] for r in rows) - outs), len(rows))
        self.assertGreater(s["inv3"][0]["change_sats"], 0)

        m5 = b.rpc("mintspark", {merchant_spark: {"amount": 1.5, "memo": ""}}, True, [addr["inv4"]])
        self.net.wait_mempool(m5)
        s = await self.step("drop_seen")
        self.assertEqual(self.st(s, "inv4"), [core.MINT_UNCONFIRMED])
        b.rpc("setnetworkactive", False)
        self.net.restart_a_without_mempool()
        s = await self.step("drop_gone")
        self.assertEqual(self.st(s, "inv4"), [core.NOT_MINTED])
        b.rpc("setnetworkactive", True)
        self.net.connect()
        b.rpc("resendwallettransactions")
        self.net.wait_mempool(m5)
        s = await self.step("drop_rebroadcast")
        self.assertEqual(self.st(s, "inv4"), [core.MINT_UNCONFIRMED])
        self.net.mine(1)

        auto = b.rpc("automintspark")
        self.assertTrue(auto)
        self.net.wait_mempool(auto)
        self.net.mine(1)
        s = await self.step("change_minted")
        self.assertEqual(self.st(s, "inv4"), [core.MINTED_VERIFIED])
        self.assertEqual(self.st(s, "inv3"), [core.MINTED_VERIFIED])
        self.assertEqual(s["inv3"][0]["change_sats"], 0)
        self.assertEqual(s["inv3"][0]["change_hops"], 1)

        b.rpc("setnetworkactive", False)
        best = a.rpc("getbestblockhash")
        a.rpc("invalidateblock", best)
        s = await self.step("reorg")
        self.assertEqual(self.st(s, "inv3"), [core.MINT_UNCONFIRMED])
        a.rpc("reconsiderblock", best)
        s = await self.step("reorg_restored")
        self.assertEqual(self.st(s, "inv3"), [core.MINTED_VERIFIED])
        b.rpc("setnetworkactive", True)
        self.net.connect()

        self.net.mine(core.FINAL_DEPTH)
        s = await self.step("final")
        for label in ("inv0", "inv1", "inv2", "inv3", "inv4"):
            self.assertTrue(all(r["final"] for r in s[label]), label)
        self.assertFalse(s["inv5"][0]["final"])

        again = await self.step("repeat")
        self.assertEqual(again, s, "a repeated pass changes nothing")
        rebuilt = await self.step("rebuilt_from_chain", reset_rows=True)
        self.assertEqual(sc.states(rebuilt), sc.states(s))
        for label in rebuilt:
            for x, y in zip(rebuilt[label], s[label]):
                for f in ("minted_sats", "fee_sats", "change_sats", "mint_txid", "final"):
                    self.assertEqual(x[f], y[f], (label, f))

        db_path = os.environ["DATABASE_URL"].split("///", 1)[1]
        raw = open(db_path, "rb").read()
        for i in range(len(payments)):
            self.assertNotIn(seed.wif(i).encode(), raw)
        self.assertNotIn(b"tprv", raw)

        out = os.environ.get("FIROGATE_RECORD")
        if out:
            with open(out, "w") as f:
                json.dump({"tpub": seed.tpub, "view_key": view_key, "payments": payments, "steps": self.steps},
                          f, indent=1, sort_keys=True)


if __name__ == "__main__":
    unittest.main()
