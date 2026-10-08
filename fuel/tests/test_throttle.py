"""Tests for the per-IP rate limiting middleware."""

from __future__ import annotations

from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from fuel.services.ors import DirectionsResult

SHORT_ROUTE = DirectionsResult(
    coordinates=[(34.0, -118.0), (35.0, -118.0)],
    distance_miles=69.0,
    duration_seconds=3600.0,
)

API_PAYLOAD = {
    "start": {"lat": 34.0, "lon": -118.0},
    "finish": {"lat": 35.0, "lon": -118.0},
}


@override_settings(
    RATE_LIMIT_REQUESTS_PER_MINUTE=2,
    ROUTING_PROVIDER="ors",
    GEOCODING_PROVIDER="ors",
    ORS_REQUEST_INTERVAL=0.0,
)
class RateLimitTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def test_api_allows_up_to_limit_then_returns_429(self):
        with mock.patch("fuel.services.ors.get_directions", return_value=SHORT_ROUTE):
            first = self.client.post("/api/route/", API_PAYLOAD, format="json")
            second = self.client.post("/api/route/", API_PAYLOAD, format="json")
            third = self.client.post("/api/route/", API_PAYLOAD, format="json")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(third.status_code, 429)
        self.assertEqual(third.json()["code"], "rate_limited")
        self.assertIn("Retry-After", third.headers)

        # Rate limit headers are present on allowed responses too.
        self.assertEqual(first.headers["X-RateLimit-Limit"], "2")
        self.assertEqual(first.headers["X-RateLimit-Remaining"], "1")
        self.assertEqual(second.headers["X-RateLimit-Remaining"], "0")
        self.assertEqual(third.headers["X-RateLimit-Remaining"], "0")

    def test_map_endpoint_is_limited_too(self):
        url = "/map/?start=34.0,-118.0&finish=35.0,-118.0"
        with mock.patch("fuel.services.ors.get_directions", return_value=SHORT_ROUTE):
            responses = [self.client.get(url) for _ in range(3)]
        self.assertEqual([r.status_code for r in responses], [200, 200, 429])

    def test_health_is_exempt(self):
        with override_settings(RATE_LIMIT_REQUESTS_PER_MINUTE=1):
            for _ in range(4):
                response = self.client.get("/health/")
                self.assertEqual(response.status_code, 200)

    def test_zero_disables_the_limit(self):
        with override_settings(RATE_LIMIT_REQUESTS_PER_MINUTE=0):
            with mock.patch("fuel.services.ors.get_directions", return_value=SHORT_ROUTE):
                for _ in range(4):
                    response = self.client.post("/api/route/", API_PAYLOAD, format="json")
                    self.assertEqual(response.status_code, 200)

    def test_forwarded_for_is_used_for_the_client_identity(self):
        with mock.patch("fuel.services.ors.get_directions", return_value=SHORT_ROUTE):
            for _ in range(2):
                response = self.client.post(
                    "/api/route/", API_PAYLOAD, format="json", HTTP_X_FORWARDED_FOR="10.0.0.1"
                )
                self.assertEqual(response.status_code, 200)
            # A different client behind the proxy gets its own bucket.
            response = self.client.post(
                "/api/route/", API_PAYLOAD, format="json", HTTP_X_FORWARDED_FOR="10.0.0.2"
            )
            self.assertEqual(response.status_code, 200)
            # The first client is now over its limit.
            response = self.client.post(
                "/api/route/", API_PAYLOAD, format="json", HTTP_X_FORWARDED_FOR="10.0.0.1"
            )
            self.assertEqual(response.status_code, 429)
