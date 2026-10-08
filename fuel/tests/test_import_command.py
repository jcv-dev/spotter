"""Tests for the import_fuel_prices management command (ORS mocked)."""

from __future__ import annotations

import csv
import os
import tempfile
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings

from fuel.models import FuelStation
from fuel.services.ors import GeocodeResult

HEADER = ["OPIS Truckstop ID", "Truckstop Name", "Address", "City", "State", "Rack ID", "Retail Price"]


def write_csv(rows) -> str:
    handle = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8", newline="")
    writer = csv.writer(handle)
    writer.writerow(HEADER)
    writer.writerows(rows)
    handle.close()
    return handle.name


def fake_geocode(query, country="US", **kwargs):
    """City queries resolve; the Gila Bend address resolves near its city;
    the Loxley highway address fails (city fallback)."""
    lowered = query.lower()
    if lowered.startswith("gila bend"):
        return GeocodeResult(32.95, -112.72, label=query)
    if lowered.startswith("i-8"):
        return GeocodeResult(32.95, -112.72, label=query)
    if lowered.startswith("loxley"):
        return GeocodeResult(30.62, -87.75, label=query)
    return None


@override_settings(OPENROUTESERVICE_API_KEY="test-key", ORS_REQUEST_INTERVAL=0.0)
class ImportCommandTests(TestCase):
    def setUp(self):
        self.path = write_csv(
            [
                ["20", "PILOT #1243", "I-8, EXIT 119 & SR-85", "Gila Bend", "AZ", "930", "3.899"],
                ["20", "PILOT TRAVEL CENTER #1243", "I-8, EXIT 119 & SR-85", "Gila Bend", "AZ", "930", "3.100"],
                ["21", "LOVE'S #1", "I-10, EXIT 44", "Loxley", "AL", "12", "3.500"],
                ["22", "CANADIAN STOP", "HWY 1", "Calgary", "AB", "13", "4.000"],
            ]
        )
        self.addCleanup(os.unlink, self.path)

    def run_command(self, **kwargs) -> str:
        output = StringIO()
        call_command("import_fuel_prices", csv=self.path, delay=0, stdout=output, **kwargs)
        return output.getvalue()

    def test_dry_run_deduplicates_and_skips_non_us(self):
        output = self.run_command(dry_run=True)
        self.assertIn("2 stations would be imported", output)
        self.assertIn("Skipped 1 rows outside the USA", output)
        self.assertEqual(FuelStation.objects.count(), 0)

    def test_import_dedupes_and_keeps_min_price(self):
        with mock.patch(
            "fuel.management.commands.import_fuel_prices.ors.geocode", side_effect=fake_geocode
        ) as geocode:
            self.run_command()

        self.assertEqual(FuelStation.objects.count(), 2)
        # Two calls per station: city centroid + address attempt.
        self.assertEqual(geocode.call_count, 4)
        station = FuelStation.objects.get(city="Gila Bend")
        self.assertEqual(station.price, Decimal("3.100"))
        self.assertEqual(station.truckstop_id, 20)
        self.assertFalse(station.is_approximate)
        self.assertAlmostEqual(station.latitude, 32.95)
        self.assertAlmostEqual(station.longitude, -112.72)
        self.assertTrue(FuelStation.objects.get(city="Loxley").is_approximate)

    def test_import_is_idempotent_and_resumable(self):
        with mock.patch(
            "fuel.management.commands.import_fuel_prices.ors.geocode", side_effect=fake_geocode
        ):
            self.run_command()
        self.assertEqual(FuelStation.objects.count(), 2)

        # A second run must not call the geocoder again.
        with mock.patch(
            "fuel.management.commands.import_fuel_prices.ors.geocode", side_effect=fake_geocode
        ) as geocode:
            self.run_command()
        self.assertEqual(geocode.call_count, 0)
        self.assertEqual(FuelStation.objects.count(), 2)

    def test_city_fallback_marks_station_approximate(self):
        def fake(query, country="US", **kwargs):
            # The highway-exit address fails; the city query succeeds.
            return None if "exit" in query.lower() else GeocodeResult(30.62, -87.75, label=query)

        with mock.patch(
            "fuel.management.commands.import_fuel_prices.ors.geocode", side_effect=fake
        ) as geocode:
            self.run_command(limit=1)
        # City centroid + failed address attempt.
        self.assertEqual(geocode.call_count, 2)
        station = FuelStation.objects.get()
        self.assertTrue(station.is_approximate)
        self.assertAlmostEqual(station.longitude, -87.75)

    def test_unresolvable_station_is_skipped(self):
        with mock.patch(
            "fuel.management.commands.import_fuel_prices.ors.geocode", return_value=None
        ):
            output = self.run_command()
        self.assertEqual(FuelStation.objects.count(), 0)
        self.assertIn("2 failed", output)

    def test_states_filter(self):
        with mock.patch(
            "fuel.management.commands.import_fuel_prices.ors.geocode", side_effect=fake_geocode
        ):
            self.run_command(states="AL")
        self.assertEqual(FuelStation.objects.count(), 1)
        self.assertEqual(FuelStation.objects.get().state, "AL")

    def test_limit(self):
        with mock.patch(
            "fuel.management.commands.import_fuel_prices.ors.geocode", side_effect=fake_geocode
        ):
            self.run_command(limit=1)
        # Stations are sorted by (state, city, name): AL comes before AZ.
        self.assertEqual(FuelStation.objects.count(), 1)
        self.assertEqual(FuelStation.objects.get().state, "AL")

    def test_missing_csv_raises(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("import_fuel_prices", csv="/nonexistent/file.csv", stdout=StringIO())

    @override_settings(GEOCODING_PROVIDER="nominatim")
    def test_bulk_import_uses_ors_by_default(self):
        with mock.patch(
            "fuel.management.commands.import_fuel_prices.ors.geocode", side_effect=fake_geocode
        ) as geocode:
            output = self.run_command(limit=1)
        self.assertIn("Bulk imports always use ORS", output)
        self.assertTrue(geocode.called)

    @override_settings(GEOCODING_PROVIDER="nominatim")
    def test_bulk_import_can_opt_into_public_nominatim(self):
        with mock.patch(
            "fuel.services.nominatim.geocode",
            return_value=GeocodeResult(30.62, -87.75, label="Loxley, AL"),
        ) as geocode:
            self.run_command(limit=1, allow_public_nominatim=True)
        self.assertTrue(geocode.called)
