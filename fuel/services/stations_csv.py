"""Shared CSV parsing/deduplication for the fuel station import commands."""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fuel.models import FuelStation

logger = logging.getLogger("fuel.stations_csv")

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO",
    "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
    "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
}


def clean(value: str | None) -> str:
    return " ".join((value or "").split())


def to_int(value: str | None) -> int | None:
    text = clean(value)
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


@dataclass
class CsvLoadResult:
    stations: list[dict] = field(default_factory=list)
    skipped_rows: int = 0
    skipped_non_us: int = 0


def load_unique_stations(
    path: Path,
    *,
    states: str | None = None,
    limit: int | None = None,
    include_non_us: bool = False,
) -> CsvLoadResult:
    """Read and deduplicate the assessment CSV.

    Rows are keyed by ``(truckstop_id, address, city, state)`` (name + address +
    city + state when the id is missing) and the minimum retail price wins.
    """
    state_filter = None
    if states:
        state_filter = {part.strip().upper() for part in states.split(",") if part.strip()}

    unique: dict[tuple, dict] = {}
    result = CsvLoadResult()

    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            name = clean(row.get("Truckstop Name"))
            address = clean(row.get("Address"))
            city = clean(row.get("City"))
            state = clean(row.get("State")).upper()
            truckstop_id = to_int(row.get("OPIS Truckstop ID"))
            rack_id = to_int(row.get("Rack ID"))

            if not name or not address or not city or len(state) != 2:
                result.skipped_rows += 1
                continue
            if state_filter and state not in state_filter:
                continue
            if not include_non_us and state not in US_STATES:
                result.skipped_non_us += 1
                continue
            try:
                price = Decimal(clean(row.get("Retail Price")))
            except (InvalidOperation, TypeError):
                result.skipped_rows += 1
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

    stations = list(unique.values())
    # Group by location so city-level lookups/geocoding are efficient.
    stations.sort(key=lambda s: (s["state"], s["city"], s["name"]))
    if limit is not None:
        stations = stations[:limit]
    result.stations = stations
    return result


def station_lookup(station: dict) -> dict:
    """Database lookup identifying a station row."""
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


def save_station(station: dict, latitude: float, longitude: float, approximate: bool) -> FuelStation:
    obj, _created = FuelStation.objects.update_or_create(
        **station_lookup(station),
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
