"""Service layer: resolve locations, fetch the route and optimise fuel stops.

Both ``POST /api/route/`` and ``GET /map/`` call :func:`build_route_response`
so geocoding/route caches are shared and ORS is never called twice for the
same request.
"""

from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import numpy as np
from django.conf import settings

from fuel.models import FuelStation

from . import maps
from .errors import ProviderConfigurationError, ProviderError
from .geo import MILES_PER_DEGREE_LAT, simplify_polyline
from .optimizer import (
    NoFeasiblePlanError,
    optimize_fuel_stops,
    route_length_miles,
    select_candidates,
)

logger = logging.getLogger("fuel.routing")

_COORD_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


class RouteServiceError(Exception):
    """Base error for the route service, mapped to HTTP responses by the views."""

    http_status = 500
    code = "route_service_error"


class LocationValidationError(RouteServiceError):
    http_status = 400
    code = "invalid_location"


class NoFeasibleFuelPlanError(RouteServiceError):
    http_status = 422
    code = "no_feasible_fuel_plan"


class RouteUnavailableError(RouteServiceError):
    http_status = 502
    code = "route_unavailable"


class ORSNotConfiguredError(RouteServiceError):
    http_status = 503
    code = "ors_not_configured"


@dataclass(frozen=True)
class Location:
    latitude: float
    longitude: float
    label: str


def is_within_usa(latitude: float, longitude: float) -> bool:
    return (
        settings.USA_LAT_MIN <= latitude <= settings.USA_LAT_MAX
        and settings.USA_LON_MIN <= longitude <= settings.USA_LON_MAX
    )


def resolve_location(spec: dict) -> Location:
    """Turn ``{"address": ...}`` or ``{"lat": ..., "lon": ...}`` into coordinates."""
    if not isinstance(spec, dict):
        raise LocationValidationError("Each endpoint must be an object with 'address' or 'lat'/'lon'.")

    address = (spec.get("address") or "").strip()
    if address:
        try:
            result = maps.geocode(address)
        except ProviderConfigurationError as exc:
            raise ORSNotConfiguredError(str(exc)) from exc
        except ProviderError as exc:
            raise RouteUnavailableError(f"Geocoding failed: {exc}") from exc
        if result is None:
            raise LocationValidationError(f"Could not geocode address {address!r}.")
        if not is_within_usa(result.latitude, result.longitude):
            raise LocationValidationError(
                f"{result.label!r} is outside the USA. Only locations within the "
                "continental United States are supported."
            )
        return Location(result.latitude, result.longitude, result.label)

    lat, lon = spec.get("lat"), spec.get("lon")
    if lat is None or lon is None:
        raise LocationValidationError("Provide either 'address' or both 'lat' and 'lon'.")
    try:
        latitude, longitude = float(lat), float(lon)
    except (TypeError, ValueError) as exc:
        raise LocationValidationError("'lat' and 'lon' must be numbers.") from exc
    if not is_within_usa(latitude, longitude):
        raise LocationValidationError(
            f"Coordinates ({latitude}, {longitude}) are outside the USA. Only locations "
            "within the continental United States are supported."
        )
    return Location(latitude, longitude, f"{latitude:.4f}, {longitude:.4f}")


def parse_location_query(value: str) -> dict:
    """Parse a map query-string location: ``"lat,lon"`` or a free-form address."""
    match = _COORD_RE.match(value or "")
    if match:
        return {"lat": float(match.group(1)), "lon": float(match.group(2))}
    return {"address": (value or "").strip()}


def _load_candidates(coords: np.ndarray, radius_miles: float):
    """Query stations in the route's bounding box, then project them exactly."""
    lats = coords[:, 0]
    lons = coords[:, 1]
    lat_pad = radius_miles / MILES_PER_DEGREE_LAT
    cos_lat = max(math.cos(math.radians(max(abs(float(lats.min())), abs(float(lats.max()))))), 0.2)
    lon_pad = radius_miles / (MILES_PER_DEGREE_LAT * cos_lat)

    stations = FuelStation.objects.filter(
        latitude__range=(float(lats.min()) - lat_pad, float(lats.max()) + lat_pad),
        longitude__range=(float(lons.min()) - lon_pad, float(lons.max()) + lon_pad),
    ).only("id", "name", "address", "city", "state", "latitude", "longitude", "price", "is_approximate")

    return select_candidates(coords, stations, radius_miles)


