from django.urls import path

from .views import AuditLogViewSet

urlpatterns = [
    path("audit/", AuditLogViewSet.as_view({"get": "list"}), name="audit-list"),
    path(
        "audit/models/",
        AuditLogViewSet.as_view({"get": "models"}),
        name="audit-models",
    ),
    path(
        "audit/users/",
        AuditLogViewSet.as_view({"get": "users"}),
        name="audit-users",
    ),
]