"""Routing client for the public OSRM server (no API key required).

Used when ``ROUTING_PROVIDER=osrm`` (or as the ``auto`` fallback when the ORS
quota is exhausted). The public demo server at ``router.project-osrm.org`` is
fine for light/demo usage; production deployments should self-host OSRM.
"""

from __future__ import annotations

import hashlib
import logging
import time

import requests
from django.conf import settings
from django.core.cache import cache

from .errors import ProviderRequestError
from .geo import decode_polyline
from .http import session
from .ors import METERS_PER_MILE, DirectionsResult

logger = logging.getLogger("fuel.osrm")

_MISSING = object()

#: ORS profile names -> OSRM profile names.
_PROFILES = {
    "driving-car": "driving",
    "driving-hgv": "driving",
    "cycling-regular": "bike",
    "foot-walking": "foot",
}


def _cache_key(kind: str, *parts: str) -> str:
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return f"osrm:{kind}:{digest}"


def _request(url: str, params: dict, retries: int = 3) -> dict:
    """GET a JSON payload with retries on transient failures."""
    headers = {"User-Agent": settings.MAPS_USER_AGENT}
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        if attempt:
            backoff = min(30.0, 2.0 * (2 ** (attempt - 1)))
            logger.warning("Retrying OSRM request in %.0fs (attempt %d/%d)", backoff, attempt + 1, retries + 1)
            time.sleep(backoff)
        try:
            response = session.get(
                url, params=params, headers=headers, timeout=settings.ORS_TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            last_error = ProviderRequestError(f"Could not reach OSRM: {exc}")
            logger.warning("OSRM connection error: %s", exc)
            continue

        if response.status_code == 200:
            return response.json()
        if response.status_code == 429 or response.status_code >= 500:
            last_error = ProviderRequestError(
                f"OSRM returned HTTP {response.status_code}: {response.text[:200]}"
            )
            continue
        raise ProviderRequestError(
            f"OSRM returned HTTP {response.status_code}: {response.text[:200]}"
        )
    raise last_error or ProviderRequestError("OSRM request failed")


def get_directions(
    start: tuple[float, float],
    finish: tuple[float, float],
    *,
    profile: str = "driving-car",
) -> DirectionsResult:
    """Fetch a driving route between two ``(lat, lon)`` points via OSRM."""
    start_lat, start_lon = start
    finish_lat, finish_lon = finish
    osrm_profile = _PROFILES.get(profile, "driving")
    key = _cache_key(
        "route",
        osrm_profile,
        f"{start_lat:.6f},{start_lon:.6f}",
        f"{finish_lat:.6f},{finish_lon:.6f}",
    )
    cached = cache.get(key, default=_MISSING)
    if cached is not _MISSING:
        logger.info("OSRM route cache hit (%s)", key[-12:])
        return cached

    base = settings.OSRM_BASE_URL.rstrip("/")
    url = (
        f"{base}/route/v1/{osrm_profile}/"
        f"{start_lon},{start_lat};{finish_lon},{finish_lat}"
    )
    logger.info("Requesting OSRM directions %s -> %s", start, finish)
    # ``polyline`` (precision 5) keeps the response several times smaller than
    # GeoJSON with ~1 m accuracy – the same compact format the ORS client uses.
    data = _request(url, {"overview": "full", "geometries": "polyline", "steps": "false"})

    if data.get("code") != "Ok" or not data.get("routes"):
        raise ProviderRequestError(f"OSRM could not find a route: {data.get('code')}")
    route = data["routes"][0]
    try:
        geometry = route["geometry"]
        if isinstance(geometry, str):
            coordinates = decode_polyline(geometry)
        else:  # GeoJSON geometry, in case a server ignores ``geometries``.
            coordinates = [(float(lat), float(lon)) for lon, lat in geometry["coordinates"]]
        result = DirectionsResult(
            coordinates=coordinates,
            distance_miles=float(route["distance"]) / METERS_PER_MILE,
            duration_seconds=float(route.get("duration", 0.0)),
            profile=profile,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProviderRequestError(f"Unexpected OSRM response: {exc}") from exc

    cache.set(key, result, settings.ROUTE_CACHE_TTL)
    return result
