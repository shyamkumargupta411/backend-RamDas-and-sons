from django.contrib import admin

from .models import (
    ProductReturn,
    StockAdjustment,
    StockLevel,
    StockMovement,
    StockReceipt,
    StockReceiptLine,
    StockTransfer,
    StockTransferItem,
)


class StockReceiptLineInline(admin.TabularInline):
    model = StockReceiptLine
    extra = 0
    readonly_fields = ("variant", "quantity")


class StockTransferItemInline(admin.TabularInline):
    model = StockTransferItem
    extra = 0
    readonly_fields = (
        "variant",
        "quantity",
        "received_quantity",
        "shortage_quantity",
        "shortage_reason",
    )


@admin.register(StockLevel)
class StockLevelAdmin(admin.ModelAdmin):
    list_display = ("shop", "variant", "quantity", "updated_at")
    list_filter = ("shop",)
    search_fields = ("variant__product__code", "variant__product__name")
    autocomplete_fields = ("shop", "variant")


@admin.register(StockReceipt)
class StockReceiptAdmin(admin.ModelAdmin):
    list_display = ("reference", "shop", "supplier", "created_by", "created_at")
    list_filter = ("shop",)
    search_fields = ("reference", "supplier")
    inlines = [StockReceiptLineInline]
    readonly_fields = ("reference", "created_by", "created_at")


@admin.register(StockTransfer)
class StockTransferAdmin(admin.ModelAdmin):
    list_display = (
        "reference", "source", "destination", "status",
        "created_by", "dispatched_at", "received_at",
    )
    list_filter = ("status", "source", "destination")
    search_fields = ("reference",)
    inlines = [StockTransferItemInline]
    readonly_fields = (
        "reference", "created_by", "dispatched_by", "received_by",
        "cancelled_by", "created_at", "dispatched_at", "received_at",
        "cancelled_at",
    )


@admin.register(StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = (
        "transaction_id", "kind", "shop", "variant",
        "quantity", "direction", "created_at",
    )
    list_filter = ("kind", "shop")
    search_fields = ("transaction_id", "variant__product__code")
    readonly_fields = [f.name for f in StockMovement._meta.fields]


@admin.register(StockAdjustment)
class StockAdjustmentAdmin(admin.ModelAdmin):
    """Immutable record: view only (Stage 7 — never edited afterwards)."""

    list_display = (
        "reference", "shop", "variant", "quantity", "direction",
        "reason", "created_by", "created_at",
    )
    list_filter = ("reason", "shop")
    search_fields = ("reference",)
    readonly_fields = [f.name for f in StockAdjustment._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ProductReturn)
class ProductReturnAdmin(admin.ModelAdmin):
    """Immutable record: view only (decisions happen through the API)."""

    list_display = (
        "reference", "shop", "variant", "quantity", "status",
        "created_by", "decided_by", "created_at",
    )
    list_filter = ("status", "shop")
    search_fields = ("reference", "customer_name")
    readonly_fields = [f.name for f in ProductReturn._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
