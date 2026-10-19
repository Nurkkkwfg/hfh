import asyncio
import logging
import time

import httpx

logger = logging.getLogger(__name__)

_request_lock = asyncio.Lock()
_last_request_at = 0.0


async def geocode_bishkek_address(
    address: str,
    endpoint: str | None = None,
    user_agent: str | None = None,
) -> tuple[float, float] | None:
    global _last_request_at

    query = f"{address}, Bishkek, Kyrgyzstan"
    headers = {"User-Agent": user_agent or "LaundryDispatcherBot/1.0 (contact@example.com)"}

    async with _request_lock:
        delay = 1.0 - (time.monotonic() - _last_request_at)
        if delay > 0:
            await asyncio.sleep(delay)

        try:
            async with httpx.AsyncClient(timeout=8.0, headers=headers) as client:
                _last_request_at = time.monotonic()
                response = await client.get(
                    "https://photon.komoot.io/api/",
                    params={"q": query, "limit": 1, "lang": "ru"},
                )
                response.raise_for_status()
                data = response.json()

            features = data.get("features") or []
            if features:
                coords = features[0]["geometry"]["coordinates"]
                lon, lat = float(coords[0]), float(coords[1])
                return lat, lon
        except Exception as e:
            logger.warning("Photon geocode failed for %s: %s", address, e)

        if endpoint:
            try:
                async with httpx.AsyncClient(timeout=8.0, headers=headers) as client:
                    _last_request_at = time.monotonic()
                    response = await client.get(
                        endpoint,
                        params={
                            "q": query,
                            "format": "jsonv2",
                            "limit": 1,
                            "email": "contact@example.com",
                        },
                    )
                    response.raise_for_status()
                    results = response.json()

                if results:
                    return float(results[0]["lat"]), float(results[0]["lon"])
            except Exception as e:
                logger.warning("Nominatim geocode failed for %s: %s", address, e)

    return None
