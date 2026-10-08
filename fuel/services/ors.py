"""Thin client around the OpenRouteService (ORS) HTTP API.

Two endpoints are used:

* ``/pelias/v1/search`` – address -> coordinates (geocoding).
* ``/openrouteservice/v2/directions/{profile}/geojson`` – coordinates -> route geometry.

Both are cached through Django's cache framework to keep external calls to a
minimum (geocoding for 24h by default, routes for 1h).
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger("fuel.ors")

METERS_PER_MILE = 1609.344
_MISSING = object()


class ORSError(Exception):
    """Base class for ORS related failures."""


class ORSConfigurationError(ORSError):
    """The API key is missing or rejected by ORS."""


class ORSQuotaError(ORSError):
    """The daily ORS quota has been exhausted."""


class ORSRequestError(ORSError):
    """ORS could not be reached or returned an unexpected response."""


@dataclass(frozen=True)
class GeocodeResult:
    latitude: float
    longitude: float
    label: str
    confidence: float | None = None


@dataclass(frozen=True)
class DirectionsResult:
    #: Route polyline as ``[(lat, lon), ...]``.
    coordinates: list[tuple[float, float]]
    distance_miles: float
    duration_seconds: float
    profile: str = "driving-car"


def _base_url() -> str:
    return settings.ORS_BASE_URL.rstrip("/")


def get_api_key() -> str:
    key = (settings.OPENROUTESERVICE_API_KEY or "").strip()
    if not key:
        raise ORSConfigurationError(
            "OPENROUTESERVICE_API_KEY is not configured. Set it in the environment "
            "or in the project's .env file."
        )
    return key


def _cache_key(kind: str, *parts: str) -> str:
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return f"ors:{kind}:{digest}"


_last_request_at = 0.0


def _throttle() -> None:
    """Keep at least ``ORS_REQUEST_INTERVAL`` seconds between real HTTP calls."""
    global _last_request_at
    interval = float(getattr(settings, "ORS_REQUEST_INTERVAL", 0.0) or 0.0)
    if interval > 0:
        wait = _last_request_at + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
    _last_request_at = time.monotonic()


def _request(
    method: str,
    url: str,
    *,
    params: dict | None = None,
    json_body: dict | None = None,
    headers: dict | None = None,
    retries: int = 3,
) -> dict:
    """Perform an ORS request with retries on transient failures."""
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        if attempt:
            backoff = min(60.0, 5.0 * (2 ** (attempt - 1)))
            logger.warning("Retrying ORS request in %.0fs (attempt %d/%d)", backoff, attempt + 1, retries + 1)
            time.sleep(backoff)
        _throttle()
        try:
            response = requests.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=settings.ORS_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            last_error = ORSRequestError(f"Could not reach OpenRouteService: {exc}")
            logger.warning("ORS connection error: %s", exc)
            continue

        if response.status_code == 200:
            return response.json()

        body_snippet = response.text[:300]
        if response.status_code == 401:
            raise ORSConfigurationError(
                f"OpenRouteService rejected the API key (HTTP 401): {body_snippet}"
            )
        if response.status_code == 403:
            raise ORSQuotaError(
                "OpenRouteService quota exhausted (HTTP 403). The daily limit has "
                f"been reached – try again after the quota resets. Response: {body_snippet}"
            )
        if response.status_code == 429 or response.status_code >= 500:
            last_error = ORSRequestError(
                f"OpenRouteService returned HTTP {response.status_code}: {body_snippet}"
            )
            logger.warning("ORS transient error HTTP %s", response.status_code)
            continue
        # Any other 4xx is not retryable.
        raise ORSRequestError(
            f"OpenRouteService returned HTTP {response.status_code} for {url}: {body_snippet}"
        )

    raise last_error or ORSRequestError("OpenRouteService request failed")


def geocode(
    query: str,
    *,
    country: str = "US",
    rect: tuple[float, float, float, float] | None = None,
    circle: tuple[float, float, float] | None = None,
    focus: tuple[float, float] | None = None,
) -> GeocodeResult | None:
    """Geocode a free-form address query. Returns ``None`` when nothing matches.

    ``rect`` is ``(min_lat, min_lon, max_lat, max_lon)``, ``circle`` is
    ``(lat, lon, radius_km)`` and ``focus`` is a ``(lat, lon)`` ranking bias.
    Results (including negative results) are cached to avoid repeated calls.
    """
    query = " ".join((query or "").split())
    if not query:
        return None

    key_parts = [country.upper(), query.lower()]
    if rect:
        key_parts.append("rect:" + ",".join(f"{value:.4f}" for value in rect))
    if circle:
        key_parts.append("circle:" + ",".join(f"{value:.4f}" for value in circle))
    if focus:
        key_parts.append("focus:" + ",".join(f"{value:.4f}" for value in focus))
    key = _cache_key("geocode", *key_parts)
    cached = cache.get(key, default=_MISSING)
    if cached is not _MISSING:
        return cached

    params: dict = {
        "api_key": get_api_key(),
        "text": query,
        "size": 1,
        "boundary.country": country.upper(),
        "lang": "en",
    }
    if rect:
        min_lat, min_lon, max_lat, max_lon = rect
        params["boundary.rect.min_lat"] = min_lat
        params["boundary.rect.min_lon"] = min_lon
        params["boundary.rect.max_lat"] = max_lat
        params["boundary.rect.max_lon"] = max_lon
    if circle:
        lat, lon, radius_km = circle
        params["boundary.circle.lat"] = lat
        params["boundary.circle.lon"] = lon
        params["boundary.circle.radius"] = radius_km
    if focus:
        params["focus.point.lat"] = focus[0]
        params["focus.point.lon"] = focus[1]

    logger.info("Geocoding %r", query)
    data = _request("GET", f"{_base_url()}/pelias/v1/search", params=params)

    result: GeocodeResult | None = None
    features = data.get("features") or []
    if features:
        feature = features[0]
        try:
            lon, lat = feature["geometry"]["coordinates"][:2]
        except (KeyError, TypeError, ValueError):
            logger.warning("Malformed geocoding response for %r", query)
        else:
            properties = feature.get("properties") or {}
            result = GeocodeResult(
                latitude=float(lat),
                longitude=float(lon),
                label=str(properties.get("label") or query),
                confidence=properties.get("confidence"),
            )

    cache.set(key, result, settings.GEOCODE_CACHE_TTL)
    return result


def get_directions(
    start: tuple[float, float],
    finish: tuple[float, float],
    *,
    profile: str = "driving-car",
) -> DirectionsResult:
    """Fetch a driving route between two ``(lat, lon)`` points."""
    start_lat, start_lon = start
    finish_lat, finish_lon = finish
    key = _cache_key(
        "route",
        profile,
        f"{start_lat:.6f},{start_lon:.6f}",
        f"{finish_lat:.6f},{finish_lon:.6f}",
    )
    cached = cache.get(key, default=_MISSING)
    if cached is not _MISSING:
        logger.info("Route cache hit (%s -> %s)", key[-12:], profile)
        return cached

    api_key = get_api_key()
    body = {
        "coordinates": [[start_lon, start_lat], [finish_lon, finish_lat]],
        "instructions": False,
    }
    url = f"{_base_url()}/openrouteservice/v2/directions/{profile}/geojson"
    logger.info("Requesting ORS directions %s -> %s", start, finish)
    data = _request("POST", url, json_body=body, headers={"Authorization": api_key})

    try:
        feature = data["features"][0]
        coordinates = [(float(lat), float(lon)) for lon, lat in feature["geometry"]["coordinates"]]
        summary = feature["properties"]["summary"]
        result = DirectionsResult(
            coordinates=coordinates,
            distance_miles=float(summary["distance"]) / METERS_PER_MILE,
            duration_seconds=float(summary.get("duration", 0.0)),
            profile=profile,
        )
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ORSRequestError(f"Unexpected ORS directions response: {exc}") from exc

    cache.set(key, result, settings.ROUTE_CACHE_TTL)
    return result
