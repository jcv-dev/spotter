import logging

from django.db import connection
from django.http import JsonResponse
from django.views.generic import TemplateView
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import RouteRequestSerializer
from .services.routing import (
    RouteServiceError,
    build_route_response,
    parse_location_query,
)

logger = logging.getLogger("fuel.views")

DEFAULT_MAP_START = "Los Angeles, CA"
DEFAULT_MAP_FINISH = "New York, NY"


def health(request):
    """Simple liveness/readiness endpoint used by Docker/Dokploy healthchecks."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        database_ok = True
    except Exception:  # pragma: no cover - defensive
        database_ok = False
    return JsonResponse({"status": "ok" if database_ok else "degraded", "database": database_ok})


class RouteAPIView(APIView):
    """``POST /api/route/`` -> route GeoJSON + optimal fuel stops."""

    def post(self, request):
        serializer = RouteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            payload = build_route_response(
                data["start"], data["finish"], data["start_full_tank"]
            )
        except RouteServiceError as exc:
            logger.warning("Route request failed (%s): %s", exc.code, exc)
            return Response({"error": str(exc), "code": exc.code}, status=exc.http_status)

        return Response(payload)


class MapView(TemplateView):
    """``GET /map/`` -> Leaflet map fed by the very same service layer."""

    template_name = "fuel/map.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        start_raw = self.request.GET.get("start") or DEFAULT_MAP_START
        finish_raw = self.request.GET.get("finish") or DEFAULT_MAP_FINISH
        flag = (self.request.GET.get("start_full_tank") or "true").strip().lower()
        start_full_tank = flag not in {"false", "0", "no", "off"}

        payload = None
        error = None
        try:
            payload = build_route_response(
                parse_location_query(start_raw),
                parse_location_query(finish_raw),
                start_full_tank,
            )
        except RouteServiceError as exc:
            logger.warning("Map request failed (%s): %s", exc.code, exc)
            error = {"message": str(exc), "code": exc.code}

        context.update(
            {
                "payload": payload,
                "map_error": error,
                "query_start": start_raw,
                "query_finish": finish_raw,
                "start_full_tank": start_full_tank,
            }
        )
        return context
