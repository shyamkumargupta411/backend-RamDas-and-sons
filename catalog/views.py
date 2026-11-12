from django.db.models import Count, Q
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from accounts.permissions import CanManageProducts

from .models import Product, ProductCategory, ProductVariant, Unit
from .serializers import (
    ProductCategorySerializer,
    ProductSerializer,
    ProductVariantSerializer,
    ProductWriteSerializer,
    UnitSerializer,
)


class CatalogWritePermissionMixin:
    """SAFE methods: any authenticated user. Writes: CanManageProducts."""

    def get_permissions(self):
        if self.request.method in ("GET", "HEAD", "OPTIONS"):
            return [IsAuthenticated()]
        return [CanManageProducts()]


class UnitViewSet(CatalogWritePermissionMixin, viewsets.ReadOnlyModelViewSet):
    """Read-only unit list for forms."""

    serializer_class = UnitSerializer
    queryset = Unit.objects.filter(is_active=True)
    lookup_field = "id"
    pagination_class = None


class ProductCategoryViewSet(CatalogWritePermissionMixin, viewsets.ModelViewSet):
    serializer_class = ProductCategorySerializer
    queryset = ProductCategory.objects.filter(is_deleted=False)
    lookup_field = "id"

    def get_queryset(self):
        qs = super().get_queryset().annotate(product_count=Count("products"))
        params = self.request.query_params
        search = params.get("search")
        if search:
            qs = qs.filter(
                Q(name__icontains=search) | Q(code__icontains=search)
            )
        form_factor = params.get("form_factor")
        if form_factor:
            qs = qs.filter(form_factor=form_factor)
        active = params.get("is_active")
        if active in ("1", "true", "True"):
            qs = qs.filter(is_active=True)
        return qs

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user, updated_by=self.request.user)

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)

    def perform_destroy(self, instance):
        instance.is_deleted = True
        instance.is_active = False
        instance.updated_by = self.request.user
        instance.save(
            update_fields=["is_deleted", "is_active", "updated_by", "updated_at"]
        )


class ProductViewSet(CatalogWritePermissionMixin, viewsets.ModelViewSet):
    queryset = Product.objects.filter(is_deleted=False)
    lookup_field = "id"

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return ProductWriteSerializer
        return ProductSerializer

    def get_queryset(self):
        qs = super().get_queryset().select_related("category").prefetch_related(
            "variants__unit"
        )
        params = self.request.query_params
        search = params.get("search")
        if search:
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(code__icontains=search)
                | Q(material__icontains=search)
                | Q(grade__icontains=search))
        category = params.get("category")
        if category:
            qs = qs.filter(category_id=category)
        status_val = params.get("status")
        if status_val:
            qs = qs.filter(status=status_val)
        material = params.get("material")
        if material:
            qs = qs.filter(material__icontains=material)
        grade = params.get("grade")
        if grade:
            qs = qs.filter(grade__icontains=grade)
        return qs.order_by("code")

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user, updated_by=self.request.user)

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)

    def perform_destroy(self, instance):
        instance.is_deleted = True
        instance.status = Product.Status.DISCONTINUED
        instance.updated_by = self.request.user
        instance.save(
            update_fields=[
                "is_deleted",
                "status",
                "updated_by",
                "updated_at",
            ]
        )


class ProductVariantViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only variants (for the searchable selector)."""

    serializer_class = ProductVariantSerializer
    permission_classes = [IsAuthenticated]
    queryset = ProductVariant.objects.filter(
        is_deleted=False, is_active=True, product__is_deleted=False
    ).select_related("product", "unit")
    lookup_field = "id"

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        product = params.get("product")
        if product:
            qs = qs.filter(product_id=product)
        search = params.get("search")
        if search:
            qs = qs.filter(
                Q(product__name__icontains=search)
                | Q(product__code__icontains=search)
                | Q(spec_key__icontains=search)
            )
        return qs
