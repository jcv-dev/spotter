"""Station geocoding pipeline shared by the import/validation commands.

Highway-exit addresses ("I-44, EXIT 283 & US-69") are unreliable as free-form
geocoding queries: Pelias may match the named highway in a different city or
even a different state. The pipeline therefore:

1. geocodes the **city/state centroid** (reliable, cached per city),
2. tries the **full address** constrained to the state's bounding box,
3. accepts the address result only when it is close to the city centroid,
4. otherwise stores the city centroid and marks the station approximate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings

from . import maps
from .geo import haversine_miles
from .us_states import bounding_box, point_in_state

logger = logging.getLogger("fuel.geocoding")

#: City results may sit slightly outside our approximate state boxes.
_STATE_MARGIN_DEGREES = 0.3


@dataclass(frozen=True)
class StationGeocode:
    latitude: float
    longitude: float
    is_approximate: bool
    label: str


def city_centroid(city: str, state: str) -> tuple[float, float] | None:
    """Geocode a city to a centroid, validated against the state bounds."""
    box = bounding_box(state)
    if box is None:
        return None
    result = maps.geocode(f"{city}, {state}", rect=box)
    if result is None:
        return None
    if not point_in_state(result.latitude, result.longitude, state, margin_degrees=_STATE_MARGIN_DEGREES):
        logger.warning(
            "City centroid for %s, %s resolved outside the state (%.4f, %.4f) – rejected",
            city, state, result.latitude, result.longitude,
        )
        return None
    return result.latitude, result.longitude


def geocode_station(
    address: str,
    city: str,
    state: str,
    *,
    max_distance_miles: float | None = None,
    city_only: bool = False,
) -> StationGeocode | None:
    """Resolve a station to coordinates, or ``None`` when the city is unknown."""
    limit = (
        max_distance_miles
        if max_distance_miles is not None
        else float(settings.GEOCODE_CITY_MAX_DISTANCE_MILES)
    )
    centroid = city_centroid(city, state)
    if centroid is None:
        return None

    if not city_only:
        result = maps.geocode(
            f"{address}, {city}, {state}",
            rect=bounding_box(state),
            focus=centroid,
        )
        if result is not None:
            distance = float(
                haversine_miles(centroid[0], centroid[1], result.latitude, result.longitude)
            )
            if distance <= limit:
                return StationGeocode(result.latitude, result.longitude, False, result.label)
            logger.info(
                "Address match for %r (%s, %s) is %.1f mi from the city centroid – "
                "falling back to the centroid",
                address, city, state, distance,
            )

    return StationGeocode(centroid[0], centroid[1], True, f"{city}, {state}")
