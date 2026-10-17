import unittest
from unittest.mock import AsyncMock, patch

import httpx

import utils.geocoding as geocoding


class FailedClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def get(self, *args, **kwargs):
        raise httpx.ConnectError("offline", request=httpx.Request("GET", "https://example.test"))


class GeocodingTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_request_still_obeys_rate_limit(self):
        geocoding._last_request_at = 0
        with (
            patch("utils.geocoding.time.monotonic", side_effect=[100.0, 100.0, 100.2, 100.2]),
            patch("utils.geocoding.asyncio.sleep", new_callable=AsyncMock) as sleep,
            patch("utils.geocoding.httpx.AsyncClient", return_value=FailedClient()),
        ):
            self.assertIsNone(await geocoding.geocode_bishkek_address("Street 1", "https://example.test", "test"))
            self.assertIsNone(await geocoding.geocode_bishkek_address("Street 2", "https://example.test", "test"))

        sleep.assert_awaited_once()
        self.assertAlmostEqual(sleep.await_args.args[0], 0.8)


if __name__ == "__main__":
    unittest.main()