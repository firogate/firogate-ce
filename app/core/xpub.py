import hashlib
import hmac
import struct

_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
      0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

XPUB_MAIN = bytes.fromhex("0488B21E")
XPUB_TEST = bytes.fromhex("043587CF")
XPRV_VERSIONS = {bytes.fromhex("0488ADE4"), bytes.fromhex("04358394")}
ADDR_VERSION = {"mainnet": 82, "testnet": 65}


class XpubError(ValueError):
    pass


def _add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    if p1[0] == p2[0] and (p1[1] + p2[1]) % _P == 0:
        return None
    if p1 == p2:
        lam = 3 * p1[0] * p1[0] * pow(2 * p1[1], _P - 2, _P) % _P
    else:
        lam = (p2[1] - p1[1]) * pow(p2[0] - p1[0], _P - 2, _P) % _P
    x = (lam * lam - p1[0] - p2[0]) % _P
    return x, (lam * (p1[0] - x) - p1[1]) % _P


def _mul(k, point=_G):
    result = None
    addend = point
    while k:
        if k & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        k >>= 1
    return result


def _decompress(pub: bytes):
    if len(pub) != 33 or pub[0] not in (2, 3):
        raise XpubError("Invalid public key in xpub.")
    x = int.from_bytes(pub[1:], "big")
    if x >= _P:
        raise XpubError("Invalid public key in xpub.")
    y2 = (pow(x, 3, _P) + 7) % _P
    y = pow(y2, (_P + 1) // 4, _P)
    if y * y % _P != y2:
        raise XpubError("Invalid public key in xpub.")
    if (y & 1) != (pub[0] & 1):
        y = _P - y
    return x, y


def _compress(point) -> bytes:
    return bytes([2 + (point[1] & 1)]) + point[0].to_bytes(32, "big")


def _sha256d(b: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def b58check_decode(s: str) -> bytes:
    n = 0
    for ch in s:
        i = _B58.find(ch)
        if i < 0:
            raise XpubError("Not a valid xpub (bad character).")
        n = n * 58 + i
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    raw = b"\x00" * (len(s) - len(s.lstrip("1"))) + raw
    if len(raw) < 5 or _sha256d(raw[:-4])[:4] != raw[-4:]:
        raise XpubError("Not a valid xpub (checksum failed). Check you copied all of it.")
    return raw[:-4]


def b58check_encode(payload: bytes) -> str:
    data = payload + _sha256d(payload)[:4]
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(data) - len(data.lstrip(b"\x00"))) + out


def _ripemd160_py(data: bytes) -> bytes:
    r1 = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 7, 4, 13, 1, 10, 6, 15, 3, 12, 0, 9, 5, 2, 14, 11, 8,
          3, 10, 14, 4, 9, 15, 8, 1, 2, 7, 0, 6, 13, 11, 5, 12, 1, 9, 11, 10, 0, 8, 12, 4, 13, 3, 7, 15, 14, 5, 6, 2,
          4, 0, 5, 9, 7, 12, 2, 10, 14, 1, 3, 8, 11, 6, 15, 13]
    r2 = [5, 14, 7, 0, 9, 2, 11, 4, 13, 6, 15, 8, 1, 10, 3, 12, 6, 11, 3, 7, 0, 13, 5, 10, 14, 15, 8, 12, 4, 9, 1, 2,
          15, 5, 1, 3, 7, 14, 6, 9, 11, 8, 12, 2, 10, 0, 4, 13, 8, 6, 4, 1, 3, 11, 15, 0, 5, 12, 2, 13, 9, 7, 10, 14,
          12, 15, 10, 4, 1, 5, 8, 7, 6, 2, 13, 14, 0, 3, 9, 11]
    s1 = [11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8, 7, 6, 8, 13, 11, 9, 7, 15, 7, 12, 15, 9, 11, 7, 13, 12,
          11, 13, 6, 7, 14, 9, 13, 15, 14, 8, 13, 6, 5, 12, 7, 5, 11, 12, 14, 15, 14, 15, 9, 8, 9, 14, 5, 6, 8, 6, 5, 12,
          9, 15, 5, 11, 6, 8, 13, 12, 5, 12, 13, 14, 11, 8, 5, 6]
    s2 = [8, 9, 9, 11, 13, 15, 15, 5, 7, 7, 8, 11, 14, 14, 12, 6, 9, 13, 15, 7, 12, 8, 9, 11, 7, 7, 12, 7, 6, 15, 13, 11,
          9, 7, 15, 11, 8, 6, 6, 14, 12, 13, 5, 14, 13, 13, 7, 5, 15, 5, 8, 11, 14, 14, 6, 14, 6, 9, 12, 9, 12, 5, 15, 8,
          8, 5, 12, 9, 12, 5, 14, 6, 8, 13, 6, 5, 15, 13, 11, 11]
    k1 = [0x00000000, 0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xA953FD4E]
    k2 = [0x50A28BE6, 0x5C4DD124, 0x6D703EF3, 0x7A6D76E9, 0x00000000]
    m = 0xFFFFFFFF

    def f(j, x, y, z):
        if j < 16:
            return x ^ y ^ z
        if j < 32:
            return (x & y) | (~x & z)
        if j < 48:
            return (x | ~y) ^ z
        if j < 64:
            return (x & z) | (y & ~z)
        return x ^ (y | ~z)

    def rol(x, n):
        return ((x << n) | (x >> (32 - n))) & m

    msg = bytearray(data)
    bitlen = (8 * len(data)) & 0xFFFFFFFFFFFFFFFF
    msg.append(0x80)
    while len(msg) % 64 != 56:
        msg.append(0)
    msg += struct.pack("<Q", bitlen)
    h = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0]
    for off in range(0, len(msg), 64):
        x = struct.unpack("<16I", bytes(msg[off:off + 64]))
        al, bl, cl, dl, el = h
        ar, br, cr, dr, er = h
        for j in range(80):
            t = rol((al + (f(j, bl, cl, dl) & m) + x[r1[j]] + k1[j // 16]) & m, s1[j]) + el
            al, el, dl, cl, bl = el, dl, rol(cl, 10), bl, t & m
            t = rol((ar + (f(79 - j, br, cr, dr) & m) + x[r2[j]] + k2[j // 16]) & m, s2[j]) + er
            ar, er, dr, cr, br = er, dr, rol(cr, 10), br, t & m
        t = (h[1] + cl + dr) & m
        h[1] = (h[2] + dl + er) & m
        h[2] = (h[3] + el + ar) & m
        h[3] = (h[4] + al + br) & m
        h[4] = (h[0] + bl + cr) & m
        h[0] = t
    return struct.pack("<5I", *h)


def _hash160(b: bytes) -> bytes:
    sha = hashlib.sha256(b).digest()
    try:
        return hashlib.new("ripemd160", sha).digest()
    except (ValueError, TypeError):
        return _ripemd160_py(sha)


class Xpub:
    def __init__(self, depth: int, fingerprint: bytes, child: int, chain: bytes, point, network: str):
        self.depth = depth
        self.parent_fingerprint = fingerprint
        self.child_number = child
        self.chain = chain
        self.point = point
        self.network = network

    @classmethod
    def parse(cls, text: str) -> "Xpub":
        s = (text or "").strip()
        if not s:
            raise XpubError("Paste your xpub first.")
        raw = b58check_decode(s)
        if len(raw) != 78:
            raise XpubError("Not a valid xpub (wrong length).")
        version = raw[:4]
        if version in XPRV_VERSIONS:
            raise XpubError("This is a private key (xprv). Never share it. Paste the xpub instead.")
        if version == XPUB_MAIN:
            network = "mainnet"
        elif version == XPUB_TEST:
            network = "testnet"
        else:
            raise XpubError("Only Firo xpub/tpub keys are supported.")
        depth = raw[4]
        fingerprint = raw[5:9]
        child = int.from_bytes(raw[9:13], "big")
        chain = raw[13:45]
        point = _decompress(raw[45:78])
        return cls(depth, fingerprint, child, chain, point, network)

    def _ckd(self, chain: bytes, point, index: int):
        if index >= 0x80000000:
            raise XpubError("Hardened paths can't be derived from an xpub.")
        data = _compress(point) + index.to_bytes(4, "big")
        digest = hmac.new(chain, data, hashlib.sha512).digest()
        il = int.from_bytes(digest[:32], "big")
        if il >= _N:
            raise XpubError("Derivation failed at this index.")
        child = _add(_mul(il), point)
        if child is None:
            raise XpubError("Derivation failed at this index.")
        return digest[32:], child

    def pubkey_at(self, change: int, index: int) -> bytes:
        c1, p1 = self._ckd(self.chain, self.point, change)
        _, p2 = self._ckd(c1, p1, index)
        return _compress(p2)

    def address_at(self, index: int, change: int = 0) -> str:
        version = ADDR_VERSION[self.network]
        return b58check_encode(bytes([version]) + _hash160(self.pubkey_at(change, index)))


def pubkey_to_address(pub: bytes, version: int) -> str:
    return b58check_encode(bytes([version]) + _hash160(pub))
