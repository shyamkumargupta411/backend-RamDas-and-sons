from django.contrib import admin

from .models import Product, ProductCategory, ProductVariant, Unit


@admin.register(Unit)
class UnitAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "unit_type", "is_active")
    list_filter = ("unit_type", "is_active")
    search_fields = ("code", "name")


@admin.register(ProductCategory)
class ProductCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "form_factor", "sort_order", "is_active", "is_deleted")
    list_filter = ("form_factor", "is_active", "is_deleted")
    search_fields = ("name", "code")
    prepopulated_fields = {"code": ("name",)}


class ProductVariantInline(admin.TabularInline):
    model = ProductVariant
    extra = 0
    readonly_fields = ("spec_key", "created_at", "updated_at")


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "category", "material", "grade", "status", "is_deleted")
    list_filter = ("status", "category", "material", "is_deleted")
    search_fields = ("code", "name", "material", "grade")
    inlines = [ProductVariantInline]
    readonly_fields = ("code", "created_by", "updated_by", "created_at", "updated_at")


@admin.register(ProductVariant)
class ProductVariantAdmin(admin.ModelAdmin):
    list_display = (
        "product",
        "spec_key",
        "thickness",
        "width",
        "length",
        "diameter",
        "weight",
        "unit",
        "is_active",
    )
    list_filter = ("unit", "is_active", "is_deleted")
    search_fields = ("product__code", "product__name", "spec_key")
    readonly_fields = ("spec_key",)
