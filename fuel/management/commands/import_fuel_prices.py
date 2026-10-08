"""Import fuel stations from the assessment CSV and geocode them via ORS.

The command is idempotent and resumable:

* rows are deduplicated by ``(truckstop_id, address, city, state)`` keeping the
  minimum retail price (for rows without a truckstop id the name is part of
  the key);
* already-geocoded stations are skipped, so re-running the command after a
  quota reset continues where the previous run stopped;
* full address geocoding falls back to ``city, state`` (marked as
  ``is_approximate``) because the CSV addresses are often highway-exit
  descriptions rather than street addresses.
"""

from __future__ import annotations

import logging
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from fuel.models import FuelStation
from fuel.services import geocoding, ors
from fuel.services.stations_csv import load_unique_stations, save_station, station_lookup

logger = logging.getLogger("fuel.import")


class Command(BaseCommand):
    help = "Import fuel stations from fuel-prices-for-be-assessment.csv and geocode them."

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv",
            default=None,
            help="Path to the CSV file (defaults to <project root>/fuel-prices-for-be-assessment.csv).",
        )
        parser.add_argument("--limit", type=int, default=None, help="Import only the first N unique stations.")
        parser.add_argument(
            "--states",
            default=None,
            help="Comma separated state codes to import, e.g. --states CA,AZ,NM,TX",
        )
        parser.add_argument(
            "--delay",
            type=float,
            default=None,
            help="Seconds between ORS requests (defaults to ORS_REQUEST_INTERVAL).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-geocode stations even when coordinates are already stored.",
        )
        parser.add_argument(
            "--include-non-us",
            action="store_true",
            help="Also import stations outside the USA (skipped by default; routes only accept US endpoints).",
        )
        parser.add_argument(
            "--allow-public-nominatim",
            action="store_true",
            help=(
                "Allow bulk geocoding through the public Nominatim service (it does not "
                "permit bulk use – prefer ORS or a self-hosted instance)."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and deduplicate only; do not call ORS or touch the database.",
        )

    # ------------------------------------------------------------- geocode ---
    def _geocode(self, station: dict) -> tuple[float, float, bool] | None:
        outcome = geocoding.geocode_station(station["address"], station["city"], station["state"])
        if outcome is None:
            return None
        return outcome.latitude, outcome.longitude, outcome.is_approximate

    # ---------------------------------------------------------------- main ---
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
        stations = loaded.stations
        if loaded.skipped_rows:
            self.stdout.write(self.style.WARNING(f"Skipped {loaded.skipped_rows} malformed rows."))
        if loaded.skipped_non_us:
            self.stdout.write(
                self.style.WARNING(
                    f"Skipped {loaded.skipped_non_us} rows outside the USA "
                    "(use --include-non-us to keep them)."
                )
            )
        self.stdout.write(
            f"Loaded {len(stations)} unique stations from {csv_path.name} "
            f"(ORS interval: {settings.ORS_REQUEST_INTERVAL}s)."
        )

        if options["dry_run"]:
            for station in stations[:5]:
                self.stdout.write(
                    f"  e.g. [{station['state']}] {station['name']} – {station['address']}, "
                    f"{station['city']} – ${station['price']}"
                )
            self.stdout.write(self.style.SUCCESS(f"Dry run: {len(stations)} stations would be imported."))
            return

        # Bulk imports use ORS (the assessment's geocoder) regardless of the
        # GEOCODING_PROVIDER used by the API: the public Nominatim service does
        # not permit bulk geocoding, so that path requires an explicit opt-in.
        overridden = geocoding.force_bulk_provider(options["allow_public_nominatim"])
        if overridden is not None:
            self.stdout.write(
                self.style.WARNING(
                    f"Bulk imports always use ORS (configured provider: {overridden}). "
                    "Pass --allow-public-nominatim to use the public Nominatim service instead."
                )
            )

        if not settings.OPENROUTESERVICE_API_KEY:
            raise CommandError(
                "OPENROUTESERVICE_API_KEY is not set – cannot geocode. "
                "Add it to the environment or .env and run again."
            )

        counters = {"geocoded": 0, "approximate": 0, "cached": 0, "failed": 0, "updated": 0}
        total = len(stations)

        for index, station in enumerate(stations, start=1):
            lookup = station_lookup(station)
            existing = FuelStation.objects.filter(**lookup).first()

            if existing is not None and not options["force"]:
                # Already geocoded in a previous run: only refresh mutable data.
                if existing.price != station["price"] or existing.name != station["name"]:
                    existing.price = station["price"]
                    existing.name = station["name"]
                    existing.rack_id = station["rack_id"]
                    existing.save(update_fields=["price", "name", "rack_id", "updated_at"])
                    counters["updated"] += 1
                counters["cached"] += 1
            else:
                try:
                    geo = self._geocode(station)
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
                    logger.warning("Geocoding failed for %s: %s", station["name"], exc)
                    continue

                if geo is None:
                    counters["failed"] += 1
                    logger.warning(
                        "Could not geocode %s – %s, %s, %s (skipped)",
                        station["name"], station["address"], station["city"], station["state"],
                    )
                    continue

                latitude, longitude, approximate = geo
                save_station(station, latitude, longitude, approximate)
                counters["geocoded"] += 1
                counters["approximate"] += int(approximate)

            if index % 50 == 0 or index == total:
                self.stdout.write(
                    f"  {index}/{total} | geocoded {counters['geocoded']} "
                    f"(approx {counters['approximate']}) | already stored {counters['cached']} "
                    f"| updated {counters['updated']} | failed {counters['failed']}"
                )

        summary = (
            f"Done. {counters['geocoded']} stations geocoded "
            f"({counters['approximate']} via city fallback), "
            f"{counters['cached']} already present, {counters['updated']} updated, "
            f"{counters['failed']} failed. Database now holds "
            f"{FuelStation.objects.count()} stations."
        )
        self.stdout.write(self.style.SUCCESS(summary))
