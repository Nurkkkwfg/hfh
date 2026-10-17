import asyncio
import time

import httpx

_request_lock = asyncio.Lock()
_last_request_at = 0.0


async def geocode_bishkek_address(address: str, endpoint: str, user_agent: str) -> tuple[float, float] | None:
    """Geocode an address using Nominatim-compatible JSON search; failures are non-fatal."""
    global _last_request_at
    async with _request_lock:
        delay = 1.0 - (time.monotonic() - _last_request_at)
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            async with httpx.AsyncClient(timeout=8.0, headers={"User-Agent": user_agent}) as client:
                _last_request_at = time.monotonic()
                response = await client.get(
                    endpoint,
                    params={"q": f"{address}, Bishkek, Kyrgyzstan", "format": "jsonv2", "limit": 1},
                )
                response.raise_for_status()
                results = response.json()
        except (httpx.HTTPError, ValueError):
            return None

    if not results:
        return None
    try:
        return float(results[0]["lat"]), float(results[0]["lon"])
    except (KeyError, TypeError, ValueError, IndexError):
        return None