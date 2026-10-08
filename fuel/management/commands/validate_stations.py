"""Validate stored station coordinates and optionally fix the outliers.

The CSV addresses are highway-exit descriptions, and free-form geocoding can
occasionally match the named highway in the wrong city or state. This command
checks every stored station against

* the approximate bounding box of its state, and (with ``--full``)
* the distance to the geocoded city centroid,

and with ``--fix`` replaces the coordinates of the offenders using the same
validated pipeline as the importer (city centroid + address attempt).

The command is resumable: corrected stations pass validation on the next run.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from fuel.models import FuelStation
from fuel.services import geocoding, ors
from fuel.services.geo import haversine_miles
from fuel.services.us_states import point_in_state

#: Small tolerance for border towns whose coordinates fall just outside our
#: approximate state boxes.
_MARGIN_DEGREES = 0.2


class Command(BaseCommand):
    help = "Check station coordinates against state bounds / city centroids and fix outliers."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fix", action="store_true", help="Rewrite the coordinates of offending stations."
        )
        parser.add_argument(
            "--full",
            action="store_true",
            help="Also check the distance to the city centroid (one geocoding call per city).",
        )
        parser.add_argument(
            "--city-only",
            action="store_true",
            help="When fixing, use the city centroid directly (no address attempt).",
        )
        parser.add_argument("--states", default=None, help="Comma separated state codes to check.")
        parser.add_argument("--limit", type=int, default=None, help="Check only the first N stations.")
        parser.add_argument(
            "--delay", type=float, default=None, help="Seconds between ORS requests."
        )

    def handle(self, *args, **options):
        if options["delay"] is not None:
            settings.ORS_REQUEST_INTERVAL = options["delay"]

        queryset = FuelStation.objects.all().order_by("state", "city", "name")
        if options["states"]:
            states = {part.strip().upper() for part in options["states"].split(",") if part.strip()}
            queryset = queryset.filter(state__in=states)
        if options["limit"]:
            queryset = queryset[: options["limit"]]

        max_distance = float(settings.GEOCODE_CITY_MAX_DISTANCE_MILES)
        offenders: list[tuple[FuelStation, str]] = []
        checked = 0

        for station in queryset.iterator():
            checked += 1
            if not point_in_state(
                station.latitude, station.longitude, station.state, margin_degrees=_MARGIN_DEGREES
            ):
                offenders.append((station, "outside the state bounds"))
                continue
            if not options["full"]:
                continue
            try:
                centroid = geocoding.city_centroid(station.city, station.state)
            except ors.ORSQuotaError as exc:
                self.stdout.write(self.style.ERROR(str(exc)))
                raise CommandError(
                    "ORS daily quota reached while checking city centroids – re-run later."
                ) from exc
            except ors.ORSError as exc:
                self.stdout.write(self.style.WARNING(f"  {station.name}: {exc}"))
                continue
            if centroid is None:
                offenders.append((station, "city could not be geocoded"))
                continue
            distance = float(
                haversine_miles(station.latitude, station.longitude, centroid[0], centroid[1])
            )
            if distance > max_distance:
                offenders.append((station, f"{distance:.0f} mi from the city centroid"))

        self.stdout.write(f"Checked {checked} stations: {len(offenders)} look suspicious.")
        for station, reason in offenders[:50]:
            self.stdout.write(f"  [{station.state}] {station.city}: {station.name} – {reason}")
        if len(offenders) > 50:
            self.stdout.write(f"  ... and {len(offenders) - 50} more")

        if not options["fix"]:
            if offenders:
                self.stdout.write(self.style.WARNING("Re-run with --fix to correct them."))
            return

        fixed = failed = 0
        for index, (station, _reason) in enumerate(offenders, start=1):
            try:
                outcome = geocoding.geocode_station(
                    station.address, station.city, station.state, city_only=options["city_only"]
                )
            except ors.ORSQuotaError as exc:
                self.stdout.write(self.style.ERROR(str(exc)))
                raise CommandError(
                    "ORS daily quota reached – re-run the command later to continue fixing stations."
                ) from exc
            except ors.ORSError as exc:
                self.stdout.write(self.style.WARNING(f"  {station.name}: {exc}"))
                failed += 1
                continue

            if outcome is None:
                failed += 1
                continue

            station.latitude = outcome.latitude
            station.longitude = outcome.longitude
            station.is_approximate = outcome.is_approximate
            station.save(update_fields=["latitude", "longitude", "is_approximate", "updated_at"])
            fixed += 1
            if index % 50 == 0:
                self.stdout.write(f"  fixed {fixed}/{len(offenders)} ...")

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. {fixed} stations corrected, {failed} could not be resolved."
            )
        )
