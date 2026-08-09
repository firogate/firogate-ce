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

    async def get_balance(self) -> float:
        try:
            return float(await self.call("getbalance"))
        except: return 0.0

    async def list_addresses(self) -> list:
        """List all wallet addresses with balances."""
        try:
            return await self.call("listaddressgroupings")
        except: return []

    # ─ Send FIRO ─
    # ─ Wallet encryption helpers ─

    async def _wallet_unlock(self) -> bool:
        """
        Unlock wallet.dat for full transaction access (not staking-only).

        Always force-unlocks: walletlock first (in case it's staking-only),
        then walletpassphrase for full tx access with staking_only=false.

        Duration is always read from WALLET_UNLOCK_SECONDS in .env.

        Always reads WALLET_PASSPHRASE fresh from get_settings() so that
        changes in .env are picked up without restarting the process.

        Returns True if wallet is ready (unlocked or unencrypted).
        Raises FiroRPCError(-4) if unlock fails.
        """
        passphrase  = get_settings().WALLET_PASSPHRASE
        unlock_secs = get_settings().WALLET_UNLOCK_SECONDS or 60

        if not passphrase:
            return True

        # Force walletlock first to clear any staking-only lock,
        # then re-unlock for full transaction access.
        try:
            await self.call("walletlock")
        except FiroRPCError:
            pass

        try:
            await self.call("walletpassphrase", passphrase, unlock_secs)
            logger.debug(f"Wallet unlocked for {unlock_secs}s (full access)")
            return True
        except FiroRPCError as e:
            if e.code == -15:
                # wallet is not encrypted, no unlock needed
                return True
            if e.code == -14:
                logger.error("Wrong wallet passphrase check WALLET_PASSPHRASE in .env")
                raise FiroRPCError(-4, "Wrong wallet passphrase check WALLET_PASSPHRASE in .env")
            if e.code == -17:
                # some node versions return this if already unlocked
                logger.debug("Wallet already unlocked")
                return True
            logger.error(f"Cannot unlock wallet (code={e.code}): {e.message}")
            raise FiroRPCError(-4, f"Cannot unlock wallet: {e.message}")

    async def _wallet_lock(self):
        """Re-lock wallet after sensitive operation."""
        if not get_settings().WALLET_PASSPHRASE:
            return
        try:
            await self.call("walletlock")
            logger.debug("Wallet re-locked after operation")
        except FiroRPCError:
            pass

    async def send_to_address(
        self,
        address: str,
        amount:  float,
        comment: str = "",
    ) -> str:
        """
        Send FIRO to a transparent address. Returns txid.
        Auto-unlocks encrypted wallet.dat before sending, re-locks after.
        """
        amount = round(float(amount), 8)
        await self._wallet_unlock()
        try:
            logger.info(f"sendtoaddress → {address[:20]}… amount={amount:.8f}")
            txid = await self.call("sendtoaddress", address, amount, comment)
            logger.info(f"sendtoaddress txid={txid[:16]}…")
            return str(txid)
        finally:
            await self._wallet_lock()

    # Spark balance is private, stored in wallet.dat. We do NOT pre-check
    # balance before spendspark wallet.dat handles signing and RPC error
    # -6 means insufficient funds.

    def is_spark_address(self, address: str) -> bool:
        """
        Detect if address is a Firo Spark address.
        Official prefixes: sm (mainnet), st (testnet).
        Typical length: ~144 characters.
        """
        addr = (address or "").strip()
        return addr.startswith("sm") or addr.startswith("st")

    async def get_spark_balance(self) -> float:
        """
        Get wallet Spark-only balance.
        RPC: getsparkbalance (official Firo Spark command)
        """
        try:
            result = await self.call("getsparkbalance")
            if isinstance(result, dict):
                return float(result.get("balance", 0.0))
            return float(result or 0.0)
        except FiroRPCError as e:
            logger.warning(f"getsparkbalance failed: {e.message}")
            return 0.0
        except Exception:
            return 0.0

    async def get_spark_address_balance(self, address: str) -> dict:
        """
        Get balance of a specific Spark address.
        RPC: getsparkaddressbalance <address>
        Returns: {available, unconfirmed, total} or float
        """
        try:
            return await self.call("getsparkaddressbalance", address)
        except FiroRPCError as e:
            raise FiroRPCError(e.code, f"getsparkaddressbalance: {e.message}")

    async def get_spark_default_address(self) -> str:
        """
        Get wallet's default Spark address (always returns same address).
        RPC: getsparkdefaultaddress
        """
        try:
            result = await self.call("getsparkdefaultaddress")
            if isinstance(result, dict):
                return result.get("address", str(result))
            return str(result)
        except FiroRPCError as e:
            raise FiroRPCError(e.code, f"getsparkdefaultaddress: {e.message}")

    async def get_new_spark_address(self) -> str:
        """
        Generate a new Spark address.
        RPC: getnewsparkaddress
        """
        try:
            result = await self.call("getnewsparkaddress")
            if isinstance(result, dict):
                return result.get("address", str(result))
            return str(result)
        except FiroRPCError as e:
            raise FiroRPCError(e.code, f"getnewsparkaddress: {e.message}")

    async def spark_send(
        self,
        to_address: str,
        amount:     float,
        subtract_fee: bool = False,
        memo:       str  = "",
    ) -> str:
        """
        Send from Spark private balance.
        RPC: spendspark {"address":{amount, subtractFee, memo}}

        spendspark is synchronous, returns txid directly (not an operation ID).
        Not usable with exchange addresses per official Firo docs, they only
        accept from a transparent balance use sendtoaddress for those instead.

        Returns: txid string
        Raises: FiroRPCError on failure
        """
        amount = round(float(amount), 8)
        recipients = {
            to_address: {
                "amount": amount,
                "subtractFee": subtract_fee,
            }
        }
        if memo:
            recipients[to_address]["memo"] = memo

        # spendspark takes recipients as a JSON string per Firo RPC spec
        import json as _json
        recipients_str = _json.dumps(recipients)

        await self._wallet_unlock()
        logger.info(f"spendspark → {to_address[:20]}… amount={amount:.8f}")

        try:
            txid = await self.call("spendspark", recipients_str)
            if not txid:
                raise FiroRPCError(-1, "spendspark returned empty txid")
            logger.info(f"spendspark txid={str(txid)[:16]}…")
            return str(txid)
        except FiroRPCError as e:
            if e.code == -4:
                # Wallet re-locked between unlock and spendspark call (race / timeout);
                # unlock again and retry exactly once.
                logger.warning("spendspark got -4 (wallet locked) retrying with fresh unlock")
                await self._wallet_unlock()
                try:
                    txid = await self.call("spendspark", recipients_str)
                    if not txid:
                        raise FiroRPCError(-1, "spendspark returned empty txid on retry")
                    logger.info(f"spendspark retry txid={str(txid)[:16]}…")
                    return str(txid)
                except FiroRPCError as e2:
                    if e2.code == -4:
                        raise FiroRPCError(
                            -4,
                            "Wallet is encrypted and WALLET_PASSPHRASE in .env is "
                            "wrong or empty. Check your .env and restart the server."
                        )
                    raise e2
            if e.code == -6:
                raise FiroRPCError(-6, "Insufficient Spark balance")
            if e.code == -5:
                raise FiroRPCError(-5, f"Invalid address: {e.message}")
            if e.code == -8:
                raise FiroRPCError(-8, f"Invalid parameter: {e.message}")
            if e.code in (-32600, -32700):
                raise FiroRPCError(
                    e.code,
                    "Cannot send Spark to this address. Exchange addresses only "
                    "accept transparent (sendtoaddress) transfers."
                )
            raise
        finally:
            await self._wallet_lock()

    async def auto_mint_spark(self) -> str:
        """
        Auto-shield all transparent balance to Spark.
        RPC: automintspark
        Returns txid or empty string.

        Requires wallet to be unlocked calls _wallet_unlock() first.
        automintspark fails silently with -13 if wallet is locked.
        """
        try:
            await self._wallet_unlock()
            result = await self.call("automintspark")
            return str(result or "")
        except FiroRPCError as e:
            if e.code == -13:
                logger.warning("[auto_mint_spark] wallet locked check WALLET_PASSPHRASE in .env")
            elif e.code == -6:
                logger.debug("[auto_mint_spark] nothing to shield (balance too low or already shielded)")
            else:
                logger.warning(f"[auto_mint_spark] RPC error ({e.code}): {e.message}")
            return ""
        except Exception as e:
            logger.warning(f"[auto_mint_spark] unexpected error: {e}")
            return ""

    # Used by the view-key scanner (app/services/payment_engine.py) to detect
    # payments without any spend authority the node's own wallet is never
    # touched for this, unlike get_new_spark_address/spark_send above.

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

    async def validate_spark_address(self, address: str) -> bool:
        """
        Validate a Firo address (t-address or Spark sm/st address).
        Uses validateaddress for t-addresses.
        For Spark: checks prefix and length (no RPC method for Spark validation).
        """
        addr = (address or "").strip()
        if self.is_spark_address(addr):
            return len(addr) >= 100  # Spark addresses are long (~144 chars)
        return await self.validate_address_rpc(address)

    async def validate_address_rpc(self, address: str) -> bool:
        """Ask the node to validate an address. Returns True if valid."""
        try:
            result = await self.call("validateaddress", address)
            return bool(result.get("isvalid", False))
        except Exception:
            return False

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
