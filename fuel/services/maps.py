"""Provider dispatcher for routing and geocoding.

``ROUTING_PROVIDER`` / ``GEOCODING_PROVIDER`` accept:

* ``ors`` (default) – OpenRouteService / HeiGIT, the provider required by the
  assessment;
* ``osrm`` / ``nominatim`` – free, keyless OpenStreetMap services intended for
  light/demo usage;
* ``auto`` – try ORS first and transparently fall back to the free provider
  when ORS is unavailable (quota exhausted, missing/rejected key, or repeated
  upstream failures). The fallback is **sticky** for a cooldown window
  (``MAPS_FALLBACK_COOLDOWN_SECONDS``), so a spent quota or a rejected key
  does not make every request pay for a live failing primary call before
  using the fallback. After the cooldown expires ORS is probed again, so the
  deployment recovers on its own when the quota resets.

The API, map page and optimizer always talk to this module, so switching a
provider (or falling back) changes nothing above the service layer.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.cache import cache

from . import nominatim, ors, osrm
from .errors import ProviderConfigurationError, ProviderError, ProviderQuotaError

logger = logging.getLogger("fuel.maps")

#: Avoid repeating the same fallback warning for every station/request.
_fallback_warned: set[tuple[str, str]] = set()

#: Transient failures back off for at most this long, even when the configured
#: cooldown is longer; quota/configuration failures use the full cooldown.
_TRANSIENT_COOLDOWN_CAP_SECONDS = 60.0


def _down_key(provider: str, kind: str) -> str:
    return f"maps:provider-down:{provider}:{kind}"


def _is_down(provider: str, kind: str) -> bool:
    return cache.get(_down_key(provider, kind)) is not None


def _mark_down(provider: str, kind: str, error: Exception) -> None:
    """Remember that ``provider`` failed for ``kind`` for a cooldown window."""
    cooldown = float(getattr(settings, "MAPS_FALLBACK_COOLDOWN_SECONDS", 600.0) or 0.0)
    if not isinstance(error, (ProviderQuotaError, ProviderConfigurationError)):
        cooldown = min(cooldown, _TRANSIENT_COOLDOWN_CAP_SECONDS)
    if cooldown > 0:
        cache.set(_down_key(provider, kind), type(error).__name__, cooldown)


def _clear_down(provider: str, kind: str) -> None:
    cache.delete(_down_key(provider, kind))


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
        if provider == "auto" and _is_down("ors", "routing"):
            logger.debug("ORS routing is in fallback cooldown – using OSRM")
            return osrm.get_directions(start, finish, profile=profile)
        try:
            result = ors.get_directions(start, finish, profile=profile)
        except ProviderError as exc:
            if provider == "auto":
                _mark_down("ors", "routing", exc)
                _warn_fallback("routing", exc)
                return osrm.get_directions(start, finish, profile=profile)
            raise
        if provider == "auto":
            _clear_down("ors", "routing")
        return result
    raise ProviderConfigurationError(
        f"Unknown ROUTING_PROVIDER {provider!r}: expected 'ors', 'osrm' or 'auto'."
    )


def geocode(query: str, **kwargs):
    """Geocode a query using the configured provider."""
    provider = _normalise(settings.GEOCODING_PROVIDER)
    if provider == "nominatim":
        return nominatim.geocode(query, **kwargs)
    if provider in ("ors", "auto"):
        if provider == "auto" and _is_down("ors", "geocoding"):
            logger.debug("ORS geocoding is in fallback cooldown – using Nominatim")
            return nominatim.geocode(query, **kwargs)
        try:
            result = ors.geocode(query, **kwargs)
        except ProviderError as exc:
            if provider == "auto":
                _mark_down("ors", "geocoding", exc)
                _warn_fallback("geocoding", exc)
                return nominatim.geocode(query, **kwargs)
            raise
        if provider == "auto":
            _clear_down("ors", "geocoding")
        return result
    raise ProviderConfigurationError(
        f"Unknown GEOCODING_PROVIDER {provider!r}: expected 'ors', 'nominatim' or 'auto'."
    )
