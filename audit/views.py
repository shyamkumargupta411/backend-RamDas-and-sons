from django.core.paginator import Paginator
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.viewsets import ViewSet

from accounts.models import User
from accounts.permissions import IsOwnerOrManager
from catalog.models import Product, ProductVariant
from inventory.models import (
    ProductReturn,
    StockAdjustment,
    StockLevel,
    StockTransfer,
)
from sales.models import Sale

ACTION_LABELS = {"+": "Created", "~": "Updated", "-": "Deleted"}

# (history manager accessor, display name)
TRACKED_MODELS = {
    "product": (Product.history, "Product"),
    "productvariant": (ProductVariant.history, "ProductVariant"),
    "shop": ("accounts.Shop", "Shop"),
    "user": ("accounts.User", "User"),
    "stocklevel": (StockLevel.history, "StockLevel"),
    "productreturn": (ProductReturn.history, "ProductReturn"),
    "stockadjustment": (StockAdjustment.history, "StockAdjustment"),
    "stocktransfer": (StockTransfer.history, "StockTransfer"),
    "sale": (Sale.history, "Sale"),
}


def _get_history_manager(key):
    """Resolve a history manager from TRACKED_MODELS, handling string refs."""
    entry = TRACKED_MODELS[key]
    mgr = entry[0]
    if isinstance(mgr, str):
        app_label, model_name = mgr.split(".")
        from django.apps import apps

        model_class = apps.get_model(app_label, model_name)
        mgr = model_class.history
    return mgr, entry[1]


def _changes_for(record):
    """Compute before/after diff for a history record."""
    prev = record.prev_record
    if prev is None:
        return None
    delta = record.diff_against(prev)
    return {
        "added": getattr(delta, "added", {}) or {},
        "removed": getattr(delta, "removed", {}) or {},
        "changed": getattr(delta, "changed", {}) or {},
    }


class AuditLogViewSet(ViewSet):
    """Unified audit log across all history-tracked inventory models.

    Query params:
      model      - filter by model key (product, shop, stocklevel, ...)
      user       - filter by user id
      date_from  - ISO date, inclusive
      date_to    - ISO date, inclusive
      object_id  - filter by tracked object primary key
    """

    permission_classes = [IsOwnerOrManager]
    page_size = 50

    def list(self, request):
        model_filter = request.query_params.get("model")
        user_filter = request.query_params.get("user")
        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")
        object_id = request.query_params.get("object_id")

        results = []

        for key in TRACKED_MODELS:
            history_mgr, model_name = _get_history_manager(key)
            if model_filter and key != model_filter and model_name.lower() != model_filter.lower():
                continue

            qs = history_mgr.all()

            if user_filter:
                qs = qs.filter(history_user_id=user_filter)
            if object_id:
                qs = qs.filter(pk=object_id)
            if date_from:
                qs = qs.filter(history_date__gte=date_from)
            if date_to:
                qs = qs.filter(history_date__lte=date_to)

            qs = qs.select_related("history_user").order_by("-history_date")[:200]

            for record in qs:
                pk_field = history_mgr.model._meta.pk.name
                changes = _changes_for(record)
                results.append({
                    "id": record.history_id,
                    "model": model_name,
                    "object_id": getattr(record, pk_field),
                    "object_repr": str(record),
                    "action_type": record.history_type,
                    "action": ACTION_LABELS.get(
                        record.history_type, record.history_type
                    ),
                    "user": (
                        record.history_user.username
                        if record.history_user
                        else None
                    ),
                    "user_id": record.history_user_id,
                    "timestamp": record.history_date,
                    "changes": changes,
                })

        results.sort(key=lambda r: r["timestamp"], reverse=True)

        paginator = Paginator(results, self.page_size)
        page_number = request.query_params.get("page", 1)
        page = paginator.get_page(page_number)

        return Response({
            "count": paginator.count,
            "total_pages": paginator.num_pages,
            "page": page.number,
            "results": page.object_list,
        })

    @action(detail=False, methods=["get"])
    def models(self, request):
        """List available audit models."""
        return Response([
            {"key": key, "name": name}
            for key, (_, name) in TRACKED_MODELS.items()
        ])

    @action(detail=False, methods=["get"])
    def users(self, request):
        """List users who have audit entries."""
        user_ids = set()
        for key in TRACKED_MODELS:
            history_mgr, _ = _get_history_manager(key)
            user_ids.update(
                history_mgr.exclude(history_user__isnull=True)
                .values_list("history_user_id", flat=True)
            )
        users = User.objects.filter(id__in=user_ids).order_by("username")
        return Response([
            {"id": u.id, "username": u.username, "role": u.role}
            for u in users
        ])