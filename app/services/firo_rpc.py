"""
app/services/firo_rpc.py
========================
# === PRIVATE CORE ===
# This module contains the Firo node RPC client.
# It handles all wallet operations: address generation, balance checking,
# transaction submission, and Spark shielding.
# The public stub is available at public/stubs/firo_rpc.py
# === END PRIVATE CORE ===

Firo RPC client.
Uses getnewaddress to generate unique addresses per payment.
The node tracks all addresses it created — no importaddress needed.
UTXO-aware: each (txid, vout) can only confirm ONE payment.
"""
import httpx
import hashlib
import struct
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

        # Only route RPC through Tor if node is NOT on localhost.
        # Tor cannot route to 127.0.0.1 / localhost — that's always direct.
        _localhost = {"127.0.0.1", "::1", "localhost"}
        _rpc_is_local = settings.FIRO_RPC_HOST in _localhost

        if settings.TOR_ENABLED and not _rpc_is_local:
            # Remote node behind Tor — route via SOCKS5
            kwargs["transport"] = httpx.AsyncHTTPTransport(
                proxy=settings.tor_socks_url
            )
            logger.info(f"RPC via Tor SOCKS5 → {settings.tor_socks_url}")
        elif settings.TOR_ENABLED and _rpc_is_local:
            # Local node — direct connection regardless of TOR_ENABLED
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
    async def get_network_info(self) -> dict:    return await self.call("getnetworkinfo")

    # ─ Address generation ─
    async def get_new_address(self, label: str = "") -> str:
        """
        Generate a fresh address from the node's HD wallet.
        Node automatically watches and tracks this address.
        No importaddress needed.
        """
        try:
            # Firo Core: getnewaddress [label] [address_type]
            addr = await self.call("getnewaddress", label, "legacy")
            logger.info(f"New address generated: {addr[:20]}… label={label}")
            return addr
        except FiroRPCError as e:
            # Try without address_type for older node versions
            if e.code == -1 or "Invalid" in e.message:
                addr = await self.call("getnewaddress", label)
                logger.info(f"New address (v2): {addr[:20]}… label={label}")
                return addr
            raise

    async def get_new_address_for_payment(self, payment_id: str) -> str:
        """Generate address with payment ID as label for easy tracking."""
        label = f"pay:{payment_id[:12]}"
        return await self.get_new_address(label)

    async def get_new_address_for_plan(self, order_id: str) -> str:
        """Generate address for plan purchase."""
        label = f"plan:{order_id[:12]}"
        return await self.get_new_address(label)

    # ─ Transaction list ─
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

    async def get_confirmations(self, txid: str) -> int:
        try:
            tx = await self.get_transaction(txid)
            return tx.get("confirmations", 0)
        except: return 0

    # ─ UTXO payment detection ─
    async def find_utxo_for_address(
        self,
        address:      str,
        amount:       float,
        locked_utxos: set[tuple] | None = None,
        tolerance:    float = 0.02,
    ) -> tuple[str | None, int | None, float, int]:
        """
        Find a unique UTXO for the given address+amount.
        The node knows about this address (it generated it).

        Returns: (txid, vout, amount_received, confirmations)
        """
        if locked_utxos is None:
            locked_utxos = set()

        min_amount = amount * (1 - tolerance)
        txs = await self.list_transactions(count=1000)

        receives = [t for t in txs if t.get("category") == "receive"]
        logger.info(
            f"[UTXO] scan addr={address[:20]}… "
            f"target={amount:.8f} receives={len(receives)}"
        )

        # ─ Fast path: getaddresstxids (Firo-specific, works with Spark) ──
        try:
            txids_for_addr = await self.call("getaddresstxids", {"addresses": [address]})
            if txids_for_addr:
                for txid in reversed(txids_for_addr):
                    try:
                        full = await self.get_transaction(txid)
                        received = sum(
                            float(d.get("amount", 0))
                            for d in full.get("details", [])
                            if d.get("category") == "receive"
                            and d.get("address") == address
                        )
                        if received < min_amount:
                            continue
                        confs = full.get("confirmations", 0)
                        vout  = await self._get_vout(txid, address, received)
                        utxo  = (txid, vout)
                        if utxo in locked_utxos:
                            continue
                        logger.info(
                            f"[UTXO] ✅ (getaddresstxids) {txid[:16]}…:{vout} "
                            f"amt={received:.8f} confs={confs}"
                        )
                        return txid, vout, received, confs
                    except Exception:
                        continue
                logger.info(f"[UTXO] getaddresstxids: no match for {address[:20]}…")
        except Exception as e:
            logger.debug(f"[UTXO] getaddresstxids unavailable: {e}")

        # ─ listtransactions loop (skip EMPTY-address TX to avoid RPC flood) ─
        for tx in sorted(txs, key=lambda x: x.get("time", 0), reverse=True):
            if tx.get("category") != "receive":
                continue

            txid      = tx.get("txid", "")
            tx_amount = float(tx.get("amount", 0))
            if tx_amount < min_amount:
                continue

            tx_addr = tx.get("address", "")
            # If address is present and doesn't match → skip immediately
            if tx_addr and tx_addr != address:
                continue
            # If address is EMPTY → skip (don't flood with gettransaction)
            if not tx_addr:
                continue

            vout = await self._get_vout(txid, address, tx_amount)
            utxo = (txid, vout)
            if utxo in locked_utxos:
                continue

            confs = tx.get("confirmations", 0)
            logger.info(
                f"[UTXO] ✅ (listtransactions) {txid[:16]}…:{vout} "
                f"amt={tx_amount:.8f} confs={confs}"
            )
            return txid, vout, tx_amount, confs

        logger.info(f"[UTXO] No match in listtransactions for {address[:20]}…")

        # ─ Fallback 1: listunspent (more reliable for fresh addresses) ─
        try:
            unspent = await self.call("listunspent", 0, 9999999, [address])
            for u in unspent:
                u_amount = float(u.get("amount", 0))
                if u_amount < min_amount:
                    continue
                txid = u.get("txid", "")
                vout = u.get("vout", 0)
                utxo = (txid, vout)
                if utxo in locked_utxos:
                    continue
                confs = u.get("confirmations", 0)
                logger.info(
                    f"[UTXO] ✅ (listunspent) {txid[:16]}…:{vout} "
                    f"amt={u_amount:.8f} confs={confs}"
                )
                return txid, vout, u_amount, confs
        except FiroRPCError as e:
            logger.debug(f"[UTXO] listunspent fallback failed: {e}")

        # ─ Fallback 2: listreceivedbyaddress (catches zero-conf too) ─
        try:
            # Note: this Firo node version takes only 3 args (no address filter)
            # so we fetch all and filter in Python
            received_all = await self.call("listreceivedbyaddress", 0, False, True)
            for r in (received_all or []):
                if r.get("address") != address:
                    continue
                r_amount = float(r.get("amount", 0))
                if r_amount < min_amount:
                    continue
                txids = r.get("txids", [])
                if not txids:
                    continue
                txid = txids[-1]
                utxo = (txid, 0)
                if utxo in locked_utxos:
                    continue
                confs = r.get("confirmations", 0)
                vout  = await self._get_vout(txid, address, r_amount)
                logger.info(
                    f"[UTXO] ✅ (listreceivedbyaddress) {txid[:16]}…:{vout} "
                    f"amt={r_amount:.8f} confs={confs}"
                )
                return txid, vout, r_amount, confs
        except FiroRPCError as e:
            logger.debug(f"[UTXO] listreceivedbyaddress fallback failed: {e}")

        logger.info(f"[UTXO] No match for {address[:20]}… (all methods tried)")
        return None, None, 0.0, 0

    async def _get_vout(self, txid: str, address: str, amount: float) -> int:
        try:
            raw = await self.get_raw_transaction(txid)
            for i, vout in enumerate(raw.get("vout", [])):
                script = vout.get("scriptPubKey", {})
                addrs  = script.get("addresses", [script.get("address", "")])
                if address in addrs:
                    if abs(float(vout.get("value", 0)) - amount) < 0.001:
                        return i
        except Exception:
            pass
        return 0

    async def verify_utxo(
        self,
        txid:    str,
        vout:    int,
        address: str,
        amount:  float,
    ) -> tuple[bool, int, float]:
        """Verify specific UTXO pays correct address+amount."""
        try:
            tx = await self.get_transaction(txid)
        except FiroRPCError:
            return False, 0, 0.0

        confs = tx.get("confirmations", 0)
        for d in tx.get("details", []):
            if d.get("category") == "receive" and d.get("address") == address:
                recv = float(d.get("amount", 0))
                if recv >= amount * 0.98:
                    return True, confs, recv

        # Spark fallback
        raw = float(tx.get("amount", 0))
        if abs(raw - amount) <= amount * 0.02:
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
            return True  # wallet not encrypted — nothing to do

        # Force walletlock first to clear any staking-only lock,
        # then re-unlock for full transaction access.
        try:
            await self.call("walletlock")
        except FiroRPCError:
            pass  # already locked or not encrypted — ignore

        try:
            # walletpassphrase accepts 2 params: passphrase + timeout.
            # This unlocks for full transaction access (send, spendspark).
            # walletlock was called above to clear any staking-only state.
            await self.call("walletpassphrase", passphrase, unlock_secs)
            logger.debug(f"Wallet unlocked for {unlock_secs}s (full access)")
            return True
        except FiroRPCError as e:
            if e.code == -15:
                # -15 = wallet is not encrypted — no unlock needed
                return True
            if e.code == -14:
                logger.error("Wrong wallet passphrase — check WALLET_PASSPHRASE in .env")
                raise FiroRPCError(-4, "Wrong wallet passphrase — check WALLET_PASSPHRASE in .env")
            if e.code == -17:
                # -17 = wallet is already unlocked (some node versions)
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
            pass  # already locked or not encrypted — ignore

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

    # ─ Spark methods (Firo Spark RPC) ─
    # Source: https://github.com/firoorg/firo/wiki/Spark-RPC-calls
    #
    # Spark address format:
    #   Mainnet: sm...  (~144 chars)
    #   Testnet: st...  (~144 chars)
    #
    # Key design decision:
    #   Spark balance is PRIVATE — stored in wallet.dat.
    #   We do NOT pre-check balance before spendspark.
    #   wallet.dat handles signing; RPC error -6 means insufficient funds.
    #
    # Core commands used:
    #   spendspark    — send from Spark balance (sync, returns txid directly)
    #   getnewsparkaddress      — generate Spark address
    #   getsparkdefaultaddress  — get default Spark address
    #   automintspark           — shield transparent balance to Spark

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
            # Returns float directly or dict with 'balance' key
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

        spendspark is SYNCHRONOUS — returns txid directly (not an operation ID).
        Works for:
          Spark to Spark address (sm.../st...)   — private send
          Spark to t-address (personal wallet)   — deshield
          Spark to exchange t-address            — NOT supported (exchanges only accept transparent sendtoaddress)

        Per official Firo docs: "Not usable with exchange addresses as they
        can only accept from a transparent balance."
        If user wants to withdraw to an exchange, use sendtoaddress instead.

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

        # Unlock wallet before calling spendspark.
        # Duration comes from WALLET_UNLOCK_SECONDS in .env.
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
                # Wallet re-locked between unlock and spendspark call (race / timeout).
                # Unlock again and retry exactly once.
                logger.warning("spendspark got -4 (wallet locked) — retrying with fresh unlock")
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

        Requires wallet to be unlocked — calls _wallet_unlock() first.
        automintspark fails silently with -13 if wallet is locked.
        """
        try:
            await self._wallet_unlock()
            result = await self.call("automintspark")
            return str(result or "")
        except FiroRPCError as e:
            if e.code == -13:
                logger.warning("[auto_mint_spark] wallet locked — check WALLET_PASSPHRASE in .env")
            elif e.code == -6:
                logger.debug("[auto_mint_spark] nothing to shield (balance too low or already shielded)")
            else:
                logger.warning(f"[auto_mint_spark] RPC error ({e.code}): {e.message}")
            return ""
        except Exception as e:
            logger.warning(f"[auto_mint_spark] unexpected error: {e}")
            return ""

    async def validate_spark_address(self, address: str) -> bool:
        """
        Validate a Firo address (t-address or Spark sm/st address).
        Uses validateaddress for t-addresses.
        For Spark: checks prefix and length (no RPC method for Spark validation).
        """
        addr = (address or "").strip()
        if self.is_spark_address(addr):
            # Official Spark address validation:
            # mainnet: starts with 'sm', length ~144 chars
            # testnet: starts with 'st', length ~144 chars
            return len(addr) >= 100  # Spark addresses are long (~144 chars)
        # t-address: use node validateaddress
        return await self.validate_address_rpc(address)

    async def validate_address_rpc(self, address: str) -> bool:
        """Ask the node to validate an address. Returns True if valid."""
        try:
            result = await self.call("validateaddress", address)
            return bool(result.get("isvalid", False))
        except Exception:
            return False

    async def get_transaction_confirmations(self, txid: str) -> int:
        """Return confirmation count for a txid, or -1 if not found."""
        try:
            tx = await self.call("gettransaction", txid, True)
            return int(tx.get("confirmations", 0))
        except FiroRPCError as e:
            if e.code == -5:   # Invalid or non-wallet transaction id
                return -1
            return 0
        except Exception:
            return 0


_rpc: Optional[FiroRPC] = None

def get_rpc() -> FiroRPC:
    global _rpc
    if _rpc is None: _rpc = FiroRPC()
    return _rpc
