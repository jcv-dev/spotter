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

import csv
import logging
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from fuel.models import FuelStation
from fuel.services import geocoding, ors

logger = logging.getLogger("fuel.import")

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO",
    "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
    "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
}


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def _to_int(value: str | None) -> int | None:
    text = _clean(value)
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


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

    # ------------------------------------------------------------------ CSV ---
    def _read_stations(
        self,
        path: Path,
        states: str | None,
        limit: int | None,
        include_non_us: bool = False,
    ) -> list[dict]:
        state_filter = None
        if states:
            state_filter = {part.strip().upper() for part in states.split(",") if part.strip()}

        unique: dict[tuple, dict] = {}
        skipped_rows = 0
        skipped_non_us = 0
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                name = _clean(row.get("Truckstop Name"))
                address = _clean(row.get("Address"))
                city = _clean(row.get("City"))
                state = _clean(row.get("State")).upper()
                truckstop_id = _to_int(row.get("OPIS Truckstop ID"))
                rack_id = _to_int(row.get("Rack ID"))

                if not name or not address or not city or len(state) != 2:
                    skipped_rows += 1
                    continue
                if state_filter and state not in state_filter:
                    continue
                if not include_non_us and state not in US_STATES:
                    skipped_non_us += 1
                    continue
                try:
                    price = Decimal(_clean(row.get("Retail Price")))
                except (InvalidOperation, TypeError):
                    skipped_rows += 1
                    continue

                if truckstop_id is not None:
                    key = (truckstop_id, address.lower(), city.lower(), state)
                else:
                    key = (None, name.lower(), address.lower(), city.lower(), state)

                existing = unique.get(key)
                if existing is None:
                    unique[key] = {
                        "name": name,
                        "address": address,
                        "city": city,
                        "state": state,
                        "truckstop_id": truckstop_id,
                        "rack_id": rack_id,
                        "price": price,
                    }
                elif price < existing["price"]:
                    # Conservative: the cheapest price for a duplicated station.
                    existing["price"] = price

        if skipped_rows:
            self.stdout.write(self.style.WARNING(f"Skipped {skipped_rows} malformed rows."))
        if skipped_non_us:
            self.stdout.write(
                self.style.WARNING(
                    f"Skipped {skipped_non_us} rows outside the USA (use --include-non-us to keep them)."
                )
            )

        stations = list(unique.values())
        # Group by location so the city-level geocode cache is used efficiently.
        stations.sort(key=lambda s: (s["state"], s["city"], s["name"]))
        if limit is not None:
            stations = stations[:limit]
        return stations

    # --------------------------------------------------------------- DB IO ---
    def _lookup(self, station: dict) -> dict:
        lookup = {
            "address": station["address"],
            "city": station["city"],
            "state": station["state"],
        }
        if station["truckstop_id"] is not None:
            lookup["truckstop_id"] = station["truckstop_id"]
        else:
            lookup["truckstop_id__isnull"] = True
            lookup["name"] = station["name"]
        return lookup

    def _save(self, station: dict, latitude: float, longitude: float, approximate: bool) -> FuelStation:
        lookup = self._lookup(station)
        obj, _created = FuelStation.objects.update_or_create(
            **lookup,
            defaults={
                "name": station["name"],
                "latitude": latitude,
                "longitude": longitude,
                "price": station["price"],
                "rack_id": station["rack_id"],
                "is_approximate": approximate,
            },
        )
        return obj

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

        stations = self._read_stations(
            csv_path, options["states"], options["limit"], options["include_non_us"]
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
        configured_provider = (settings.GEOCODING_PROVIDER or "ors").lower()
        if not options["allow_public_nominatim"]:
            settings.GEOCODING_PROVIDER = "ors"
            if configured_provider != "ors":
                self.stdout.write(
                    self.style.WARNING(
                        f"Bulk imports always use ORS (configured provider: {configured_provider}). "
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
            lookup = self._lookup(station)
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
                self._save(station, latitude, longitude, approximate)
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
