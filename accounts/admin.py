from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import EmployeeShopAssignment, Shop, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = (
        "username",
        "email",
        "first_name",
        "last_name",
        "role",
        "can_manage_products",
        "is_active",
    )
    list_filter = ("role", "can_manage_products", "is_active", "is_staff")
    fieldsets = DjangoUserAdmin.fieldsets + (
        (
            "Metal inventory access",
            {"fields": ("role", "can_manage_products")},
        ),
    )
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        (
            "Metal inventory access",
            {"fields": ("role", "can_manage_products")},
        ),
    )


@admin.register(Shop)
class ShopAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "code",
        "is_active",
        "is_deleted",
        "created_at",
    )
    list_filter = ("is_active", "is_deleted")
    search_fields = ("name", "code")
    readonly_fields = ("created_by", "updated_by", "created_at", "updated_at")


@admin.register(EmployeeShopAssignment)
class EmployeeShopAssignmentAdmin(admin.ModelAdmin):
    list_display = (
        "employee",
        "shop",
        "assigned_from",
        "assigned_to",
        "is_active",
    )
    list_filter = ("is_active", "shop")
    autocomplete_fields = ("employee", "shop")
    readonly_fields = ("created_by", "updated_by", "created_at", "updated_at")