def _format_spec_for_query(spec: dict) -> str:
    address = (spec.get("address") or "").strip()
    if address:
        return address
    return f"{float(spec['lat']):.6f},{float(spec['lon']):.6f}"


def _map_url(start_spec: dict, finish_spec: dict, start_full_tank: bool) -> str:
    query = urlencode(
        {
            "start": _format_spec_for_query(start_spec),
            "finish": _format_spec_for_query(finish_spec),
            "start_full_tank": "true" if start_full_tank else "false",
        }
    )
    return f"/map/?{query}"


def _serialize_stop(stop) -> dict:
    return {
        "name": stop.name,
        "address": stop.address,
        "city": stop.city,
        "state": stop.state,
        "latitude": round(stop.latitude, 6),
        "longitude": round(stop.longitude, 6),
        "price": round(stop.price, 3),
        "gallons_purchased": round(stop.gallons_purchased, 2),
        "cost": round(stop.cost, 2),
        "distance_from_start_miles": round(stop.distance_from_start_miles, 1),
        "detour_miles": round(stop.detour_miles, 1),
        "is_approximate": stop.is_approximate,
        "is_start_fuel": stop.is_start_fuel,
    }


def build_route_response(start_spec: dict, finish_spec: dict, start_full_tank: bool = True) -> dict:
    """Full pipeline: resolve -> route -> candidates -> optimal fuel plan."""
    started = time.perf_counter()
    start = resolve_location(start_spec)
    finish = resolve_location(finish_spec)
    logger.info(
        "Route request %s -> %s (start_full_tank=%s)", start.label, finish.label, start_full_tank
    )

    try:
        directions = maps.get_directions(
            (start.latitude, start.longitude), (finish.latitude, finish.longitude)
        )
    except ProviderConfigurationError as exc:
        raise ORSNotConfiguredError(str(exc)) from exc
    except ProviderError as exc:
        raise RouteUnavailableError(f"Could not compute the route: {exc}") from exc

    # Simplify slightly (<= ~8 m deviation) to keep responses/caches small
    # while staying accurate for station projection and display.
    coords = simplify_polyline(np.asarray(directions.coordinates, dtype=float), tolerance_miles=0.005)
    route_miles = route_length_miles(coords)
    if route_miles <= 0:
        raise RouteUnavailableError("The route is empty; check the start and finish locations.")

    radius = settings.FUEL_STATION_RADIUS_MILES
    candidates = _load_candidates(coords, radius)

    try:
        plan = optimize_fuel_stops(
            route_miles,
            candidates,
            start_full_tank=start_full_tank,
            start_coordinates=(start.latitude, start.longitude),
        )
    except NoFeasiblePlanError as exc:
        raise NoFeasibleFuelPlanError(str(exc)) from exc

    geometry = {
        "type": "LineString",
        "coordinates": [[round(lon, 6), round(lat, 6)] for lat, lon in coords],
    }

    return {
        "route": geometry,
        "total_distance_miles": round(route_miles, 1),
        "route_duration_hours": round(directions.duration_seconds / 3600.0, 2),
        "fuel_stops": [_serialize_stop(stop) for stop in plan.stops],
        "total_fuel_cost": round(plan.total_cost, 2),
        "total_gallons_purchased": round(plan.total_gallons, 2),
        "remaining_fuel_gallons": round(plan.remaining_fuel_gallons, 2),
        "start": {
            "label": start.label,
            "latitude": round(start.latitude, 6),
            "longitude": round(start.longitude, 6),
        },
        "finish": {
            "label": finish.label,
            "latitude": round(finish.latitude, 6),
            "longitude": round(finish.longitude, 6),
        },
        "start_full_tank": bool(start_full_tank),
        "map_url": _map_url(start_spec, finish_spec, start_full_tank),
        "response_time_ms": round((time.perf_counter() - started) * 1000.0, 1),
    }
