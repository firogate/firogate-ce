"""
Firo RPC client.
Checkout addresses are derived offline from each merchant's Spark view key
(see app/services/payment_engine.py) this client just talks to the node
for chain state, price/balance reads, and manual tx-hash verification.
"""
import httpx
from loguru import logger
from typing import Any, Optional
from app.core.config import get_settings

settings = get_settings()


class FiroRPCError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"RPC Error {code}: {message}")


class FiroRPC:
    def __init__(self):
        logger.info(f"RPC → {settings.rpc_url}")
        self._id = 0
        kwargs: dict = {
            "base_url": settings.rpc_url,
            "auth":     (settings.FIRO_RPC_USER, settings.FIRO_RPC_PASSWORD),
            "timeout":  30.0,
            "headers":  {"Content-Type": "application/json"},
        }

        # Tor cannot route to 127.0.0.1 / localhost, so only proxy remote nodes.
        _localhost = {"127.0.0.1", "::1", "localhost"}
        _rpc_is_local = settings.FIRO_RPC_HOST in _localhost

        if settings.TOR_ENABLED and not _rpc_is_local:
            kwargs["transport"] = httpx.AsyncHTTPTransport(
                proxy=settings.tor_socks_url
            )
            logger.info(f"RPC via Tor SOCKS5 → {settings.tor_socks_url}")
        elif settings.TOR_ENABLED and _rpc_is_local:
            logger.info("RPC → localhost (direct, Tor not used for local node)")

        self._client = httpx.AsyncClient(**kwargs)

    async def call(self, method: str, *params) -> Any:
        self._id += 1
        try:
            resp = await self._client.post("", json={
                "jsonrpc": "1.1", "id": self._id,
                "method": method, "params": list(params),
            })
            if resp.status_code not in (200, 500):
                raise FiroRPCError(-1, f"HTTP {resp.status_code}")
            data = resp.json()
        except httpx.RequestError as e:
            raise FiroRPCError(-1, f"Cannot reach Firo node: {e}")
        if data.get("error"):
            e = data["error"]
            raise FiroRPCError(e.get("code", -1), e.get("message", "Unknown"))
        return data["result"]

    async def close(self): await self._client.aclose()
    async def ping(self) -> bool:
        try: await self.call("ping"); return True
        except: return False
    async def get_blockchain_info(self) -> dict: return await self.call("getblockchaininfo")
    async def get_block_count(self) -> int:      return await self.call("getblockcount")

    # Cached block height (10s TTL) avoids calling getblockcount once per
    # payment in the monitor loop.
    _bc_cache: dict = {"height": 0, "ts": 0.0}
    async def get_block_count_cached(self, ttl: float = 10.0) -> int:
        import time as _t
        now = _t.time()
        if now - self._bc_cache["ts"] < ttl and self._bc_cache["height"]:
            return self._bc_cache["height"]
        h = await self.get_block_count()
        self._bc_cache = {"height": h, "ts": now}
        return h
    async def get_network_info(self) -> dict:    return await self.call("getnetworkinfo")

    async def list_transactions(self, count: int = 500) -> list:
        try:
            return await self.call("listtransactions", "*", count, 0, True)
        except FiroRPCError:
            try: return await self.call("listtransactions", "*", count, 0)
            except FiroRPCError: return []

    async def get_transaction(self, txid: str) -> dict:
        return await self.call("gettransaction", txid, True)

    async def get_raw_transaction(self, txid: str) -> dict:
        return await self.call("getrawtransaction", txid, True)

    async def get_block_header(self, blockhash: str) -> dict:
        return await self.call("getblockheader", blockhash, True)

    async def verify_utxo(
        self,
        txid:      str,
        vout:      int,
        address:   str,
        amount:    float,
        tolerance: float = 0.0,
    ) -> tuple[bool, int, float]:
        """Verify a UTXO pays the correct address, within `tolerance` of
        `amount`. `amount` is the invoice's remaining/expected amount, used
        as an upper bound (plus tolerance) — not an exact-match target. This
        lets a genuine partial payment (less than the remaining amount)
        validate as a real receive rather than being rejected as a
        mismatch; the caller decides partial-vs-complete by comparing the
        returned `received` against the invoice total. A received amount
        above `amount + tolerance` still rejects here, since that's more
        likely an unrelated transaction to the same (possibly reused)
        address than a legitimate overpayment of THIS remaining balance."""
        try:
            tx = await self.get_transaction(txid)
        except FiroRPCError:
            return False, 0, 0.0

        confs = tx.get("confirmations", 0)
        for d in tx.get("details", []):
            if d.get("category") == "receive" and d.get("address") == address:
                recv = float(d.get("amount", 0))
                if recv > 0 and round(recv - amount, 8) <= tolerance:
                    return True, confs, recv

        # Spark fallback
        raw = float(tx.get("amount", 0))
        if raw > 0 and round(raw - amount, 8) <= tolerance:
            return True, confs, raw

        return False, confs, 0.0

    # ─ Wallet info ─
    async def get_wallet_info(self) -> dict:
        try:
            return await self.call("getwalletinfo")
        except: return {}

    # Used by the view-key scanner (app/services/payment_engine.py) to detect
    # payments without any spend authority the node's own wallet is never
    # touched for this.

    async def get_spark_latest_coin_id(self) -> int:
        """RPC: getsparklatestcoinid current highest Spark coin group id."""
        result = await self.call("getsparklatestcoinid")
        return int(result)

    async def get_spark_anonymity_set(
        self, coin_group_id: int, start_block_hash: str = ""
    ) -> dict:
        """
        RPC: getsparkanonymityset <coinGroupId> <startBlockHash>
        Returns {"blockHash": ..., "setHash": ..., "coins": [[serializedCoin, txHash, context], ...]}
        (all three coin fields are base64-encoded strings).
        start_block_hash="" fetches the full set for that group from genesis.
        """
        return await self.call("getsparkanonymityset", str(coin_group_id), start_block_hash)


_rpc: Optional[FiroRPC] = None

def get_rpc() -> FiroRPC:
    global _rpc
    if _rpc is None: _rpc = FiroRPC()
    return _rpc


_NODE_HEALTH: dict = {"ok": None, "ts": 0.0}
_NODE_HEALTH_TTL = 15.0
_NODE_HEALTH_REFRESHING = False


async def node_is_online(ttl: float = _NODE_HEALTH_TTL) -> bool:
    import time as _t
    global _NODE_HEALTH_REFRESHING
    now = _t.time()
    if now - _NODE_HEALTH["ts"] < ttl and _NODE_HEALTH["ok"] is not None:
        return bool(_NODE_HEALTH["ok"])

    if not _NODE_HEALTH_REFRESHING:
        _NODE_HEALTH_REFRESHING = True

        async def _refresh():
            global _NODE_HEALTH_REFRESHING
            try:
                ok = await get_rpc().ping()
                _NODE_HEALTH["ok"] = ok
                _NODE_HEALTH["ts"] = _t.time()
            finally:
                _NODE_HEALTH_REFRESHING = False

        import asyncio
        asyncio.create_task(_refresh())

    # Stale-but-known value is still more useful than blocking; if we've
    # never checked at all, assume online so a cold cache never shows a
    # false maintenance banner or stalls the first checkout load.
    return bool(_NODE_HEALTH["ok"]) if _NODE_HEALTH["ok"] is not None else True
