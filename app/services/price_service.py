import time
import asyncio
import httpx
from loguru import logger

_TIMEOUT = 6.0
_DIRECT_CMC_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"

_cache: dict = {
    "price":      None,
    "fetched_at": 0.0,
    "lock":       None,
    "refreshing": False,
}


def _get_lock() -> asyncio.Lock:
    if _cache["lock"] is None:
        _cache["lock"] = asyncio.Lock()
    return _cache["lock"]


async def _fetch_from_provider() -> float | None:
    from app.core.config import get_settings
    s = get_settings()
    if not s.CMC_API_KEY:
        logger.warning("[price] CMC_API_KEY not set in .env")
        return None

    headers = {
        "X-CMC_PRO_API_KEY": s.CMC_API_KEY,
        "Accept":            "application/json",
    }
    params = {"symbol": "FIRO", "convert": "USD"}

    def _extract(body: dict) -> float | None:
        price = (
            body
            .get("data", {})
            .get("FIRO", {})
            .get("quote", {})
            .get("USD", {})
            .get("price")
        )
        if price is None:
            logger.warning(f"[price] response missing price: {body}")
            return None
        price = float(price)
        if price <= 0:
            logger.warning(f"[price] non-positive price: {price}")
            return None
        return price

    # Preferred: through the local nginx proxy (see PRICE_PROXY_URL), so
    # the provider only ever sees this server's IP. Falls back to calling
    # the provider directly if that proxy isn't reachable (e.g. the nginx
    # block hasn't been deployed yet) — a missing proxy setup step
    # shouldn't take the whole price feature down.
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(s.PRICE_PROXY_URL, headers=headers, params=params)
            r.raise_for_status()
            price = _extract(r.json())
            if price is not None:
                return price
    except httpx.HTTPStatusError as e:
        logger.warning(f"[price] proxy HTTP {e.response.status_code}: {e.response.text[:200]}")
    except Exception as e:
        logger.warning(f"[price] proxy unreachable ({e}), falling back to direct call")
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.get(_DIRECT_CMC_URL, headers=headers, params=params)
                r.raise_for_status()
                price = _extract(r.json())
                if price is not None:
                    return price
        except httpx.HTTPStatusError as e2:
            logger.warning(f"[price] direct HTTP {e2.response.status_code}: {e2.response.text[:200]}")
        except Exception as e2:
            logger.warning(f"[price] direct fetch error: {e2}")

    return None


async def _refresh_cache() -> None:
    async with _get_lock():
        price = await _fetch_from_provider()
        if price is not None:
            _cache["price"]      = price
            _cache["fetched_at"] = time.monotonic()
        elif _cache["price"] is not None:
            logger.warning("[price] refresh failed, keeping stale price")


async def _background_refresh() -> None:
    if _cache["refreshing"]:
        return
    _cache["refreshing"] = True
    try:
        await _refresh_cache()
    finally:
        _cache["refreshing"] = False


async def get_firo_price(fresh: bool = False) -> float | None:
    from app.core.config import get_settings
    s   = get_settings()
    ttl = s.PRICE_FRESH_SECONDS if fresh else s.PRICE_CACHE_SECONDS
    age = time.monotonic() - _cache["fetched_at"]

    if _cache["price"] is not None:
        if age >= ttl:
            asyncio.create_task(_background_refresh())
        return _cache["price"]

    await _refresh_cache()
    return _cache["price"]


async def firo_to_usd(firo_amount: float) -> float | None:
    price = await get_firo_price(fresh=True)
    return round(firo_amount * price, 2) if price else None


async def usd_to_firo(usd_amount: float) -> float | None:
    price = await get_firo_price(fresh=True)
    return round(usd_amount / price, 8) if price else None
