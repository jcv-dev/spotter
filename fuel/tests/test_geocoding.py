"""Tests for the station geocoding pipeline and the validate_stations command."""

from __future__ import annotations

from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings

from fuel.models import FuelStation
from fuel.services import geocoding
from fuel.services.ors import GeocodeResult
from fuel.services.us_states import bounding_box, point_in_state


@override_settings(ORS_REQUEST_INTERVAL=0.0, GEOCODE_CITY_MAX_DISTANCE_MILES=15.0)
class StationGeocodingTests(TestCase):
    def setUp(self):
        cache.clear()

    def _patch(self, mapping):
        def fake(query, **kwargs):
            return mapping.get(query.lower())

        patch = mock.patch("fuel.services.ors.geocode", side_effect=fake)
        self.addCleanup(patch.stop)
        return patch.start()

    def test_address_result_near_city_is_exact(self):
        mapping = {
            "clanton, al": GeocodeResult(32.84, -86.62, label="Clanton, AL"),
            "i-65, exit 205 & us-31/sr-22, clanton, al": GeocodeResult(
                32.80, -86.65, label="Exit 205, Clanton, AL"
            ),
        }
        self._patch(mapping)
        outcome = geocoding.geocode_station("I-65, EXIT 205 & US-31/SR-22", "Clanton", "AL")
        self.assertIsNotNone(outcome)
        self.assertFalse(outcome.is_approximate)
        self.assertAlmostEqual(outcome.latitude, 32.80)
        self.assertAlmostEqual(outcome.longitude, -86.65)

    def test_far_address_result_falls_back_to_city_centroid(self):
        # Pelias matched a highway segment 136 miles away – must be rejected.
        mapping = {
            "clanton, al": GeocodeResult(32.84, -86.62, label="Clanton, AL"),
            "i-65, exit 205 & us-31/sr-22, clanton, al": GeocodeResult(
                34.80, -86.95, label="Athens, AL"
            ),
        }
        self._patch(mapping)
        outcome = geocoding.geocode_station("I-65, EXIT 205 & US-31/SR-22", "Clanton", "AL")
        self.assertTrue(outcome.is_approximate)
        self.assertAlmostEqual(outcome.latitude, 32.84)
        self.assertAlmostEqual(outcome.longitude, -86.62)

    def test_missing_address_result_uses_city_centroid(self):
        self._patch({"loxley, al": GeocodeResult(30.62, -87.75, label="Loxley, AL")})
        outcome = geocoding.geocode_station("I-10, EXIT 44", "Loxley", "AL")
        self.assertTrue(outcome.is_approximate)
        self.assertAlmostEqual(outcome.latitude, 30.62)

    def test_city_result_outside_state_is_rejected(self):
        # A "Gila Bend, AZ" query that resolves to Chicago must not be used.
        self._patch({"gila bend, az": GeocodeResult(41.85, -87.65, label="Chicago, IL")})
        outcome = geocoding.geocode_station("I-8, EXIT 119", "Gila Bend", "AZ")
        self.assertIsNone(outcome)

    def test_city_only_skips_the_address_attempt(self):
        mapping = {
            "loxley, al": GeocodeResult(30.62, -87.75, label="Loxley, AL"),
            "i-10, exit 44, loxley, al": GeocodeResult(30.60, -87.70, label="near Loxley"),
        }
        patch = mock.patch("fuel.services.ors.geocode", side_effect=lambda q, **kw: mapping.get(q.lower()))
        with patch as geocode:
            outcome = geocoding.geocode_station("I-10, EXIT 44", "Loxley", "AL", city_only=True)
        self.assertEqual(geocode.call_count, 1)
        self.assertTrue(outcome.is_approximate)

    def test_unknown_state_returns_none(self):
        self.assertIsNone(geocoding.geocode_station("HWY 1", "Calgary", "AB"))

    def test_state_helpers(self):
        self.assertEqual(len(bounding_box("IL")), 4)
        self.assertIsNone(bounding_box("XX"))
        self.assertTrue(point_in_state(41.88, -87.63, "IL"))
        self.assertFalse(point_in_state(39.07, -108.50, "IL"))


@override_settings(ORS_REQUEST_INTERVAL=0.0)
class ValidateStationsCommandTests(TestCase):
    def setUp(self):
        # Station whose coordinates are in Colorado but claims to be in Illinois.
        self.bad = FuelStation.objects.create(
            name="SPEEDWAY #7461",
            address="I-294 & SR-50/SR-31",
            city="Alsip",
            state="IL",
            latitude=39.075802,
            longitude=-108.507266,
            price=Decimal("3.219"),
        )
        self.good = FuelStation.objects.create(
            name="QUIKTRIP #7203",
            address="I-57, EXIT 354",
            city="Peru",
            state="IL",
            latitude=41.363656,
            longitude=-89.144366,
            price=Decimal("2.969"),
        )

    def test_report_only_does_not_change_data(self):
        output = StringIO()
        call_command("validate_stations", stdout=output)
        text = output.getvalue()
        self.assertIn("1 look suspicious", text)
        self.assertIn("outside the state bounds", text)
        self.bad.refresh_from_db()
        self.assertAlmostEqual(self.bad.latitude, 39.075802)

    def test_fix_replaces_outlier_with_city_centroid(self):
        outcome = geocoding.StationGeocode(41.669264, -87.736173, True, "Alsip, IL")
        with mock.patch("fuel.services.geocoding.geocode_station", return_value=outcome) as fixed:
            output = StringIO()
            call_command("validate_stations", "--fix", "--city-only", stdout=output)
        fixed.assert_called_once()
        self.bad.refresh_from_db()
        self.assertAlmostEqual(self.bad.latitude, 41.669264)
        self.assertAlmostEqual(self.bad.longitude, -87.736173)
        self.assertTrue(self.bad.is_approximate)
        self.assertIn("1 stations corrected", output.getvalue())

    def test_fix_skips_unresolvable_station(self):
        with mock.patch("fuel.services.geocoding.geocode_station", return_value=None):
            output = StringIO()
            call_command("validate_stations", "--fix", stdout=output)
        self.bad.refresh_from_db()
        self.assertAlmostEqual(self.bad.latitude, 39.075802)
        self.assertIn("0 stations corrected, 1 could not be resolved", output.getvalue())
