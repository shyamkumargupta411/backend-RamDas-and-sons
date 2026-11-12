from django.contrib import admin

from .models import Sale, SaleAttachment, SaleItem


class SaleItemInline(admin.TabularInline):
    model = SaleItem
    extra = 0
    readonly_fields = ("variant", "quantity", "unit")


class SaleAttachmentInline(admin.TabularInline):
    model = SaleAttachment
    extra = 0
    readonly_fields = ("file", "content_type", "size_bytes", "uploaded_at")


@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    list_display = (
        "reference", "shop", "employee", "customer_name",
        "status", "sold_at",
    )
    list_filter = ("status", "shop")
    search_fields = ("reference", "customer_name", "employee__username")
    readonly_fields = (
        "reference", "sold_at", "corrects", "correction_reason",
        "corrected_by", "corrected_at", "idempotency_key",
    )
    inlines = [SaleItemInline, SaleAttachmentInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        # Sales are immutable in the admin: corrections happen via the API.
        return False
