"""Request timing middleware.

Adds the server-side processing time to every response, both as the
``X-Response-Time-Ms`` header and as the standard ``Server-Timing`` header
(visible in browser dev tools). The route API additionally reports the plan
computation time in its JSON body as ``response_time_ms``.
"""

from __future__ import annotations

import time


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
