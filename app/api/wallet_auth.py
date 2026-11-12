"""
app/api/wallet_auth.py

Wallet-based authentication (Direct Wallet Mode). A user can sign in by proving
ownership of a Firo address no email or password required. This sits ALONGSIDE
the existing email/Google login; users may use either.

Flow:
  1. POST /auth/wallet/challenge  { address }      → returns a one-time nonce
  2. wallet signs the nonce with the address's private key (locally)
  3. POST /auth/wallet/verify     { address, signature }
        → node verifies the signature (RPC verifymessage)
        → creates or loads the user, returns an access token

Signature verification is delegated to the Firo node (RPC verifymessage), so no
extra crypto dependency is added.
"""
import secrets
import time
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.core.database import get_db
from app.core.security import create_access_token, _cookie_kwargs
from app.core.rate_limit import rate_limit_auth
from app.models.models import User, UserRole
from app.core.config import get_settings

router = APIRouter(prefix="/auth/wallet", tags=["wallet-auth"])

# In-memory nonce store: address → (nonce, expires_at). Small + short-lived.
_NONCES: dict[str, tuple[str, float]] = {}
_NONCE_TTL = 300  # 5 minutes


def _make_nonce(address: str) -> str:
    nonce = f"FiroGate login {secrets.token_hex(16)} @ {int(time.time())}"
    _NONCES[address] = (nonce, time.time() + _NONCE_TTL)
    now = time.time()
    for a in [k for k, (_, exp) in _NONCES.items() if exp < now]:
        _NONCES.pop(a, None)
    return nonce


def _take_nonce(address: str) -> str | None:
    item = _NONCES.pop(address, None)
    if not item:
        return None
    nonce, exp = item
    if exp < time.time():
        return None
    return nonce


class ChallengeIn(BaseModel):
    address: str

class VerifyIn(BaseModel):
    address:   str
    signature: str
    label:     str | None = None


def _valid_firo_address(a: str) -> bool:
    a = (a or "").strip()
    # Testnet addresses start with T, mainnet with a; legacy P2PKH length range
    return 26 <= len(a) <= 64 and a[0] in ("T", "a", "t")


@router.post("/challenge", dependencies=[Depends(rate_limit_auth)])
async def wallet_challenge(body: ChallengeIn):
    addr = body.address.strip()
    if not _valid_firo_address(addr):
        raise HTTPException(422, "invalid Firo address")
    nonce = _make_nonce(addr)
    return {"nonce": nonce, "expires_in": _NONCE_TTL}


@router.post("/verify", dependencies=[Depends(rate_limit_auth)])
async def wallet_verify(
    body: VerifyIn,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    addr = body.address.strip()
    if not _valid_firo_address(addr):
        raise HTTPException(422, "invalid Firo address")

    nonce = _take_nonce(addr)
    if not nonce:
        raise HTTPException(400, "no active challenge request a new one")

    from app.services.firo_rpc import get_rpc
    rpc = get_rpc()
    try:
        ok = await rpc.call("verifymessage", addr, body.signature, nonce)
    except Exception as e:
        logger.error(f"wallet verify RPC error: {e}")
        raise HTTPException(503, "could not verify signature")
    if not ok:
        raise HTTPException(401, "signature verification failed")

    res = await db.execute(select(User).where(User.wallet_address == addr))
    user = res.scalar_one_or_none()
    if user is None:
        username = f"wallet_{addr[:10].lower()}"
        exists = await db.execute(select(User).where(User.username == username))
        if exists.scalar_one_or_none():
            username = f"wallet_{addr[:10].lower()}_{secrets.token_hex(2)}"
        user = User(
            username=username,
            wallet_address=addr,
            auth_method="wallet",
            hashed_password=None,
            role=UserRole.merchant,
            is_active=True,
            merchant_setup_unlocked=True,
        )
        db.add(user)
        await db.flush()
        try:
            from app.services.analytics_service import on_user_registered
            await on_user_registered(db, user)
        except Exception:
            pass
        logger.info(f"wallet user created: {username} ({addr[:12]}…)")

    if not user.is_active:
        raise HTTPException(401, "Account is inactive.")

    from app.core.security import record_login_meta
    record_login_meta(user, request)
    user_id = user.id
    await db.commit()

    s = get_settings()
    is_onion = bool(getattr(s, "ONION_URL", "")) and (s.ONION_URL or "") in str(request.url)
    token = create_access_token(
        user_id,
        ip="" if is_onion else (request.client.host if request.client else ""),
        ua="" if is_onion else request.headers.get("user-agent", ""),
        privacy=is_onion,
    )
    response.set_cookie("access_token", token, **_cookie_kwargs(request))

    return {
        "message": "Signed in with wallet",
        "user_id": user_id,
        "address": addr,
        "access_token": token,
        "token": token,
    }
