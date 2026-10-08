"""Tests for the Nominatim geocoding client (HTTP mocked)."""

from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings

from fuel.services import nominatim
from fuel.services.errors import ProviderRequestError

NOMINATIM_PAYLOAD = [
    {
        "lat": "34.0536923",
        "lon": "-118.2427676",
        "display_name": "Los Angeles, Los Angeles County, California, United States",
    }
]


def json_response(payload, status=200):
    response = mock.Mock(status_code=status)
    response.json.return_value = payload
    response.text = str(payload)
    return response


@override_settings(NOMINATIM_REQUEST_INTERVAL=0.0)
class NominatimClientTests(TestCase):
    def setUp(self):
        cache.clear()
        nominatim._last_request_at = 0.0

    def test_geocode_parses_response_and_caches(self):
        with mock.patch("fuel.services.nominatim.session") as session:
            session.get.return_value = json_response(NOMINATIM_PAYLOAD)
            first = nominatim.geocode("Los Angeles, CA")
            second = nominatim.geocode("  los   angeles, ca ")

        request = session.get
        self.assertEqual(request.call_count, 1)
        self.assertAlmostEqual(first.latitude, 34.0536923)
        self.assertAlmostEqual(first.longitude, -118.2427676)
        self.assertIn("Los Angeles", first.label)
        self.assertEqual(first, second)
        params = request.call_args.kwargs["params"]
        self.assertEqual(params["countrycodes"], "us")
        self.assertEqual(params["format"], "jsonv2")
        self.assertIn("fuel-route-planner", request.call_args.kwargs["headers"]["User-Agent"])

    def test_rect_is_sent_as_bounded_viewbox(self):
        with mock.patch("fuel.services.nominatim.session") as session:
            session.get.return_value = json_response([])
            nominatim.geocode("Springfield", rect=(36.9, -91.6, 42.6, -87.0))
        params = session.get.call_args.kwargs["params"]
        # viewbox is left, top, right, bottom (lon/lat).
        self.assertEqual(params["viewbox"], "-91.6,42.6,-87.0,36.9")
        self.assertEqual(params["bounded"], 1)

    def test_empty_result_is_cached(self):
        with mock.patch("fuel.services.nominatim.session") as session:
            session.get.return_value = json_response([])
            self.assertIsNone(nominatim.geocode("Nowhere"))
            self.assertIsNone(nominatim.geocode("Nowhere"))
        self.assertEqual(session.get.call_count, 1)

    def test_429_is_retried_then_succeeds(self):
        responses = [json_response({"error": "rate limited"}, status=429), json_response(NOMINATIM_PAYLOAD)]
        with mock.patch("fuel.services.nominatim.session") as session:
            session.get.side_effect = responses
            with mock.patch("fuel.services.nominatim.time.sleep"):
                result = nominatim.geocode("Los Angeles, CA")
        self.assertEqual(session.get.call_count, 2)
        self.assertAlmostEqual(result.latitude, 34.0536923)

    def test_persistent_failure_raises(self):
        with mock.patch("fuel.services.nominatim.session") as session:
            session.get.return_value = json_response({"error": "boom"}, status=500)
            with mock.patch("fuel.services.nominatim.time.sleep"):
                with self.assertRaises(ProviderRequestError):
                    nominatim.geocode("Los Angeles, CA")

    def test_throttle_enforces_minimum_interval(self):
        nominatim._last_request_at = 0.0
        with override_settings(NOMINATIM_REQUEST_INTERVAL=10.0):
            with mock.patch(
                "fuel.services.nominatim.time.monotonic", side_effect=[0.0, 0.0, 20.0, 20.0]
            ):
                with mock.patch("fuel.services.nominatim.time.sleep") as sleep:
                    nominatim._throttle()  # first call waits 10s
                    nominatim._throttle()  # 20s later: no wait
        sleep.assert_called_once()
