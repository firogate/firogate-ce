from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import subprocess
import tempfile
import time

import httpx

from app.core import xpub as X

RPC_USER = "rt"
RPC_PASS = "rtpass"


def available() -> bool:
    return bool(os.environ.get("FIROGATE_REGTEST")) and shutil.which("firod") is not None


class Node:
    def __init__(self, root: str, name: str, rpc_port: int, p2p_port: int, extra: list[str]):
        self.dir = os.path.join(root, name)
        self.rpc_port = rpc_port
        self.p2p_port = p2p_port
        os.makedirs(self.dir, exist_ok=True)
        lines = ["regtest=1", "server=1", "listen=1", "dandelion=0", "txindex=1", "fallbackfee=0.0001",
                 f"rpcuser={RPC_USER}", f"rpcpassword={RPC_PASS}", f"rpcport={rpc_port}", f"port={p2p_port}",
                 "rpcbind=127.0.0.1", "rpcallowip=127.0.0.1", "dnsseed=0", *extra]
        with open(os.path.join(self.dir, "firo.conf"), "w") as f:
            f.write("\n".join(lines) + "\n")

    def start(self) -> None:
        subprocess.run(["firod", f"-datadir={self.dir}", f"-conf={self.dir}/firo.conf", "-daemon"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(90):
            try:
                self.rpc("getblockcount")
                return
            except Exception:
                time.sleep(1)
        raise RuntimeError(f"node {self.dir} did not start")

    def _pid(self) -> int | None:
        for path in (os.path.join(self.dir, "regtest", "firod.pid"), os.path.join(self.dir, "firod.pid")):
            try:
                return int(open(path).read().strip())
            except (OSError, ValueError):
                continue
        return None

    def stop(self) -> None:
        pid = self._pid()
        try:
            self.rpc("stop")
        except Exception:
            pass
        for _ in range(120):
            alive = pid is not None and os.path.exists(f"/proc/{pid}")
            if not alive and self._pid() is None:
                return
            time.sleep(0.5)

    def rpc(self, method: str, *params):
        r = httpx.post(f"http://127.0.0.1:{self.rpc_port}/", auth=(RPC_USER, RPC_PASS),
                       json={"jsonrpc": "1.0", "id": 1, "method": method, "params": list(params)}, timeout=120)
        j = r.json()
        if j.get("error"):
            raise RuntimeError(f"{method}: {j['error']}")
        return j["result"]


class Regtest:
    def __init__(self, base_port: int = 28930):
        self.root = tempfile.mkdtemp(prefix="fg-mint-regtest-")
        self.a = Node(self.root, "a", base_port + 1, base_port + 11,
                      ["addressindex=1", "spentindex=1", "timestampindex=1", "mobile=1"])
        self.b = Node(self.root, "b", base_port + 2, base_port + 12, ["usemnemonic=0"])

    def __enter__(self):
        self.a.start()
        self.b.start()
        self.connect()
        self.a.rpc("generate", 310)
        self.sync()
        return self

    def __exit__(self, *exc):
        self.b.stop()
        self.a.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def connect(self) -> None:
        try:
            self.b.rpc("addnode", f"127.0.0.1:{self.a.p2p_port}", "onetry")
        except RuntimeError:
            pass
        for _ in range(30):
            if self.a.rpc("getconnectioncount") > 0:
                return
            time.sleep(0.5)

    def sync(self) -> None:
        for _ in range(120):
            if self.a.rpc("getbestblockhash") == self.b.rpc("getbestblockhash"):
                return
            time.sleep(0.5)
        raise RuntimeError("nodes did not sync")

    def mine(self, n: int = 1) -> None:
        self.a.rpc("generate", n)
        self.sync()

    def wait_mempool(self, txids: list[str]) -> None:
        for _ in range(60):
            pool = set(self.a.rpc("getrawmempool"))
            if all(t in pool for t in txids):
                return
            time.sleep(0.5)
        raise RuntimeError("transactions did not reach node A")

    def restart_a_without_mempool(self) -> None:
        self.a.stop()
        try:
            os.remove(os.path.join(self.a.dir, "regtest", "mempool.dat"))
        except FileNotFoundError:
            pass
        self.a.start()


class MerchantSeed:

    WIF_PREFIX = 239

    def __init__(self, seed: bytes | None = None):
        seed = seed or os.urandom(32)
        k, c = self._master(seed)
        parent = None
        for i in (44 | 0x80000000, 1 | 0x80000000, 0 | 0x80000000):
            parent = X._compress(X._mul(k))
            k, c = self._ckd(k, c, i)
        self._k, self._c = k, c
        fp = hashlib.new("ripemd160", hashlib.sha256(parent).digest()).digest()[:4]
        raw = X.XPUB_TEST + bytes([3]) + fp + (0x80000000).to_bytes(4, "big") + c + X._compress(X._mul(k))
        self.tpub = X.b58check_encode(raw)

    @staticmethod
    def _master(seed: bytes):
        i = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
        return int.from_bytes(i[:32], "big"), i[32:]

    @staticmethod
    def _ckd(k: int, c: bytes, i: int):
        if i >= 0x80000000:
            data = b"\x00" + k.to_bytes(32, "big") + i.to_bytes(4, "big")
        else:
            data = X._compress(X._mul(k)) + i.to_bytes(4, "big")
        h = hmac.new(c, data, hashlib.sha512).digest()
        return (int.from_bytes(h[:32], "big") + k) % X._N, h[32:]

    def wif(self, index: int) -> str:
        k, c = self._ckd(self._k, self._c, 0)
        k, _ = self._ckd(k, c, index)
        return X.b58check_encode(bytes([self.WIF_PREFIX]) + k.to_bytes(32, "big") + b"\x01")
