"""Fill missing stations with offline city-centre coordinates.

Uses the public Census Gazetteer (and optionally GeoNames) datasets through
:mod:`fuel.services.city_lookup`, so it consumes **no geocoding quota**. The
stations are stored with ``is_approximate=true``; run
``upgrade_approximate_stations`` later (with ORS) to refine them to
address-level coordinates.
"""

from __future__ import annotations

import logging
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from fuel.models import FuelStation
from fuel.services.city_lookup import CityLookup
from fuel.services.stations_csv import load_unique_stations, save_station, station_lookup

logger = logging.getLogger("fuel.city_import")


class Command(BaseCommand):
    help = (
        "Fill stations missing from the database with offline city-centre coordinates "
        "(Census/GeoNames, no geocoding quota). Stations are marked is_approximate."
    )

    def add_arguments(self, parser):
        parser.add_argument("--csv", default=None, help="Path to the assessment CSV.")
        parser.add_argument("--limit", type=int, default=None, help="Only the first N unique stations.")
        parser.add_argument("--states", default=None, help="Comma separated state codes to process.")
        parser.add_argument(
            "--include-non-us",
            action="store_true",
            help="Also process stations outside the USA (skipped by default).",
        )
        parser.add_argument(
            "--cache-dir",
            default=None,
            help="Directory for the downloaded datasets (default: ~/.cache/fuel-route).",
        )
        parser.add_argument(
            "--with-geonames",
            action="store_true",
            help="Also use the GeoNames US dump (adds unincorporated communities; larger download).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be filled without writing to the database.",
        )

    def handle(self, *args, **options):
        csv_path = Path(options["csv"] or settings.BASE_DIR / "fuel-prices-for-be-assessment.csv")
        if not csv_path.exists():
            raise CommandError(f"CSV file not found: {csv_path}")

        loaded = load_unique_stations(
            csv_path,
            states=options["states"],
            limit=options["limit"],
            include_non_us=options["include_non_us"],
        )
        if loaded.skipped_rows:
            self.stdout.write(self.style.WARNING(f"Skipped {loaded.skipped_rows} malformed rows."))
        if loaded.skipped_non_us:
            self.stdout.write(
                self.style.WARNING(
                    f"Skipped {loaded.skipped_non_us} rows outside the USA "
                    "(use --include-non-us to keep them)."
                )
            )

        cache_dir = Path(options["cache_dir"]) if options["cache_dir"] else None
        lookup = CityLookup.load(cache_dir, with_geonames=options["with_geonames"])
        source = "Census + GeoNames" if options["with_geonames"] else "Census"
        self.stdout.write(f"City lookup ready: {len(lookup)} places ({source}).")

        counters = {"filled": 0, "stored": 0, "unmatched": 0}
        unmatched: list[str] = []

        for station in loaded.stations:
            existing = FuelStation.objects.filter(**station_lookup(station)).first()
            if existing is not None:
                counters["stored"] += 1
                continue

            coordinates = lookup.find(station["city"], station["state"])
            if coordinates is None:
                counters["unmatched"] += 1
                if len(unmatched) < 15:
                    unmatched.append(f"{station['city']}, {station['state']}")
                continue

            if not options["dry_run"]:
                save_station(station, coordinates[0], coordinates[1], approximate=True)
            counters["filled"] += 1

        action = "would be filled" if options["dry_run"] else "filled"
        self.stdout.write(
            f"Done. {counters['filled']} stations {action} with city-centre coordinates, "
            f"{counters['stored']} already present, {counters['unmatched']} unmatched. "
            f"Database now holds {FuelStation.objects.count()} stations."
        )
        if unmatched:
            self.stdout.write(
                self.style.WARNING(
                    "Unmatched cities (left for the ORS upgrade): " + "; ".join(unmatched)
                )
            )
        if counters["filled"] and not options["dry_run"]:
            self.stdout.write(
                "Run `python manage.py upgrade_approximate_stations` later to refine them "
                "to address-level coordinates via ORS."
            )
