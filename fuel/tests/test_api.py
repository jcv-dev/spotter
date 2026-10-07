"""Integration tests for POST /api/route/ and GET /map/ (ORS mocked)."""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

import numpy as np
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from fuel.models import FuelStation
from fuel.services import geo, ors
from fuel.services.geo import simplify_polyline
from fuel.services.ors import DirectionsResult, GeocodeResult

LOS_ANGELES = (34.0522, -118.2437)
NEW_YORK = (40.7128, -74.0060)


def straight_route(start, finish, points=400):
    return np.column_stack(
        [np.linspace(start[0], finish[0], points), np.linspace(start[1], finish[1], points)]
    )


def point_at(coords, fraction):
    cum = geo.cumulative_miles(coords)
    target = cum[-1] * fraction
    index = max(0, min(int(np.searchsorted(cum, target, side="right")) - 1, len(coords) - 2))
    segment = cum[index + 1] - cum[index]
    t = 0.0 if segment <= 0 else (target - cum[index]) / segment
    latitude = coords[index, 0] + t * (coords[index + 1, 0] - coords[index, 0])
    longitude = coords[index, 1] + t * (coords[index + 1, 1] - coords[index, 1])
    return float(latitude), float(longitude)


@override_settings(ORS_REQUEST_INTERVAL=0.0)
class RouteAPITests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.coords = straight_route(LOS_ANGELES, NEW_YORK)
        # The service simplifies polylines; place stations on the same route so
        # the distance-along values match exactly.
        self.route_coords = simplify_polyline(self.coords, tolerance_miles=0.005)

    # ------------------------------------------------------------- helpers ---
    def mock_ors(self, coordinates=None, geocode_map=None, directions_error=None):
        coordinates = self.coords if coordinates is None else coordinates
        cum = geo.cumulative_miles(coordinates)
        directions = DirectionsResult(
            coordinates=[(float(lat), float(lon)) for lat, lon in coordinates],
            distance_miles=float(cum[-1]),
            duration_seconds=3600.0,
        )
        if directions_error is not None:
            directions_patch = mock.patch(
                "fuel.services.ors.get_directions", side_effect=directions_error
            )
        else:
            directions_patch = mock.patch("fuel.services.ors.get_directions", return_value=directions)
        self.addCleanup(directions_patch.stop)
        directions_patch.start()

        if geocode_map is None:
            geocode_map = {
                "los angeles": GeocodeResult(*LOS_ANGELES, label="Los Angeles, CA, United States"),
                "new york": GeocodeResult(*NEW_YORK, label="New York, NY, United States"),
            }

        def fake_geocode(query, country="US"):
            lowered = query.lower()
            for key, value in geocode_map.items():
                if key in lowered:
                    return value
            return None

        geocode_patch = mock.patch("fuel.services.ors.geocode", side_effect=fake_geocode)
        self.addCleanup(geocode_patch.stop)
        geocode_patch.start()

    def add_station(self, coords, fraction, price, name=None):
        latitude, longitude = point_at(coords, fraction)
        return FuelStation.objects.create(
            name=name or f"Station {fraction:.2f}",
            address="1 Main St",
            city="Town",
            state="TX",
            latitude=latitude,
            longitude=longitude,
            price=Decimal(str(price)),
        )

    # ---------------------------------------------------------------- tests ---
    def test_route_with_addresses_returns_fuel_plan(self):
        self.mock_ors()
        for fraction, price in [(1 / 6, 3.5), (2 / 6, 3.0), (3 / 6, 3.2), (4 / 6, 2.9), (5 / 6, 3.1)]:
            self.add_station(self.route_coords, fraction, price)

        response = self.client.post(
            "/api/route/",
            {"start": {"address": "Los Angeles, CA"}, "finish": {"address": "New York, NY"}},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()

        self.assertEqual(payload["route"]["type"], "LineString")
        self.assertEqual(len(payload["route"]["coordinates"]), len(self.route_coords))
        self.assertGreater(payload["total_distance_miles"], 2000)
        self.assertTrue(payload["start_full_tank"])

        stops = payload["fuel_stops"]
        self.assertGreaterEqual(len(stops), 1)
        for stop in stops:
            for key in (
                "name", "address", "city", "state", "latitude", "longitude", "price",
                "gallons_purchased", "cost", "distance_from_start_miles", "detour_miles",
            ):
                self.assertIn(key, stop)

        self.assertAlmostEqual(
            sum(stop["cost"] for stop in stops), payload["total_fuel_cost"], delta=0.05
        )
        self.assertAlmostEqual(
            sum(stop["gallons_purchased"] for stop in stops),
            payload["total_gallons_purchased"],
            delta=0.05,
        )
        self.assertGreater(payload["total_fuel_cost"], 0)
        self.assertTrue(payload["map_url"].startswith("/map/?"))
        self.assertIn("start=Los+Angeles", payload["map_url"])

    def test_route_with_coordinates(self):
        self.mock_ors()
        for fraction, price in [(0.2, 3.0), (0.4, 3.0), (0.6, 3.0), (0.8, 3.0)]:
            self.add_station(self.route_coords, fraction, price)

        response = self.client.post(
            "/api/route/",
            {
                "start": {"lat": LOS_ANGELES[0], "lon": LOS_ANGELES[1]},
                "finish": {"lat": NEW_YORK[0], "lon": NEW_YORK[1]},
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["start"]["label"], "34.0522, -118.2437")
        self.assertIn("start=34.052200", payload["map_url"])

    def test_empty_tank_first_stop_is_at_the_start(self):
        self.mock_ors()
        self.add_station(self.route_coords, 0.0, 3.0, name="Start Fuel")
        for fraction in (0.2, 0.4, 0.6, 0.8):
            self.add_station(self.route_coords, fraction, 3.0)

        response = self.client.post(
            "/api/route/",
            {
                "start": {"lat": LOS_ANGELES[0], "lon": LOS_ANGELES[1]},
                "finish": {"lat": NEW_YORK[0], "lon": NEW_YORK[1]},
                "start_full_tank": False,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        stops = response.json()["fuel_stops"]
        self.assertTrue(stops[0]["is_start_fuel"])
        self.assertEqual(stops[0]["name"], "Start Fuel")
        self.assertEqual(stops[0]["distance_from_start_miles"], 0.0)
        # Full tank start would need fewer gallons purchased overall.
        self.assertFalse(response.json()["start_full_tank"])

    def test_outside_usa_coordinates_rejected(self):
        response = self.client.post(
            "/api/route/",
            {
                "start": {"lat": 51.5074, "lon": -0.1278},  # London
                "finish": {"lat": NEW_YORK[0], "lon": NEW_YORK[1]},
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("outside the USA", response.json()["error"])

    def test_outside_usa_address_rejected(self):
        self.mock_ors(
            geocode_map={
                "mexico city": GeocodeResult(19.4326, -99.1332, label="Mexico City, Mexico"),
                "new york": GeocodeResult(*NEW_YORK, label="New York, NY, United States"),
            }
        )
        response = self.client.post(
            "/api/route/",
            {"start": {"address": "Mexico City"}, "finish": {"address": "New York, NY"}},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("outside the USA", response.json()["error"])

    def test_malformed_payloads_rejected(self):
        cases = [
            {},
            {"start": {"address": "Los Angeles, CA"}},
            {"start": {"address": "Los Angeles, CA"}, "finish": {"lat": 40.7}},
            {
                "start": {"address": "Los Angeles, CA", "lat": 34.0, "lon": -118.0},
                "finish": {"address": "New York, NY"},
            },
            {"start": {"lat": 34.0, "lon": -118.0}, "finish": {"address": "  "}},
        ]
        for case in cases:
            response = self.client.post("/api/route/", case, format="json")
            self.assertEqual(response.status_code, 400, msg=str(case))

    def test_unresolvable_address_returns_400(self):
        self.mock_ors(geocode_map={})
        response = self.client.post(
            "/api/route/",
            {"start": {"address": "Nowhere Land"}, "finish": {"address": "New York, NY"}},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Could not geocode", response.json()["error"])

    def test_infeasible_route_returns_422(self):
        # 1200 mile straight route with a single station 300 miles in.
        route = straight_route((30.0, -100.0), (47.35, -100.0))
        self.mock_ors(coordinates=route)
        self.add_station(route, 0.25, 3.0)
        response = self.client.post(
            "/api/route/",
            {
                "start": {"lat": 30.0, "lon": -100.0},
                "finish": {"lat": 47.35, "lon": -100.0},
            },
            format="json",
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "no_feasible_fuel_plan")

    def test_empty_tank_without_station_near_start_returns_422(self):
        route = straight_route((30.0, -100.0), (38.0, -100.0))  # ~553 miles
        self.mock_ors(coordinates=route)
        self.add_station(route, 0.5, 3.0)  # ~276 miles away, unreachable from empty
        response = self.client.post(
            "/api/route/",
            {
                "start": {"lat": 30.0, "lon": -100.0},
                "finish": {"lat": 38.0, "lon": -100.0},
                "start_full_tank": False,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 422)

    def test_missing_api_key_returns_503(self):
        self.mock_ors(directions_error=ors.ORSConfigurationError("OPENROUTESERVICE_API_KEY is not configured"))
        response = self.client.post(
            "/api/route/",
            {
                "start": {"lat": LOS_ANGELES[0], "lon": LOS_ANGELES[1]},
                "finish": {"lat": NEW_YORK[0], "lon": NEW_YORK[1]},
            },
            format="json",
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "ors_not_configured")

    def test_upstream_failure_returns_502(self):
        self.mock_ors(directions_error=ors.ORSRequestError("upstream exploded"))
        response = self.client.post(
            "/api/route/",
            {
                "start": {"lat": LOS_ANGELES[0], "lon": LOS_ANGELES[1]},
                "finish": {"lat": NEW_YORK[0], "lon": NEW_YORK[1]},
            },
            format="json",
        )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["code"], "route_unavailable")


@override_settings(ORS_REQUEST_INTERVAL=0.0)
class MapViewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.coords = straight_route(LOS_ANGELES, NEW_YORK)
        self.route_coords = simplify_polyline(self.coords, tolerance_miles=0.005)
        self.directions = DirectionsResult(
            coordinates=[(float(lat), float(lon)) for lat, lon in self.coords],
            distance_miles=float(geo.cumulative_miles(self.coords)[-1]),
            duration_seconds=3600.0,
        )
        for fraction, price in [(1 / 6, 3.5), (2 / 6, 3.0), (3 / 6, 3.2), (4 / 6, 2.9), (5 / 6, 3.1)]:
            latitude, longitude = point_at(self.route_coords, fraction)
            FuelStation.objects.create(
                name=f"Station {fraction:.2f}",
                address="1 Main St",
                city="Town",
                state="TX",
                latitude=latitude,
                longitude=longitude,
                price=Decimal(str(price)),
            )

    def _patch_directions(self):
        patch = mock.patch("fuel.services.ors.get_directions", return_value=self.directions)
        self.addCleanup(patch.stop)
        patch.start()

    def _patch_geocode(self, result):
        patch = mock.patch("fuel.services.ors.geocode", return_value=result)
        self.addCleanup(patch.stop)
        patch.start()

    def test_map_page_renders_route_and_stops(self):
        self._patch_directions()
        self._patch_geocode(GeocodeResult(*LOS_ANGELES, label="Los Angeles, CA, United States"))
        response = self.client.get("/map/", {"start": "Los Angeles, CA", "finish": "New York, NY"})
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("leaflet", content)
        self.assertIn("route-data", content)
        self.assertIn("Station 0.17", content)
        self.assertIn("Fuel stops", content)

    def test_map_page_shows_error_state(self):
        self._patch_directions()
        self._patch_geocode(None)
        response = self.client.get("/map/", {"start": "Atlantis", "finish": "New York, NY"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Could not build the route", response.content.decode())

    def test_health_endpoint(self):
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
