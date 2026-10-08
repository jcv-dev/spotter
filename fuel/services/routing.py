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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlencode

import numpy as np
from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Max

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


def _has_address(spec: dict) -> bool:
    return isinstance(spec, dict) and bool((spec.get("address") or "").strip())


def _resolve_locations(start_spec: dict, finish_spec: dict) -> tuple[Location, Location]:
    """Resolve both endpoints, geocoding them concurrently when possible.

    Two address endpoints need two independent geocoding round trips; running
    them in parallel saves one full round trip on a cold request. Errors are
    re-raised in the caller's thread with the same types as before.
    """
    if _has_address(start_spec) and _has_address(finish_spec):
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="geocode") as pool:
            start_future = pool.submit(resolve_location, start_spec)
            finish_future = pool.submit(resolve_location, finish_spec)
            return start_future.result(), finish_future.result()
    return resolve_location(start_spec), resolve_location(finish_spec)


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


@dataclass
class _RouteGeometry:
    coordinates: np.ndarray
    route_miles: float
    geojson: dict
    duration_seconds: float


def _plan_cache_key(
    start: Location, finish: Location, start_full_tank: bool, radius_miles: float
) -> str:
    """Key for the fully computed plan (route + candidates + DP result).

    Includes the vehicle/tank settings and the station-data version, so a
    price import or a settings change invalidates cached plans automatically.
    """
    return (
        f"route:plan:{_route_cache_signature(start, finish)}"
        f":{radius_miles:g}:{int(bool(start_full_tank))}:{_stations_version()}"
        f":{float(settings.TANK_CAPACITY_GALLONS):g}"
        f":{float(settings.MILES_PER_GALLON):g}"
        f":{float(settings.FUEL_GRID_STEP_GALLONS):g}"
    )


def _route_cache_signature(start: Location, finish: Location) -> str:
    provider = (settings.ROUTING_PROVIDER or "ors").lower()
    return (
        f"{provider}:{start.latitude:.6f},{start.longitude:.6f}"
        f":{finish.latitude:.6f},{finish.longitude:.6f}"
    )


def _stations_version() -> str:
    """Cheap version of the station table so cached candidates refresh after
    imports/price updates (the version itself is cached for a minute)."""
    version = cache.get("fuel:stations-version")
    if version is None:
        aggregate = FuelStation.objects.aggregate(count=Count("id"), latest=Max("updated_at"))
        latest = aggregate["latest"].timestamp() if aggregate["latest"] else 0.0
        version = f"{aggregate['count']}:{latest:.3f}"
        cache.set("fuel:stations-version", version, 60)
    return version


def _route_geometry(start: Location, finish: Location) -> _RouteGeometry:
    """Simplify the polyline once and cache it (geometry + distance + GeoJSON).

    The provider is only called on a cache miss, so a warm request never has to
    deserialize the (larger) raw directions result just to read its duration.
    """
    key = f"route:geometry:{_route_cache_signature(start, finish)}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    try:
        directions = maps.get_directions(
            (start.latitude, start.longitude), (finish.latitude, finish.longitude)
        )
    except ProviderConfigurationError as exc:
        raise ORSNotConfiguredError(str(exc)) from exc
    except ProviderError as exc:
        raise RouteUnavailableError(f"Could not compute the route: {exc}") from exc

    coordinates = simplify_polyline(
        np.asarray(directions.coordinates, dtype=float),
        tolerance_miles=0.005,
    )
    geometry = {
        "type": "LineString",
        "coordinates": [
            [round(float(lon), 5), round(float(lat), 5)] for lat, lon in coordinates
        ],
    }
    result = _RouteGeometry(
        coordinates=coordinates,
        route_miles=route_length_miles(coordinates),
        geojson=geometry,
        duration_seconds=float(directions.duration_seconds),
    )
    cache.set(key, result, settings.ROUTE_CACHE_TTL)
    return result


def _route_candidates(start: Location, finish: Location, coords: np.ndarray, radius_miles: float):
    """Cache the projected candidate stations for a route."""
    key = (
        f"route:candidates:{_route_cache_signature(start, finish)}"
        f":{radius_miles:g}:{_stations_version()}"
    )
    cached = cache.get(key)
    if cached is not None:
        return cached

    candidates = _load_candidates(coords, radius_miles)
    cache.set(key, candidates, settings.CANDIDATES_CACHE_TTL)
    return candidates


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


def _assemble_response(
    core: dict,
    start: Location,
    finish: Location,
    start_spec: dict,
    finish_spec: dict,
    start_full_tank: bool,
    started: float,
) -> dict:
    """Build the HTTP payload from a cached/computed plan core.

    ``core`` is copied so cached entries are never mutated; the per-request
    fields (labels, map URL, timing) are (re)computed here.
    """
    payload = dict(core)
    payload.update(
        {
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
    )
    return payload


def build_route_response(start_spec: dict, finish_spec: dict, start_full_tank: bool = True) -> dict:
    """Full pipeline: resolve -> route -> candidates -> optimal fuel plan.

    The computed plan is cached as a whole, so a repeated request only resolves
    the endpoints (cached) and assembles the payload.
    """
    started = time.perf_counter()
    start, finish = _resolve_locations(start_spec, finish_spec)
    logger.info(
        "Route request %s -> %s (start_full_tank=%s)", start.label, finish.label, start_full_tank
    )

    radius = float(settings.FUEL_STATION_RADIUS_MILES)
    plan_key = _plan_cache_key(start, finish, start_full_tank, radius)
    cached = cache.get(plan_key)
    if cached is not None:
        return _assemble_response(
            cached, start, finish, start_spec, finish_spec, start_full_tank, started
        )

    # Simplify slightly (<= ~8 m deviation) to keep responses/caches small
    # while staying accurate for station projection and display. Geometry and
    # candidates are cached separately from the plan so a plan-cache miss
    # (e.g. after a price import) does not re-hit the provider.
    geometry_info = _route_geometry(start, finish)
    coords = geometry_info.coordinates
    route_miles = geometry_info.route_miles
    if route_miles <= 0:
        raise RouteUnavailableError("The route is empty; check the start and finish locations.")

    candidates = _route_candidates(start, finish, coords, radius)

    try:
        plan = optimize_fuel_stops(
            route_miles,
            candidates,
            start_full_tank=start_full_tank,
            start_coordinates=(start.latitude, start.longitude),
        )
    except NoFeasiblePlanError as exc:
        raise NoFeasibleFuelPlanError(str(exc)) from exc

    core = {
        "route": geometry_info.geojson,
        "total_distance_miles": round(route_miles, 1),
        "route_duration_hours": round(geometry_info.duration_seconds / 3600.0, 2),
        "fuel_stops": [_serialize_stop(stop) for stop in plan.stops],
        "total_fuel_cost": round(plan.total_cost, 2),
        "total_gallons_purchased": round(plan.total_gallons, 2),
        "remaining_fuel_gallons": round(plan.remaining_fuel_gallons, 2),
    }
    cache.set(plan_key, core, settings.PLAN_CACHE_TTL)
    return _assemble_response(
        core, start, finish, start_spec, finish_spec, start_full_tank, started
    )
