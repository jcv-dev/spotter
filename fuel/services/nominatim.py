"""Geocoding client for the public Nominatim service (no API key required).

Used when ``GEOCODING_PROVIDER=nominatim`` (or as the ``auto`` fallback when
the ORS geocoding quota is exhausted). The public server is rate limited to
~1 request/second and should only be used lightly; bulk geocoding belongs on
ORS, a self-hosted Nominatim, or a paid provider.
"""

from __future__ import annotations

import hashlib
import logging
import time

import requests
from django.conf import settings
from django.core.cache import cache

from .errors import ProviderRequestError
from .ors import GeocodeResult

logger = logging.getLogger("fuel.nominatim")

_MISSING = object()
_last_request_at = 0.0


def _cache_key(kind: str, *parts: str) -> str:
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return f"nominatim:{kind}:{digest}"


def _throttle() -> None:
    """Keep at least ``NOMINATIM_REQUEST_INTERVAL`` seconds between requests."""
    global _last_request_at
    interval = float(getattr(settings, "NOMINATIM_REQUEST_INTERVAL", 0.0) or 0.0)
    if interval > 0:
        wait = _last_request_at + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
    _last_request_at = time.monotonic()


def _request(url: str, params: dict, retries: int = 3) -> list:
    """GET a JSON payload with throttling and retries on transient failures."""
    headers = {"User-Agent": settings.MAPS_USER_AGENT}
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        if attempt:
            backoff = min(30.0, 2.0 * (2 ** (attempt - 1)))
            logger.warning("Retrying Nominatim request in %.0fs (attempt %d/%d)", backoff, attempt + 1, retries + 1)
            time.sleep(backoff)
        _throttle()
        try:
            response = requests.get(
                url, params=params, headers=headers, timeout=settings.ORS_TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            last_error = ProviderRequestError(f"Could not reach Nominatim: {exc}")
            logger.warning("Nominatim connection error: %s", exc)
            continue

        if response.status_code == 200:
            return response.json()
        if response.status_code == 429 or response.status_code >= 500:
            last_error = ProviderRequestError(
                f"Nominatim returned HTTP {response.status_code}: {response.text[:200]}"
            )
            continue
        raise ProviderRequestError(
            f"Nominatim returned HTTP {response.status_code}: {response.text[:200]}"
        )
    raise last_error or ProviderRequestError("Nominatim request failed")


def geocode(
    query: str,
    *,
    country: str = "US",
    rect: tuple[float, float, float, float] | None = None,
    circle: tuple[float, float, float] | None = None,
    focus: tuple[float, float] | None = None,
) -> GeocodeResult | None:
    """Geocode a free-form query. Returns ``None`` when nothing matches.

    ``rect`` (``min_lat, min_lon, max_lat, max_lon``) is enforced with
    Nominatim's ``viewbox`` + ``bounded`` parameters; ``circle``/``focus`` are
    accepted for interface compatibility with the ORS client and ignored.
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
        "q": query,
        "format": "jsonv2",
        "countrycodes": country.lower(),
        "limit": 1,
    }
    if rect:
        min_lat, min_lon, max_lat, max_lon = rect
        # Nominatim viewbox: left, top, right, bottom (lon/lat order).
        params["viewbox"] = f"{min_lon},{max_lat},{max_lon},{min_lat}"
        params["bounded"] = 1

    logger.info("Geocoding %r via Nominatim", query)
    data = _request(f"{settings.NOMINATIM_BASE_URL.rstrip('/')}/search", params)

    result: GeocodeResult | None = None
    if isinstance(data, list) and data:
        item = data[0]
        try:
            result = GeocodeResult(
                latitude=float(item["lat"]),
                longitude=float(item["lon"]),
                label=str(item.get("display_name") or query),
                confidence=None,
            )
        except (KeyError, TypeError, ValueError):
            logger.warning("Malformed Nominatim response for %r", query)

    cache.set(key, result, settings.GEOCODE_CACHE_TTL)
    return result
