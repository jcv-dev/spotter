from django.http import JsonResponse
from django.db import connection


def health(request):
    """Simple liveness/readiness endpoint used by Docker/Dokploy healthchecks."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        database_ok = True
    except Exception:  # pragma: no cover - defensive
        database_ok = False
    return JsonResponse({"status": "ok" if database_ok else "degraded", "database": database_ok})
