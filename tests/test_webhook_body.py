import hashlib
import hmac
import json
import unittest

import httpx

from app.core.database import AsyncSessionLocal, create_tables
from app.core.security import encrypt_field, sign_webhook, webhook_body
from app.models.models import User
from app.services import webhook as wh

SECRET = "whsec_test_secret"
TRICKY = {
    "payment_id": "p1",
    "order_id": "ORD-1",
    "amount_firo": 5.0,
    "amount_received": 5.0,
    "confirmations": 2,
    "metadata": {"seller_id": "s1", "commission_pct": 12, "name": "قهوة", "a": {"z": 1, "b": 2}},
}


class WebhookBodyTest(unittest.IsolatedAsyncioTestCase):

    def test_body_is_what_gets_signed(self):
        payload = dict(TRICKY, nonce="ab", timestamp=1705312500)
        body = webhook_body(payload)
        self.assertEqual(hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest(), sign_webhook(payload, SECRET))
        self.assertEqual(json.loads(body), payload)
        self.assertIn(b'"amount_firo":5.0', body)

    async def test_event_webhook_sends_the_signed_bytes(self):
        await create_tables()
        async with AsyncSessionLocal() as db:
            user = User(username="wh-body-test", webhook_url="https://merchant.example/hook",
                        webhook_secret_enc=encrypt_field(SECRET))
            db.add(user)
            await db.commit()
            uid = user.id

        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = request.content
            seen["headers"] = request.headers
            return httpx.Response(200, json={"ok": True})

        async def passthrough(url):
            return url, {}, {}

        async def allow(url, label=""):
            return url

        import app.core.validators as validators
        old = (wh._build_client, wh._resolve_delivery, validators.validate_url)
        wh._build_client = lambda url: httpx.AsyncClient(transport=httpx.MockTransport(handler))
        wh._resolve_delivery = passthrough
        validators.validate_url = allow
        try:
            await wh.fire_event_webhook(uid, "payment.refunded", dict(TRICKY))
        finally:
            wh._build_client, wh._resolve_delivery, validators.validate_url = old
            from app.core.database import engine
            await engine.dispose()

        self.assertIn("body", seen, "webhook was not sent")
        body = seen["body"]
        expected = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        self.assertEqual(seen["headers"]["x-firogate-signature"], expected)
        self.assertEqual(seen["headers"]["content-type"], "application/json")
        data = json.loads(body)
        self.assertEqual(data["event"], "payment.refunded")
        self.assertEqual(data["metadata"], TRICKY["metadata"])
        self.assertEqual(sign_webhook(data, SECRET), expected)


if __name__ == "__main__":
    unittest.main()
