import json
import os
import unittest

from tests import mint_scenario as sc
from app.core.database import AsyncSessionLocal
from app.services.mint_watcher import run_pass

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "mint_regtest.json")


class ReplayRPC:
    def __init__(self, calls: dict):
        self.calls = calls

    async def call(self, method, *params):
        key = json.dumps([method, list(params)], sort_keys=True)
        if key not in self.calls:
            raise RuntimeError(f"not recorded: {key[:120]}")
        res = self.calls[key]
        if isinstance(res, dict) and "__error__" in res:
            raise RuntimeError(res["__error__"])
        return res


@unittest.skipUnless(os.path.exists(FIXTURE), "fixture not recorded")
class MintReplayTest(unittest.IsolatedAsyncioTestCase):

    async def test_replay_matches_live_run(self):
        data = json.load(open(FIXTURE))
        await sc.reset_db()
        await sc.setup_merchant(data["tpub"], data["view_key"], data["payments"])
        for step in data["steps"]:
            await sc.set_payment_progress(step["progress"])
            if step["reset_rows"]:
                await sc.drop_rows()
            async with AsyncSessionLocal() as db:
                await run_pass(ReplayRPC(step["calls"]), db)
            self.assertEqual(await sc.snapshot(), step["expect"], step["name"])

    async def test_check_off_skips_merchant(self):
        from app.models.spark_mint import SparkMintSettings
        from sqlalchemy import select
        from app.models.models import User
        data = json.load(open(FIXTURE))
        await sc.reset_db()
        await sc.setup_merchant(data["tpub"], data["view_key"], data["payments"])
        async with AsyncSessionLocal() as db:
            uid = (await db.execute(select(User.id))).scalar_one()
            db.add(SparkMintSettings(merchant_id=uid, check_enabled=False))
            await db.commit()
        step = data["steps"][1]
        await sc.set_payment_progress(step["progress"])
        async with AsyncSessionLocal() as db:
            checked = await run_pass(ReplayRPC({}), db)
        self.assertEqual(checked, 0)
        self.assertEqual(await sc.snapshot(), {})


if __name__ == "__main__":
    unittest.main()
