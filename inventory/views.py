from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import SAFE_METHODS, BasePermission, IsAuthenticated
from rest_framework.response import Response

from accounts.permissions import IsOwnerOrManager

from .models import ProductReturn, StockAdjustment, StockLevel, StockReceipt, StockTransfer
from .serializers import (
    ProductReturnSerializer,
    ProductReturnWriteSerializer,
    StockAdjustmentSerializer,
    StockAdjustmentWriteSerializer,
    StockLevelSerializer,
    StockReceiptSerializer,
    StockReceiptWriteSerializer,
    StockTransferReceiveSerializer,
    StockTransferSerializer,
    StockTransferWriteSerializer,
)
from .services import (
    accept_return,
    cancel_transfer,
    dispatch_transfer,
    reject_return,
)


class StockLevelViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin,
                        viewsets.GenericViewSet):
    """Read current stock per location.

    Owner/manager see everything; sellers read only their assigned shops
    (Stage 6 "My Shop Stock").
    """

    serializer_class = StockLevelSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ["shop", "variant", "variant__product"]
    search_fields = [
        "variant__product__code",
        "variant__product__name",
        "variant__spec_key",
        "shop__name",
    ]
    pagination_class = None

    def get_queryset(self):
        qs = (
            StockLevel.objects.select_related(
                "shop",
                "variant",
                "variant__product",
                "variant__unit",
            )
            .filter(quantity__gt=0)
            .order_by("shop__name", "variant__product__code", "variant__id")
        )
        user = self.request.user
        if user.is_authenticated and user.is_seller:
            qs = qs.filter(
                shop__in=user.active_shops().values_list("pk", flat=True)
            )
        return qs


class StockReceiptViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin,
                          viewsets.GenericViewSet):
    """Stock-in receipts: list/retrieve all; create applies stock atomically."""

    permission_classes = [IsAuthenticated, IsOwnerOrManager]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ["shop"]
    search_fields = ["reference", "supplier", "note"]
    pagination_class = None

    def get_queryset(self):
        return (
            StockReceipt.objects.select_related(
                "shop", "created_by"
            )
            .prefetch_related(
                "lines__variant__product",
                "lines__variant__unit",
            )
            .order_by("-id")
        )

    def get_serializer_class(self):
        if self.action == "create":
            return StockReceiptWriteSerializer
        return StockReceiptSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt = serializer.save()
        read_serializer = StockReceiptSerializer(
            receipt, context=self.get_serializer_context()
        )
        return Response(read_serializer.data, status=status.HTTP_201_CREATED)


class TransferAccess(BasePermission):
    """Transfers matrix:
    - list/retrieve: any authenticated (sellers scoped to incoming).
    - create/dispatch/cancel: owner or manager.
    - receive: owner/manager, or seller assigned to the destination.
    """

    message = "You do not have permission for this transfer action."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if view.action in ("create", "do_dispatch", "cancel"):
            return user.role in ("OWNER", "MANAGER")
        return True  # list/retrieve/receive (receive checked at object level)

    def has_object_permission(self, request, view, obj):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if view.action == "receive":
            if user.role in ("OWNER", "MANAGER"):
                return True
            if user.role == "SELLER":
                return user.active_shops().filter(pk=obj.destination_id).exists()
            return False
        # retrieve: sellers only see incoming transfers
        if user.role == "SELLER":
            return user.active_shops().filter(pk=obj.destination_id).exists()
        return True


