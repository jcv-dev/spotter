"""Request timing and rate limiting middleware.

* :class:`ResponseTimeMiddleware` adds the server-side processing time to every
  response (``X-Response-Time-Ms`` and the standard ``Server-Timing`` header).
* :class:`RateLimitMiddleware` enforces ``RATE_LIMIT_REQUESTS_PER_MINUTE``
  (default 60) per client IP on the ``/api/`` and ``/map/`` endpoints, which
  are the ones that can trigger external map/geocoding calls. ``/health/``
  and the admin are exempt.
"""

from __future__ import annotations

import time

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse


class ResponseTimeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started = time.perf_counter()
        response = self.get_response(request)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        response["X-Response-Time-Ms"] = f"{elapsed_ms:.1f}"
        response["Server-Timing"] = f"app;dur={elapsed_ms:.1f}"
        return response


class RateLimitMiddleware:
    """Fixed-window per-IP rate limit using Django's cache framework."""

    _LIMITED_PREFIXES = ("/api/", "/map/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        limit = int(getattr(settings, "RATE_LIMIT_REQUESTS_PER_MINUTE", 0) or 0)
        if limit <= 0 or not request.path.startswith(self._LIMITED_PREFIXES):
            return self.get_response(request)

        window = int(time.time() // 60)
        key = f"ratelimit:{self._client_ip(request)}:{window}"
        try:
            count = cache.incr(key)
        except ValueError:
            cache.add(key, 1, timeout=120)
            count = 1

        reset_at = (window + 1) * 60
        remaining = max(limit - count, 0)

        if count > limit:
            retry_after = max(reset_at - int(time.time()), 1)
            response = JsonResponse(
                {
                    "error": f"Rate limit exceeded: {limit} requests per minute.",
                    "code": "rate_limited",
                    "retry_after_seconds": retry_after,
                },
                status=429,
            )
            response["Retry-After"] = str(retry_after)
        else:
            response = self.get_response(request)

        response["X-RateLimit-Limit"] = str(limit)
        response["X-RateLimit-Remaining"] = str(remaining)
        response["X-RateLimit-Reset"] = str(reset_at)
        return response

    @staticmethod
    def _client_ip(request) -> str:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.META.get("REMOTE_ADDR", "unknown")
