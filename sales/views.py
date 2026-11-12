import json

from django_filters import rest_framework as df_filters
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters as drf_filters
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response

from .models import Sale
from .serializers import SaleCorrectSerializer, SaleSerializer, SaleWriteSerializer


class SaleFilter(df_filters.FilterSet):
    shop = df_filters.NumberFilter()
    employee = df_filters.NumberFilter()
    product = df_filters.NumberFilter(field_name="items__variant__product")
    date_from = df_filters.DateFilter(
        field_name="sold_at", lookup_expr="date__gte"
    )
    date_to = df_filters.DateFilter(
        field_name="sold_at", lookup_expr="date__lte"
    )

    class Meta:
        model = Sale
        fields = ["shop", "employee", "product", "status"]


class SaleAccess(BasePermission):
    """Sales matrix (accounts/permissions.py):
    - list/retrieve: any authenticated; sellers scoped to their shops.
    - create: any authenticated; sellers restricted to their own shop
      (validated on the write serializer -> 400).
    - correct: OWNER / MANAGER only.
    - edit/delete: nobody (the viewset is get/post only; corrections
      replace editing).
    """

    message = "You do not have permission for this sales action."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if view.action == "correct":
            return user.role in ("OWNER", "MANAGER")
        return True

    def has_object_permission(self, request, view, obj):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_seller:
            return user.active_shops().filter(pk=obj.shop_id).exists()
        return True


class SaleViewSet(mixins.ListModelMixin,
                  mixins.RetrieveModelMixin,
                  mixins.CreateModelMixin,
                  viewsets.GenericViewSet):
    """Sales: list/retrieve/create + owner/manager correction.

    http_method_names excludes PATCH/PUT/DELETE — sellers cannot edit or
    delete, and managers/owners correct via POST /{id}/correct/ instead.
    """

    serializer_class = SaleSerializer
    permission_classes = [IsAuthenticated, SaleAccess]
    filter_backends = [DjangoFilterBackend, drf_filters.SearchFilter,
                       drf_filters.OrderingFilter]
    filterset_class = SaleFilter
    search_fields = [
        "reference",
        "customer_name",
        "employee__username",
        "items__variant__product__code",
        "items__variant__product__name",
    ]
    ordering_fields = ["sold_at", "reference"]
    ordering = ["-id"]
    pagination_class = None
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        qs = (
            Sale.objects.select_related(
                "shop", "employee", "corrects",
                "created_by", "corrected_by",
            )
            .prefetch_related(
                "items__variant__product",
                "items__unit",
                "attachments",
                "returns",
            )
            .order_by("-id")
            .distinct()
        )
        user = self.request.user
        if user.is_authenticated and user.is_seller:
            qs = qs.filter(
                shop__in=user.active_shops().values_list("pk", flat=True)
            )
        return qs

    def get_serializer_class(self):
        if self.action == "create":
            return SaleWriteSerializer
        if self.action == "correct":
            return SaleCorrectSerializer
        return SaleSerializer

    def _payload(self, request):
        """Accept JSON bodies as-is; parse multipart (slip photo) forms."""
        data = request.data
        if hasattr(data, "getlist"):  # QueryDict from multipart/form-data
            raw_lines = data.get("lines") or "[]"
            if hasattr(raw_lines, "read"):  # sent as a file part
                raw_lines = raw_lines.read()
            if isinstance(raw_lines, (bytes, bytearray)):
                raw_lines = raw_lines.decode("utf-8", errors="replace")
            try:
                lines = (
                    json.loads(raw_lines)
                    if isinstance(raw_lines, str)
                    else raw_lines
                )
            except json.JSONDecodeError:
                raise DRFValidationError(
                    {"detail": ["lines must be valid JSON."]}
                )
            return {
                "shop": data.get("shop"),
                "customer_name": data.get("customer_name") or "",
                "idempotency_key": data.get("idempotency_key") or None,
                "lines": lines,
                "slip": data.get("slip"),
            }
        return data

    def create(self, request, *args, **kwargs):
        serializer = SaleWriteSerializer(
            data=self._payload(request),
            context=self.get_serializer_context(),
        )
        serializer.is_valid(raise_exception=True)
        sale = serializer.save()
        out = SaleSerializer(sale, context=self.get_serializer_context())
        return Response(
            out.data,
            status=(
                status.HTTP_201_CREATED
                if serializer.created
                else status.HTTP_200_OK
            ),
        )

    @action(detail=True, methods=["post"])
    def correct(self, request, pk=None):
        sale = self.get_object()
        serializer = SaleCorrectSerializer(
            data=request.data,
            context={**self.get_serializer_context(), "sale": sale},
        )
        serializer.is_valid(raise_exception=True)
        corrected = serializer.save()
        out = SaleSerializer(
            corrected, context=self.get_serializer_context()
        )
        return Response(out.data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["get"])
    def employees(self, request):
        """Distinct employees present in the visible sales (filter options)."""
        rows = (
            self.get_queryset()
            .values("employee_id", "employee__username",
                    "employee__first_name", "employee__last_name")
            .distinct()
            .order_by("employee__username")
        )
        return Response(
            [
                {
                    "id": r["employee_id"],
                    "username": r["employee__username"],
                    "name": (
                        f"{r['employee__first_name']} "
                        f"{r['employee__last_name']}".strip()
                        or r["employee__username"]
                    ),
                }
                for r in rows
            ]
        )
