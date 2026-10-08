"""Tests for the offline city-coordinate lookup (no network)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from fuel.services import city_lookup
from fuel.services.city_lookup import CityLookup, normalize_city

GAZETTEER_SAMPLE = (
    "USPS\tGEOID\tANSICODE\tNAME\tLSAD\tFUNCSTAT\tALAND\tAWATER\tALAND_SQMI\tAWATER_SQMI\tINTPTLAT\tINTPTLONG\n"
    "AL\t0100124\t02403054\tAbbeville city\t25\tA\t40255361\t107642\t15.543\t0.042\t31.564706\t-85.259121\n"
    "AL\t0100124\t02403055\tAbbeville CDP\t57\tS\t1000\t0\t0.1\t0.0\t31.600000\t-85.300000\n"
    "NJ\t3400001\t00000000\tMahwah township\t44\tA\t66000000\t1000000\t25.0\t0.4\t41.088707\t-74.143772\n"
    "MO\t2900001\t00000000\tSt. Louis city\t25\tA\t160000000\t2000000\t61.0\t0.9\t38.635297\t-90.245103\n"
    "CO\t0800001\t00000000\tCañon City city\t25\tA\t32000000\t200000\t12.0\t0.1\t38.441032\t-105.242623\n"
)

GEONAMES_SAMPLE = (
    "1\tWillow Beach\tWillow Beach\t\t36.0839\t-114.6969\tP\tPPL\tUS\t\tAZ\t\t\t\t0\t\t\t\t2024-01-01\n"
    "2\tS Coffeyville\tS Coffeyville\t\t36.9900\t-95.6200\tP\tPPL\tUS\t\tOK\t\t\t\t0\t\t\t\t2024-01-01\n"
    "3\tSouth Coffeyville\tSouth Coffeyville\t\t36.9901\t-95.6201\tP\tPPL\tUS\t\tOK\t\t\t\t1500\t\t\t\t2024-01-01\n"
)


class NormalizeCityTests(SimpleTestCase):
    def test_abbreviations_and_suffixes(self):
        self.assertEqual(normalize_city("St. Louis"), normalize_city("Saint Louis"))
        self.assertEqual(normalize_city("St. Louis city"), "stlouis")
        self.assertEqual(normalize_city("Sault Sainte Marie"), normalize_city("Sault Ste. Marie city"))
        self.assertEqual(normalize_city("S Coffeyville"), normalize_city("South Coffeyville"))
        self.assertEqual(normalize_city("Mc Calla"), normalize_city("McCalla CDP"))

    def test_diacritics_and_suffixes(self):
        self.assertEqual(normalize_city("Cañon City"), "canon")
        self.assertEqual(normalize_city("Mahwah township"), "mahwah")
        self.assertEqual(normalize_city("Loxley town"), "loxley")


class ParseTests(SimpleTestCase):
    def _write(self, content: str) -> str:
        handle = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
        handle.write(content)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_read_gazetteer_prefers_larger_place(self):
        path = Path(self._write(GAZETTEER_SAMPLE))
        result = city_lookup._read_gazetteer(path)
        self.assertIn(("abbeville", "AL"), result)
        # The city (larger ALAND) wins over the CDP.
        self.assertAlmostEqual(result[("abbeville", "AL")][1], 31.564706)
        self.assertIn(("mahwah", "NJ"), result)
        self.assertIn(("stlouis", "MO"), result)
        self.assertIn(("canon", "CO"), result)

    def test_read_geonames_keeps_populated_places(self):
        path = Path(self._write(GEONAMES_SAMPLE))
        result = city_lookup._read_geonames(path)
        self.assertIn(("willowbeach", "AZ"), result)
        # The larger population wins for duplicate normalized names.
        self.assertAlmostEqual(result[("southcoffeyville", "OK")][1], 36.9901)

    def test_ensure_dataset_uses_existing_file(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            cache = Path(cache_dir)
            existing = cache / city_lookup._DATASETS["place"][1]
            existing.write_text("cached", encoding="utf-8")
            with mock.patch("fuel.services.city_lookup._download") as download:
                path = city_lookup.ensure_dataset(cache, "place")
        self.assertEqual(path, existing)
        download.assert_not_called()


class CityLookupTests(SimpleTestCase):
    def test_find_normalizes_input(self):
        lookup = CityLookup({("gilabend", "AZ"): (32.95, -112.72), ("stlouis", "MO"): (38.63, -90.24)})
        self.assertEqual(lookup.find("Gila Bend", "az"), (32.95, -112.72))
        self.assertEqual(lookup.find("Saint Louis", "MO"), (38.63, -90.24))
        self.assertIsNone(lookup.find("Nowhere", "AZ"))
