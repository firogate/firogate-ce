import hashlib
import struct
from loguru import logger
from app.core.config import get_settings

settings = get_settings()

FIRO_MAINNET_VER = bytes([0x52])
FIRO_TESTNET_VER = bytes([0x41])

XPUB_VER_MAP = {
    bytes.fromhex("0488B21E"): "mainnet",
    bytes.fromhex("043587CF"): "testnet",
}


def _detect_network(xpub: str) -> str:
    try:
        import base58
        raw = base58.b58decode_check(xpub)
        ver = raw[:4]
        net = XPUB_VER_MAP.get(ver)
        if net:
            return net
    except Exception:
        pass

    if xpub.startswith("tpub"):
        return "testnet"
    return "mainnet"


def _addr_version(xpub: str) -> bytes:
    if settings.HD_COIN_TYPE == 1:
        return FIRO_TESTNET_VER
    if settings.HD_COIN_TYPE == 136:
        return FIRO_MAINNET_VER
    return FIRO_TESTNET_VER if _detect_network(xpub) == "testnet" else FIRO_MAINNET_VER

def _ckd_pub(parent_key: bytes, parent_chain: bytes, index: int):
    import hmac as _hmac
    if index >= 0x80000000:
        raise ValueError("Hardened index not supported with xpub")
    data = parent_key + struct.pack(">I", index)
    I    = _hmac.new(parent_chain, data, hashlib.sha512).digest()
    IL, IR = I[:32], I[32:]
    child_key = _point_add_tweak(parent_key, IL)
    return child_key, IR


def _point_add_tweak(pubkey: bytes, tweak: bytes) -> bytes:
    try:
        import coincurve
        pub   = coincurve.PublicKey(pubkey)
        tp    = coincurve.PublicKey.from_secret(tweak)
        return coincurve.PublicKey.combine_keys([pub, tp]).format(compressed=True)
    except ImportError:
        pass
    return _secp256k1_tweak(pubkey, tweak)


def _secp256k1_tweak(pubkey: bytes, tweak: bytes) -> bytes:
    P  = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
    Gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
    Gy = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8

    def inv(a): return pow(a, P - 2, P)
    def add(A, B):
        if A is None: return B
        if B is None: return A
        x1,y1=A; x2,y2=B
        if x1==x2:
            if y1!=y2: return None
            m=(3*x1*x1)*inv(2*y1)%P
        else: m=(y2-y1)*inv(x2-x1)%P
        x3=(m*m-x1-x2)%P; y3=(m*(x1-x3)-y1)%P
        return x3,y3
    def mul(k,pt):
        r,a=None,pt
        while k:
            if k&1: r=add(r,a)
            a=add(a,a); k>>=1
        return r
    def decomp(pk):
        x=int.from_bytes(pk[1:],'big'); y2=(pow(x,3,P)+7)%P; y=pow(y2,(P+1)//4,P)
        if (y%2)!=(pk[0]-2): y=P-y
        return x,y
    def comp(pt):
        x,y=pt; pfx=b'\x02' if y%2==0 else b'\x03'
        return pfx+x.to_bytes(32,'big')

    G=(Gx,Gy); pt=decomp(pubkey); tw=mul(int.from_bytes(tweak,'big'),G)
    return comp(add(pt,tw))


def _pub_to_address(pub: bytes, xpub: str) -> str:
    import base58
    sha  = hashlib.sha256(pub).digest()
    h160 = hashlib.new("ripemd160", sha).digest()
    return base58.b58encode_check(_addr_version(xpub) + h160).decode()


def _parse_xpub(xpub: str):
    import base58
    raw = base58.b58decode_check(xpub)
    chain = raw[13:45]
    key   = raw[45:78]
    return key, chain

def derive_address(merchant_index: int, payment_index: int) -> str:
    xpub = (settings.HD_XPUB or "").strip()
    if not xpub:
        raise ValueError("HD_XPUB not configured set in .env or Admin → HD Wallet")

    net = _detect_network(xpub)
    logger.debug(f"HD derive [{net}] m/{settings.HD_ACCOUNT}/{merchant_index}/{payment_index}")

    try:
        from hdwallet import HDWallet
        try:
            from hdwallet.cryptocurrencies import FiroMainnet, FiroTestnet
            coin = FiroTestnet if net == "testnet" else FiroMainnet
            hd = HDWallet(cryptocurrency=coin)
        except Exception:
            hd = HDWallet()

        hd.from_xpublic_key(xpub=xpub)
        for idx in [settings.HD_ACCOUNT, merchant_index, payment_index]:
            hd.from_index(idx)

        try:
            addr = hd.p2pkh_address()
            if addr and len(addr) > 20:
                logger.debug(f"HD [hdwallet] → {addr[:20]}…")
                return addr
        except Exception:
            pass
        pub_hex = hd.public_key()
        return _pub_to_address(bytes.fromhex(pub_hex), xpub)

    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"hdwallet strategy failed: {e}")

    try:
        import bip32utils
        key = bip32utils.BIP32Key.fromExtendedKey(xpub)
        for idx in [settings.HD_ACCOUNT, merchant_index, payment_index]:
            key = key.ChildKey(idx)
        addr = _pub_to_address(key.PublicKey(), xpub)
        logger.debug(f"HD [bip32utils] → {addr[:20]}…")
        return addr
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"bip32utils strategy failed: {e}")

    try:
        import base58
        key, chain = _parse_xpub(xpub)
        for idx in [settings.HD_ACCOUNT, merchant_index, payment_index]:
            key, chain = _ckd_pub(key, chain, idx)
        addr = _pub_to_address(key, xpub)
        logger.debug(f"HD [pure] → {addr[:20]}…")
        return addr
    except Exception as e:
        logger.error(f"Pure BIP32 failed: {e}")
        raise RuntimeError(
            "HD wallet derivation failed. "
            "Run: pip install hdwallet  (or pip install bip32utils base58)"
        )


def validate_xpub(xpub: str) -> tuple[bool, str]:
    xpub = (xpub or "").strip()
    if len(xpub) < 100:
        return False, "xpub is too short"

    net = _detect_network(xpub)

    try:
        import base58
        base58.b58decode_check(xpub)
    except Exception as e:
        return False, f"Invalid checksum: {e}"

    orig = settings.HD_XPUB
    try:
        object.__setattr__(settings, 'HD_XPUB', xpub)
        addr = derive_address(0, 0)
        if not addr or len(addr) < 20:
            return False, "Derived address looks invalid"
        logger.info(f"xpub OK: network={net} test_addr={addr[:20]}…")
        return True, net
    except Exception as e:
        return False, str(e)
    finally:
        object.__setattr__(settings, 'HD_XPUB', orig)


def get_network_info() -> dict:
    xpub = (settings.HD_XPUB or "").strip()
    net  = _detect_network(xpub) if xpub else "not configured"
    return {
        "configured":  bool(xpub),
        "network":     net,
        "is_testnet":  net == "testnet",
        "coin_type":   settings.HD_COIN_TYPE,
        "account":     settings.HD_ACCOUNT,
        "xpub_prefix": xpub[:24] + "…" if len(xpub) > 24 else xpub,
    }
