from django.db import connection
from django.db.utils import DatabaseError
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework import status

try:
    from decouple import config

    APP_VERSION = config("APP_VERSION", default="1.0.0")
except Exception:  # pragma: no cover - decouple is always installed
    APP_VERSION = "1.0.0"


@never_cache
@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    """Liveness + database readiness probe used by Vercel and uptime checks."""
    payload = {
        "status": "ok",
        "service": "metalinv-api",
        "version": APP_VERSION,
        "time": timezone.localtime().isoformat(),
    }
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        payload["database"] = "ok"
        return Response(payload, status=status.HTTP_200_OK)
    except DatabaseError:
        payload["status"] = "degraded"
        payload["database"] = "unavailable"
        return Response(payload, status=status.HTTP_503_SERVICE_UNAVAILABLE)


@never_cache
def health_plain(request):
    """Tiny dependency-free probe for systemd/Nginx (no DRF auth stack)."""
    return JsonResponse({"status": "ok", "service": "metalinv-api"})
