"""Provider dispatcher for routing and geocoding.

``ROUTING_PROVIDER`` / ``GEOCODING_PROVIDER`` accept:

* ``ors`` (default) – OpenRouteService / HeiGIT, the provider required by the
  assessment;
* ``osrm`` / ``nominatim`` – free, keyless OpenStreetMap services intended for
  light/demo usage;
* ``auto`` – try ORS first and transparently fall back to the free provider
  when ORS is unavailable (quota exhausted, missing/rejected key, or repeated
  upstream failures).

The API, map page and optimizer always talk to this module, so switching a
provider (or falling back) changes nothing above the service layer.
"""

from __future__ import annotations

import logging

from django.conf import settings

from . import nominatim, ors, osrm
from .errors import ProviderConfigurationError, ProviderError

logger = logging.getLogger("fuel.maps")

#: Avoid repeating the same fallback warning for every station/request.
_fallback_warned: set[tuple[str, str]] = set()


def _warn_fallback(kind: str, error: Exception) -> None:
    key = (kind, type(error).__name__)
    if key not in _fallback_warned:
        _fallback_warned.add(key)
        logger.warning("ORS %s unavailable (%s) – falling back to the free provider", kind, error)
    else:
        logger.debug("ORS %s unavailable (%s) – falling back", kind, error)


def _normalise(value: str) -> str:
    return (value or "ors").strip().lower()


def get_directions(start: tuple[float, float], finish: tuple[float, float], *, profile: str = "driving-car"):
    """Route between two ``(lat, lon)`` points using the configured provider."""
    provider = _normalise(settings.ROUTING_PROVIDER)
    if provider == "osrm":
        return osrm.get_directions(start, finish, profile=profile)
    if provider in ("ors", "auto"):
        try:
            return ors.get_directions(start, finish, profile=profile)
        except ProviderError as exc:
            if provider == "auto":
                _warn_fallback("routing", exc)
                return osrm.get_directions(start, finish, profile=profile)
            raise
    raise ProviderConfigurationError(
        f"Unknown ROUTING_PROVIDER {provider!r}: expected 'ors', 'osrm' or 'auto'."
    )


def geocode(query: str, **kwargs):
    """Geocode a query using the configured provider."""
    provider = _normalise(settings.GEOCODING_PROVIDER)
    if provider == "nominatim":
        return nominatim.geocode(query, **kwargs)
    if provider in ("ors", "auto"):
        try:
            return ors.geocode(query, **kwargs)
        except ProviderError as exc:
            if provider == "auto":
                _warn_fallback("geocoding", exc)
                return nominatim.geocode(query, **kwargs)
            raise
    raise ProviderConfigurationError(
        f"Unknown GEOCODING_PROVIDER {provider!r}: expected 'ors', 'nominatim' or 'auto'."
    )
