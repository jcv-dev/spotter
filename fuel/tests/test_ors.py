"""Tests for the ORS HTTP client: parsing, caching, retries and errors."""

from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings

from fuel.services import ors


def json_response(payload, status=200):
    response = mock.Mock(status_code=status)
    response.json.return_value = payload
    response.text = str(payload)
    return response


GEOCODE_PAYLOAD = {
    "features": [
        {
            "geometry": {"coordinates": [-118.2437, 34.0522]},
            "properties": {"label": "Los Angeles, CA, United States", "confidence": 0.8},
        }
    ]
}

DIRECTIONS_PAYLOAD = {
    "features": [
        {
            "geometry": {
                "type": "LineString",
                "coordinates": [[-118.2437, 34.0522], [-74.006, 40.7128]],
            },
            "properties": {"summary": {"distance": 16093.44, "duration": 3600.0}},
        }
    ]
}


@override_settings(OPENROUTESERVICE_API_KEY="test-key", ORS_REQUEST_INTERVAL=0.0)
class GeocodeClientTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_geocode_parses_response_and_caches(self):
        with mock.patch(
            "fuel.services.ors.requests.request", return_value=json_response(GEOCODE_PAYLOAD)
        ) as request:
            first = ors.geocode("Los Angeles, CA")
            second = ors.geocode("  los   angeles,  ca ")

        self.assertEqual(request.call_count, 1)
        self.assertIsNotNone(first)
        self.assertAlmostEqual(first.latitude, 34.0522)
        self.assertAlmostEqual(first.longitude, -118.2437)
        self.assertEqual(first.label, "Los Angeles, CA, United States")
        self.assertEqual(first, second)
        # API key goes in the query string for geocoding.
        self.assertEqual(request.call_args.kwargs["params"]["api_key"], "test-key")

    def test_geocode_without_match_returns_none_and_caches(self):
        with mock.patch(
            "fuel.services.ors.requests.request", return_value=json_response({"features": []})
        ) as request:
            self.assertIsNone(ors.geocode("Nowhere"))
            self.assertIsNone(ors.geocode("Nowhere"))
        self.assertEqual(request.call_count, 1)

    @override_settings(OPENROUTESERVICE_API_KEY="")
    def test_missing_api_key_raises_configuration_error(self):
        with self.assertRaises(ors.ORSConfigurationError):
            ors.geocode("Los Angeles, CA")

    def test_401_raises_configuration_error(self):
        with mock.patch(
            "fuel.services.ors.requests.request",
            return_value=json_response({"error": "unauthorized"}, status=401),
        ):
            with self.assertRaises(ors.ORSConfigurationError):
                ors.geocode("Los Angeles, CA")

    def test_403_raises_quota_error(self):
        with mock.patch(
            "fuel.services.ors.requests.request",
            return_value=json_response({"error": "Quota exceeded"}, status=403),
        ):
            with self.assertRaises(ors.ORSQuotaError):
                ors.geocode("Los Angeles, CA")

    def test_429_is_retried_then_succeeds(self):
        responses = [
            json_response({"error": "rate limited"}, status=429),
            json_response(GEOCODE_PAYLOAD),
        ]
        with mock.patch("fuel.services.ors.requests.request", side_effect=responses) as request:
            with mock.patch("fuel.services.ors.time.sleep") as sleep:
                result = ors.geocode("Los Angeles, CA")
        self.assertEqual(request.call_count, 2)
        sleep.assert_called()  # backoff was applied
        self.assertAlmostEqual(result.latitude, 34.0522)

    def test_persistent_5xx_raises_request_error(self):
        with mock.patch(
            "fuel.services.ors.requests.request",
            return_value=json_response({"error": "boom"}, status=502),
        ):
            with mock.patch("fuel.services.ors.time.sleep"):
                with self.assertRaises(ors.ORSRequestError):
                    ors.geocode("Los Angeles, CA")


@override_settings(OPENROUTESERVICE_API_KEY="test-key", ORS_REQUEST_INTERVAL=0.0)
class DirectionsClientTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_directions_parses_geojson_and_caches(self):
        with mock.patch(
            "fuel.services.ors.requests.request", return_value=json_response(DIRECTIONS_PAYLOAD)
        ) as request:
            first = ors.get_directions((34.0522, -118.2437), (40.7128, -74.006))
            second = ors.get_directions((34.0522, -118.2437), (40.7128, -74.006))

        self.assertEqual(request.call_count, 1)
        self.assertEqual(len(first.coordinates), 2)
        self.assertAlmostEqual(first.distance_miles, 10.0, places=4)
        self.assertAlmostEqual(first.duration_seconds, 3600.0)
        self.assertEqual(first, second)
        # API key goes in the Authorization header for directions.
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "test-key")
