"""Tests for the offline city fill and the ORS upgrade commands."""

from __future__ import annotations

import csv
import os
import tempfile
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from fuel.models import FuelStation
from fuel.services.city_lookup import CityLookup
from fuel.services.errors import ProviderQuotaError
from fuel.services.geocoding import StationGeocode

HEADER = ["OPIS Truckstop ID", "Truckstop Name", "Address", "City", "State", "Rack ID", "Retail Price"]

CSV_ROWS = [
    ["20", "PILOT #1243", "I-8, EXIT 119 & SR-85", "Gila Bend", "AZ", "930", "3.100"],
    ["21", "LOVE'S #1", "I-10, EXIT 44", "Loxley", "AL", "12", "3.500"],
    ["22", "MOUNTAIN STOP", "I-40, EXIT 100", "Nowhere", "NM", "13", "3.900"],
]

LOOKUP = CityLookup(
    {
        ("gilabend", "AZ"): (32.95, -112.72),
        ("loxley", "AL"): (30.62, -87.75),
    }
)


def write_csv(rows) -> str:
    handle = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8", newline="")
    writer = csv.writer(handle)
    writer.writerow(HEADER)
    writer.writerows(rows)
    handle.close()
    return handle.name


@override_settings(GEOCODING_PROVIDER="ors", OPENROUTESERVICE_API_KEY="test-key", ORS_REQUEST_INTERVAL=0.0)
class ImportCityCoordinatesTests(TestCase):
    def setUp(self):
        self.path = write_csv(CSV_ROWS)
        self.addCleanup(os.unlink, self.path)

    def run_command(self, **kwargs) -> str:
        output = StringIO()
        with mock.patch("fuel.services.city_lookup.CityLookup.load", return_value=LOOKUP):
            call_command("import_city_coordinates", csv=self.path, stdout=output, **kwargs)
        return output.getvalue()

    def test_fills_missing_stations_as_approximate(self):
        output = self.run_command()
        self.assertEqual(FuelStation.objects.count(), 2)  # "Nowhere" cannot be matched
        station = FuelStation.objects.get(city="Gila Bend")
        self.assertAlmostEqual(station.latitude, 32.95)
        self.assertAlmostEqual(station.longitude, -112.72)
        self.assertTrue(station.is_approximate)
        self.assertIn("2 stations filled", output)
        self.assertIn("1 unmatched", output)
        self.assertIn("Nowhere, NM", output)

    def test_existing_stations_are_skipped(self):
        FuelStation.objects.create(
            name="PILOT #1243", address="I-8, EXIT 119 & SR-85", city="Gila Bend", state="AZ",
            latitude=33.0, longitude=-112.0, price=Decimal("3.100"), truckstop_id=20,
        )
        output = self.run_command()
        self.assertEqual(FuelStation.objects.count(), 2)
        station = FuelStation.objects.get(city="Gila Bend")
        self.assertAlmostEqual(station.latitude, 33.0)  # unchanged
        self.assertIn("1 already present", output)

    def test_dry_run_does_not_write(self):
        output = self.run_command(dry_run=True)
        self.assertEqual(FuelStation.objects.count(), 0)
        self.assertIn("2 stations would be filled", output)


@override_settings(GEOCODING_PROVIDER="ors", OPENROUTESERVICE_API_KEY="test-key", ORS_REQUEST_INTERVAL=0.0)
class UpgradeApproximateStationsTests(TestCase):
    def setUp(self):
        self.path = write_csv(CSV_ROWS)
        self.addCleanup(os.unlink, self.path)
        self.exact = FuelStation.objects.create(
            name="EXACT STOP", address="I-40, EXIT 1", city="Flagstaff", state="AZ",
            latitude=35.19, longitude=-111.65, price=Decimal("3.200"), is_approximate=False,
        )
        self.approximate = FuelStation.objects.create(
            name="PILOT #1243", address="I-8, EXIT 119 & SR-85", city="Gila Bend", state="AZ",
            latitude=32.95, longitude=-112.72, price=Decimal("3.100"), is_approximate=True,
            truckstop_id=20,
        )
        # Exact and present: must not be a target of the upgrade.
        self.nowhere = FuelStation.objects.create(
            name="MOUNTAIN STOP", address="I-40, EXIT 100", city="Nowhere", state="NM",
            latitude=35.0, longitude=-108.0, price=Decimal("3.900"), is_approximate=False,
            truckstop_id=22,
        )

    def run_command(self, **kwargs) -> str:
        output = StringIO()
        call_command("upgrade_approximate_stations", csv=self.path, stdout=output, **kwargs)
        return output.getvalue()

    def test_upgrades_approximate_and_fills_missing(self):
        def fake(address, city, state, **kwargs):
            if city == "Gila Bend":
                return StationGeocode(32.9466, -112.7188, False, "Exit 119, Gila Bend")
            return StationGeocode(30.62, -87.75, True, "Loxley, AL")

        with mock.patch("fuel.services.geocoding.geocode_station", side_effect=fake) as geocode:
            output = self.run_command()

        # Flagstaff is exact and must not be touched.
        self.assertEqual(geocode.call_count, 2)
        self.approximate.refresh_from_db()
        self.assertFalse(self.approximate.is_approximate)
        self.assertAlmostEqual(self.approximate.latitude, 32.9466)
        loxley = FuelStation.objects.get(city="Loxley")
        self.assertTrue(loxley.is_approximate)
        self.assertIn("1 stations upgraded", output)
        self.assertIn("1 missing stations filled", output)

    def test_quota_error_is_reported_as_resumable(self):
        with mock.patch(
            "fuel.services.geocoding.geocode_station", side_effect=ProviderQuotaError("quota exceeded")
        ):
            with self.assertRaises(CommandError) as context:
                self.run_command()
        self.assertIn("quota", str(context.exception).lower())

    def test_dry_run_reports_targets_only(self):
        output = self.run_command(dry_run=True)
        self.assertIn("2 stations to upgrade", output)
        self.approximate.refresh_from_db()
        self.assertTrue(self.approximate.is_approximate)

    @override_settings(GEOCODING_PROVIDER="auto")
    def test_bulk_upgrade_forces_ors_and_warns(self):
        with mock.patch(
            "fuel.services.geocoding.geocode_station",
            return_value=StationGeocode(32.9466, -112.7188, False, "exact"),
        ):
            output = self.run_command(dry_run=False)
        self.assertIn("Bulk upgrades always use ORS", output)