class StockTransferViewSet(viewsets.ModelViewSet):
    """Transfer lifecycle: create (draft) -> dispatch -> receive / cancel."""

    serializer_class = StockTransferSerializer
    permission_classes = [IsAuthenticated, TransferAccess]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ["status", "source", "destination"]
    search_fields = ["reference", "note", "source__name", "destination__name"]
    pagination_class = None
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        qs = (
            StockTransfer.objects.select_related(
                "source", "destination",
                "created_by", "dispatched_by", "received_by", "cancelled_by",
            )
            .prefetch_related(
                "items__variant__product",
                "items__variant__unit",
            )
            .order_by("-id")
        )
        user = self.request.user
        if user.is_authenticated and user.is_seller:
            # Shop staff: incoming transfers for their assigned shops only.
            qs = qs.filter(
                destination__in=user.active_shops().values_list("pk", flat=True)
            )
        return qs

    def get_serializer_class(self):
        if self.action == "create":
            return StockTransferWriteSerializer
        return StockTransferSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        transfer = serializer.save()
        return Response(
            StockTransferSerializer(
                transfer, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_201_CREATED,
        )

    def _mutate(self, request, fn):
        transfer = self.get_object()
        try:
            fn(transfer, user=request.user)
        except DjangoValidationError as exc:
            return Response(
                {"detail": exc.messages}, status=status.HTTP_400_BAD_REQUEST
            )
        return Response(
            StockTransferSerializer(
                transfer, context=self.get_serializer_context()
            ).data
        )

    @action(detail=True, methods=["post"], url_path="dispatch")
    def do_dispatch(self, request, pk=None):
        # Method must NOT be named `dispatch` — it would shadow DRF's
        # APIView.dispatch and break every request to this viewset.
        return self._mutate(request, dispatch_transfer)

    @action(detail=True, methods=["post"])
    def receive(self, request, pk=None):
        transfer = self.get_object()
        serializer = StockTransferReceiveSerializer(
            data=request.data, context={"transfer": transfer, "request": request}
        )
        serializer.is_valid(raise_exception=True)
        try:
            serializer.save()
        except DjangoValidationError as exc:
            return Response(
                {"detail": exc.messages}, status=status.HTTP_400_BAD_REQUEST
            )
        # Re-fetch so serialized items reflect updated received quantities.
        transfer = self.get_queryset().get(pk=transfer.pk)
        return Response(
            StockTransferSerializer(
                transfer, context=self.get_serializer_context()
            ).data
        )

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        return self._mutate(request, cancel_transfer)

    @action(detail=False, methods=["get"])
    def in_transit(self, request):
        """In-transit report: dispatched transfers with pending quantities."""
        qs = self.get_queryset().filter(
            status=StockTransfer.Status.DISPATCHED
        )
        serializer = StockTransferSerializer(
            qs, many=True, context=self.get_serializer_context()
        )
        return Response(serializer.data)


# ---------------------------------------------------------------------------
# Returns (Stage 7): PENDING -> ACCEPTED (restock) / REJECTED (no change)
# ---------------------------------------------------------------------------


class ProductReturnViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin,
                           mixins.CreateModelMixin, viewsets.GenericViewSet):
    """Customer returns: create PENDING, accept/reject decide stock.

    Owner/manager only (the Stage 7 pages). No PUT/PATCH/DELETE -
    returns are decided, never edited.
    """

    permission_classes = [IsAuthenticated, IsOwnerOrManager]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter,
                       filters.OrderingFilter]
    filterset_fields = ["shop", "status", "sale", "variant"]
    search_fields = ["reference", "customer_name", "reason"]
    ordering_fields = ["created_at", "decided_at", "reference"]
    ordering = ["-id"]
    pagination_class = None
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        return (
            ProductReturn.objects.select_related(
                "shop", "variant", "variant__product", "variant__unit",
                "sale", "created_by", "decided_by",
            )
            .order_by("-id")
        )

    def get_serializer_class(self):
        if self.action == "create":
            return ProductReturnWriteSerializer
        return ProductReturnSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ret = serializer.save()
        return Response(
            ProductReturnSerializer(
                ret, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_201_CREATED,
        )

    def _decide(self, request, fn, **kwargs):
        ret = self.get_object()
        try:
            fn(ret, user=request.user, **kwargs)
        except DjangoValidationError as exc:
            return Response(
                {"detail": exc.messages}, status=status.HTTP_400_BAD_REQUEST
            )
        return Response(
            ProductReturnSerializer(
                ret, context=self.get_serializer_context()
            ).data
        )

    @action(detail=True, methods=["post"])
    def accept(self, request, pk=None):
        """PENDING -> ACCEPTED: stock increases (RETURN_IN ledger)."""
        return self._decide(request, accept_return)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        """PENDING -> REJECTED: no stock change; optional note."""
        note = (request.data.get("note") or "").strip()
        return self._decide(request, reject_return, note=note)


# ---------------------------------------------------------------------------
# Stock adjustments (Stage 7): immutable, reason required, ledger row
# ---------------------------------------------------------------------------


class StockAdjustmentViewSet(mixins.ListModelMixin,
                             mixins.RetrieveModelMixin,
                             mixins.CreateModelMixin,
                             viewsets.GenericViewSet):
    """Manual stock adjustments (DAMAGED/MISSING/CORRECTION/OTHER).

    Every create writes one ADJUSTMENT ledger row and can never be
    edited or deleted afterwards - corrections are new rows.
    """

    serializer_class = StockAdjustmentSerializer
    permission_classes = [IsAuthenticated, IsOwnerOrManager]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter,
                       filters.OrderingFilter]
    filterset_fields = ["shop", "reason", "variant"]
    search_fields = ["reference", "note", "variant__product__code"]
    ordering_fields = ["created_at", "reference"]
    ordering = ["-id"]
    pagination_class = None
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        return (
            StockAdjustment.objects.select_related(
                "shop", "variant", "variant__product", "variant__unit",
                "created_by", "transfer",
            )
            .order_by("-id")
        )

    def get_serializer_class(self):
        if self.action == "create":
            return StockAdjustmentWriteSerializer
        return StockAdjustmentSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        adj = serializer.save()
        return Response(
            StockAdjustmentSerializer(
                adj, context=self.get_serializer_context()
            ).data,
            status=status.HTTP_201_CREATED,
        )
