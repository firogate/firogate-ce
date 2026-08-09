"""
app/services/sparkmobile.py

ctypes binding for libflutter_libsparkmobile.so Firo's official Spark
protocol library (firoorg/sparkmobile, MIT), wrapped for FFI by Cypher Stack
(the Spark protocol's co-designers). Same library Campfire Wallet uses.

Only wraps the view-key / detection surface FiroGate needs:
  - import a merchant's Spark FULL VIEW KEY (no spend authority at all)
  - derive a checkout address for a given diversifier, offline, no node call
  - identify whether a coin from the public anonymity set belongs to that
    view key, and if so recover its amount/diversifier/memo/coin tag

Spend/mint functions are intentionally not wrapped here FiroGate never
needs spend authority over a merchant's Spark funds.
"""
import base64
import binascii
import ctypes
from pathlib import Path
from typing import NamedTuple, Optional

_LIB_PATH = Path(__file__).resolve().parent.parent / "native" / "libflutter_libsparkmobile.so"

_lib = ctypes.CDLL(str(_LIB_PATH))


class _AggregateCoinData(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_char),
        ("diversifier", ctypes.c_uint64),
        ("value", ctypes.c_uint64),
        ("address", ctypes.c_char_p),
        ("memo", ctypes.c_char_p),
        ("lTagHash", ctypes.c_char_p),
        ("encryptedDiversifier", ctypes.POINTER(ctypes.c_ubyte)),
        ("encryptedDiversifierLength", ctypes.c_int),
        ("serial", ctypes.POINTER(ctypes.c_ubyte)),
        ("serialLength", ctypes.c_int),
        ("nonceHex", ctypes.c_char_p),
        ("nonceHexLength", ctypes.c_int),
    ]


_lib.deserializeFullViewKey.argtypes = [ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int]
_lib.deserializeFullViewKey.restype = ctypes.c_void_p

_lib.serializeFullViewKey.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
_lib.serializeFullViewKey.restype = ctypes.POINTER(ctypes.c_ubyte)

_lib.deleteFullViewKey.argtypes = [ctypes.c_void_p]
_lib.deleteFullViewKey.restype = None

_lib.getAddressFromFullViewKey.argtypes = [
    ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
]
_lib.getAddressFromFullViewKey.restype = ctypes.c_char_p

_lib.idAndRecoverCoinByFullViewKey.argtypes = [
    ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int,
    ctypes.c_int,
]
_lib.idAndRecoverCoinByFullViewKey.restype = ctypes.POINTER(_AggregateCoinData)

_lib.native_free.argtypes = [ctypes.c_void_p]
_lib.native_free.restype = None


class SparkViewKeyError(Exception):
    """Raised when a view key hex string fails to deserialize."""


class IdentifiedCoin(NamedTuple):
    diversifier: int
    value: int          # atomic units (satoshi-equivalent)
    address: str
    memo: str
    coin_tag: str        # lTagHash unique per coin, use to dedupe


def _bytes_to_c_ubyte_p(data: bytes):
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)), buf


class SparkViewKey:
    """Wraps a merchant's Spark full view key. No spend authority this
    object can only derive addresses and identify incoming/outgoing coins.
    """

    def __init__(self, view_key_hex: str, is_testnet: bool = True):
        self.is_testnet = is_testnet
        try:
            raw = bytes.fromhex(view_key_hex.strip())
        except (ValueError, binascii.Error) as e:
            raise SparkViewKeyError(f"Invalid hex: {e}")

        ptr_arr, _keep = _bytes_to_c_ubyte_p(raw)
        handle = _lib.deserializeFullViewKey(ptr_arr, len(raw))
        if not handle:
            raise SparkViewKeyError("deserializeFullViewKey failed malformed view key")
        self._handle = handle

    def __del__(self):
        handle = getattr(self, "_handle", None)
        if handle:
            _lib.deleteFullViewKey(handle)
            self._handle = None

    def get_address(self, diversifier: int, index: int = 0) -> str:
        """Derive a Spark address for this view key + diversifier. Pure
        math no node/wallet call needed. Each diversifier is effectively
        a distinct one-time-payable address that still resolves back to
        this same view key when scanning.
        """
        result = _lib.getAddressFromFullViewKey(
            self._handle, index, diversifier, 1 if self.is_testnet else 0,
        )
        if not result:
            raise SparkViewKeyError("getAddressFromFullViewKey failed")
        return result.decode("utf-8")

    def identify_coin(
        self,
        serialized_coin_b64: str,
        context_b64: str,
    ) -> Optional[IdentifiedCoin]:
        """Given one coin from firod's getsparkanonymityset (base64
        serializedCoin + base64 context/txMetadata), return the decrypted
        payment details if this coin belongs to this view key, else None.
        """
        try:
            coin_bytes = base64.b64decode(serialized_coin_b64)
            context_bytes = base64.b64decode(context_b64)
        except (ValueError, binascii.Error) as e:
            raise SparkViewKeyError(f"Invalid base64 coin/context: {e}")

        coin_ptr, _keep1 = _bytes_to_c_ubyte_p(coin_bytes)
        ctx_ptr, _keep2 = _bytes_to_c_ubyte_p(context_bytes)

        result_ptr = _lib.idAndRecoverCoinByFullViewKey(
            coin_ptr, len(coin_bytes),
            self._handle,
            ctx_ptr, len(context_bytes),
            1 if self.is_testnet else 0,
        )
        if not result_ptr:
            return None  # not ours expected for the vast majority of coins

        data = result_ptr.contents
        try:
            identified = IdentifiedCoin(
                diversifier=data.diversifier,
                value=data.value,
                address=(data.address or b"").decode("utf-8"),
                memo=(data.memo or b"").decode("utf-8"),
                coin_tag=(data.lTagHash or b"").decode("utf-8"),
            )
        finally:
            _lib.native_free(result_ptr)
        return identified
