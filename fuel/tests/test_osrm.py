"""Tests for the OSRM routing client (HTTP mocked)."""

from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings

from fuel.services import osrm
from fuel.services.errors import ProviderRequestError
from fuel.tests.test_ors import encode_polyline

ROUTE_COORDS = [(34.0522, -118.2437), (40.7128, -74.006)]
OSRM_PAYLOAD = {
    "code": "Ok",
    "routes": [
        {
            "geometry": encode_polyline(ROUTE_COORDS),
            "distance": 16093.44,
            "duration": 3600.0,
        }
    ],
}


def json_response(payload, status=200):
    response = mock.Mock(status_code=status)
    response.json.return_value = payload
    response.text = str(payload)
    return response


@override_settings(ROUTING_PROVIDER="osrm")
class OSRMClientTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_directions_parses_response_and_caches(self):
        with mock.patch("fuel.services.osrm.session") as session:
            session.get.return_value = json_response(OSRM_PAYLOAD)
            first = osrm.get_directions((34.0522, -118.2437), (40.7128, -74.006))
            second = osrm.get_directions((34.0522, -118.2437), (40.7128, -74.006))

        request = session.get
        self.assertEqual(request.call_count, 1)
        self.assertEqual(len(first.coordinates), 2)
        self.assertAlmostEqual(first.coordinates[0][0], 34.0522, places=5)
        self.assertAlmostEqual(first.distance_miles, 10.0, places=4)
        self.assertAlmostEqual(first.duration_seconds, 3600.0)
        self.assertEqual(first, second)
        # Coordinates are sent in lon,lat order in the URL; compact polyline
        # geometry keeps the response small.
        self.assertIn("/-118.2437,34.0522;-74.006,40.7128", request.call_args.args[0])
        self.assertEqual(request.call_args.kwargs["params"]["geometries"], "polyline")
        self.assertIn("fuel-route-planner", request.call_args.kwargs["headers"]["User-Agent"])

    def test_no_route_raises_request_error(self):
        with mock.patch("fuel.services.osrm.session") as session:
            session.get.return_value = json_response({"code": "NoRoute", "routes": []})
            with self.assertRaises(ProviderRequestError):
                osrm.get_directions((34.0522, -118.2437), (40.7128, -74.006))

    def test_429_is_retried_then_succeeds(self):
        responses = [json_response({"message": "rate limited"}, status=429), json_response(OSRM_PAYLOAD)]
        with mock.patch("fuel.services.osrm.session") as session:
            session.get.side_effect = responses
            with mock.patch("fuel.services.osrm.time.sleep") as sleep:
                result = osrm.get_directions((34.0522, -118.2437), (40.7128, -74.006))
        self.assertEqual(session.get.call_count, 2)
        sleep.assert_called()
        self.assertAlmostEqual(result.distance_miles, 10.0, places=4)

    def test_persistent_5xx_raises(self):
        with mock.patch("fuel.services.osrm.session") as session:
            session.get.return_value = json_response({"error": "boom"}, status=502)
            with mock.patch("fuel.services.osrm.time.sleep"):
                with self.assertRaises(ProviderRequestError):
                    osrm.get_directions((34.0522, -118.2437), (40.7128, -74.006))
