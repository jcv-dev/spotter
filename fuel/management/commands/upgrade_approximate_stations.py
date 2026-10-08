"""Refine approximate (or still missing) stations through ORS.

Targets:

* stations present in the CSV but missing from the database (e.g. cities the
  offline lookup could not match);
* stations stored with ``is_approximate=true`` (offline city centres or ORS
  city fallbacks).

For every target the validated geocoding pipeline is used: the city centroid
is resolved first, then the full address is attempted and only accepted when
it lands close to that centroid. The command is resumable and quota aware –
when the ORS quota runs out it stops with a clear message and progress is
kept.
"""

from __future__ import annotations

import logging
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from fuel.models import FuelStation
from fuel.services import geocoding, ors
from fuel.services.stations_csv import load_unique_stations, save_station, station_lookup

logger = logging.getLogger("fuel.upgrade")


class Command(BaseCommand):
    help = "Re-geocode approximate or missing stations via ORS (resumable)."

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
            "--delay",
            type=float,
            default=None,
            help="Seconds between ORS requests (defaults to ORS_REQUEST_INTERVAL).",
        )
        parser.add_argument(
            "--allow-public-nominatim",
            action="store_true",
            help=(
                "Allow the public Nominatim service for bulk work (it does not permit "
                "bulk use – prefer ORS or a self-hosted instance)."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Only report how many stations would be upgraded.",
        )

    def handle(self, *args, **options):
        csv_path = Path(options["csv"] or settings.BASE_DIR / "fuel-prices-for-be-assessment.csv")
        if not csv_path.exists():
            raise CommandError(f"CSV file not found: {csv_path}")

        if options["delay"] is not None:
            settings.ORS_REQUEST_INTERVAL = options["delay"]

        loaded = load_unique_stations(
            csv_path,
            states=options["states"],
            limit=options["limit"],
            include_non_us=options["include_non_us"],
        )

        overridden = geocoding.force_bulk_provider(options["allow_public_nominatim"])
        if overridden is not None:
            self.stdout.write(
                self.style.WARNING(
                    f"Bulk upgrades always use ORS (configured provider: {overridden}). "
                    "Pass --allow-public-nominatim to use the public Nominatim service instead."
                )
            )

        targets: list[tuple[dict, FuelStation | None]] = []
        for station in loaded.stations:
            existing = FuelStation.objects.filter(**station_lookup(station)).first()
            if existing is None or existing.is_approximate:
                targets.append((station, existing))

        self.stdout.write(
            f"{len(targets)} stations to upgrade (missing or approximate) out of "
            f"{len(loaded.stations)} unique stations."
        )
        if options["dry_run"] or not targets:
            return

        if not settings.OPENROUTESERVICE_API_KEY:
            raise CommandError(
                "OPENROUTESERVICE_API_KEY is not set – cannot geocode. "
                "Add it to the environment or .env and run again."
            )

        counters = {"upgraded": 0, "still_approximate": 0, "filled": 0, "failed": 0}
        total = len(targets)

        for index, (station, existing) in enumerate(targets, start=1):
            try:
                outcome = geocoding.geocode_station(
                    station["address"], station["city"], station["state"]
                )
            except ors.ORSQuotaError as exc:
                self.stdout.write(self.style.ERROR(str(exc)))
                raise CommandError(
                    "ORS daily quota reached. Progress is saved – re-run the command "
                    "after the quota resets to continue where it stopped."
                ) from exc
            except ors.ORSConfigurationError as exc:
                raise CommandError(str(exc)) from exc
            except ors.ORSError as exc:
                counters["failed"] += 1
                logger.warning("Upgrade failed for %s: %s", station["name"], exc)
                continue

            if outcome is None:
                counters["failed"] += 1
                logger.warning(
                    "Could not resolve %s – %s, %s, %s (left unchanged)",
                    station["name"], station["address"], station["city"], station["state"],
                )
                continue

            save_station(station, outcome.latitude, outcome.longitude, outcome.is_approximate)
            if existing is None:
                counters["filled"] += 1
            elif not outcome.is_approximate:
                counters["upgraded"] += 1
            else:
                counters["still_approximate"] += 1

            if index % 25 == 0 or index == total:
                self.stdout.write(
                    f"  {index}/{total} | upgraded {counters['upgraded']} "
                    f"| still approximate {counters['still_approximate']} "
                    f"| filled {counters['filled']} | failed {counters['failed']}"
                )

        approximate_left = FuelStation.objects.filter(is_approximate=True).count()
        self.stdout.write(
            self.style.SUCCESS(
                f"Done. {counters['upgraded']} stations upgraded to address level, "
                f"{counters['still_approximate']} stayed approximate, "
                f"{counters['filled']} missing stations filled, {counters['failed']} failed. "
                f"{approximate_left} approximate stations remain overall."
            )
        )
