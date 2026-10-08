"""Tests for the route artifact caches, gzip and the station data version."""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

import numpy as np
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from fuel.models import FuelStation
from fuel.services import routing
from fuel.services.geo import simplify_polyline
from fuel.services.ors import DirectionsResult

SHORT_ROUTE = DirectionsResult(
    coordinates=[(34.0, -118.0), (35.0, -118.0)],
    distance_miles=69.0,
    duration_seconds=3600.0,
)


@override_settings(
    ROUTING_PROVIDER="ors",
    GEOCODING_PROVIDER="ors",
    ORS_REQUEST_INTERVAL=0.0,
    RATE_LIMIT_REQUESTS_PER_MINUTE=0,
)
class RouteArtifactCacheTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_simplified_geometry_and_candidates_are_cached(self):
        start = {"lat": 34.0, "lon": -118.0}
        finish = {"lat": 35.0, "lon": -118.0}

        with mock.patch(
            "fuel.services.ors.get_directions", return_value=SHORT_ROUTE
        ), mock.patch(
            "fuel.services.routing.simplify_polyline", wraps=simplify_polyline
        ) as simplify, mock.patch(
            "fuel.services.routing._load_candidates", wraps=routing._load_candidates
        ) as load_candidates:
            first = routing.build_route_response(start, finish, True)
            second = routing.build_route_response(start, finish, True)

        # The expensive work happens once, the second request is served from cache.
        self.assertEqual(simplify.call_count, 1)
        self.assertEqual(load_candidates.call_count, 1)
        self.assertEqual(first["route"], second["route"])
        self.assertEqual(first["total_fuel_cost"], second["total_fuel_cost"])

    def test_candidates_cache_refreshes_when_station_data_changes(self):
        start = {"lat": 34.0, "lon": -118.0}
        finish = {"lat": 35.0, "lon": -118.0}

        with mock.patch("fuel.services.ors.get_directions", return_value=SHORT_ROUTE):
            routing.build_route_response(start, finish, True)

            FuelStation.objects.create(
                name="NEW STOP", address="1 Main St", city="Town", state="CA",
                latitude=34.5, longitude=-118.0, price=Decimal("3.000"),
            )
            cache.delete("fuel:stations-version")
            with mock.patch(
                "fuel.services.routing._load_candidates", wraps=routing._load_candidates
            ) as load_candidates:
                routing.build_route_response(start, finish, True)

        # Data changed -> the candidate cache is rebuilt.
        self.assertEqual(load_candidates.call_count, 1)

    def test_stations_version_changes_after_updates(self):
        cache.clear()
        first = routing._stations_version()
        FuelStation.objects.create(
            name="A", address="1 Main St", city="Town", state="CA",
            latitude=34.0, longitude=-118.0, price=Decimal("3.000"),
        )
        cache.delete("fuel:stations-version")
        second = routing._stations_version()
        self.assertNotEqual(first, second)


@override_settings(RATE_LIMIT_REQUESTS_PER_MINUTE=0, ORS_REQUEST_INTERVAL=0.0)
class GzipTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def test_json_response_is_gzipped_when_accepted(self):
        with mock.patch("fuel.services.ors.get_directions", return_value=SHORT_ROUTE):
            response = self.client.post(
                "/api/route/",
                {"start": {"lat": 34.0, "lon": -118.0}, "finish": {"lat": 35.0, "lon": -118.0}},
                format="json",
                HTTP_ACCEPT_ENCODING="gzip",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Content-Encoding"), "gzip")

    def test_json_response_is_plain_without_accept_encoding(self):
        with mock.patch("fuel.services.ors.get_directions", return_value=SHORT_ROUTE):
            response = self.client.post(
                "/api/route/",
                {"start": {"lat": 34.0, "lon": -118.0}, "finish": {"lat": 35.0, "lon": -118.0}},
                format="json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Content-Encoding", response.headers)
