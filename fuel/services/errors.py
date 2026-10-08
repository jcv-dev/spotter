"""Provider-neutral errors raised by routing/geocoding clients.

The ORS client re-exports these under its historical ``ORS*`` names, so both
provider implementations raise the same exception hierarchy and the dispatcher
can fall back between them.
"""

from __future__ import annotations


class ProviderError(Exception):
    """Base class for map provider failures."""


class ProviderConfigurationError(ProviderError):
    """The provider is not configured (e.g. missing or rejected API key)."""


class ProviderQuotaError(ProviderError):
    """The provider's quota is exhausted."""


class ProviderRequestError(ProviderError):
    """The provider could not be reached or returned an unexpected response."""
