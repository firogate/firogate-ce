"""
Price service — fetches FIRO/USD from CoinMarketCap Pro API.

Endpoint: GET https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest
Header:   X-CMC_PRO_API_KEY: <CMC_API_KEY from .env>

Two cache tiers:
  • "fresh"  — max PRICE_FRESH_SECONDS old  (checkout, withdrawal)
  • "cached" — max PRICE_CACHE_SECONDS old  (home, dashboard)

Response path:  data["FIRO"]["quote"]["USD"]["price"]
"""
import time
import asyncio
import httpx
from loguru import logger

# CMC FIRO endpoint
_CMC_URL = (
    "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
    "?symbol=FIRO&convert=USD"
)
_TIMEOUT = 6.0  # seconds

# In-memory single-entry cache
_cache: dict = {
    "price":      None,   # float | None
    "fetched_at": 0.0,    # time.monotonic() timestamp
    "lock":       None,   # asyncio.Lock — created lazily
}


def _get_lock() -> asyncio.Lock:
    if _cache["lock"] is None:
        _cache["lock"] = asyncio.Lock()
    return _cache["lock"]


async def _fetch_from_cmc() -> float | None:
    """
    Single HTTP call to CoinMarketCap.
    Returns the FIRO/USD price as a float, or None on any error.
    """
    from app.core.config import get_settings
    api_key = get_settings().CMC_API_KEY

    if not api_key:
        logger.warning("[price] CMC_API_KEY not set in .env — price unavailable")
        return None

    headers = {
        "X-CMC_PRO_API_KEY": api_key,
        "Accept":            "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(_CMC_URL, headers=headers)
            r.raise_for_status()
            body = r.json()

            # Response structure:
            # { "data": { "FIRO": { "quote": { "USD": { "price": 1.23 } } } } }
            price = (
                body
                .get("data", {})
                .get("FIRO", {})
                .get("quote", {})
                .get("USD", {})
                .get("price")
            )
            if price is not None:
                price = float(price)
                if price > 0:
                    logger.debug(f"[price] CMC FIRO/USD = {price}")
                    return price
                logger.warning(f"[price] CMC returned non-positive price: {price}")
            else:
                logger.warning(f"[price] CMC response missing price: {body}")

    except httpx.HTTPStatusError as e:
        logger.warning(f"[price] CMC HTTP {e.response.status_code}: {e.response.text[:200]}")
    except Exception as e:
        logger.warning(f"[price] CMC fetch error: {e}")

    return None


async def get_firo_price(fresh: bool = False) -> float | None:
    """
    Returns FIRO/USD price.

    fresh=True  → bypass cache when older than PRICE_FRESH_SECONDS  (checkout, withdrawal)
    fresh=False → use longer cache PRICE_CACHE_SECONDS               (home, dashboard)
    """
    from app.core.config import get_settings
    s   = get_settings()
    ttl = s.PRICE_FRESH_SECONDS if fresh else s.PRICE_CACHE_SECONDS
    age = time.monotonic() - _cache["fetched_at"]

    # Fast path — cache still valid
    if _cache["price"] is not None and age < ttl:
        return _cache["price"]

    # Slow path — fetch under lock so parallel requests share one HTTP call
    async with _get_lock():
        # Re-check inside lock
        age = time.monotonic() - _cache["fetched_at"]
        if _cache["price"] is not None and age < ttl:
            return _cache["price"]

        price = await _fetch_from_cmc()
        if price is not None:
            _cache["price"]      = price
            _cache["fetched_at"] = time.monotonic()
        elif _cache["price"] is not None:
            # Return stale value rather than None — better UX
            logger.warning("[price] Fetch failed — returning stale cached price")
            return _cache["price"]

    return _cache["price"]


async def firo_to_usd(firo_amount: float) -> float | None:
    """Convert FIRO → USD using current price."""
    price = await get_firo_price(fresh=True)
    return round(firo_amount * price, 2) if price else None


async def usd_to_firo(usd_amount: float) -> float | None:
    """Convert USD → FIRO using current price."""
    price = await get_firo_price(fresh=True)
    return round(usd_amount / price, 8) if price else None
