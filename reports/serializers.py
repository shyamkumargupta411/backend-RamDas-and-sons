from rest_framework import serializers

from accounts.models import Shop, User
from catalog.models import ProductVariant

from .models import LowStockThreshold


class LowStockThresholdSerializer(serializers.ModelSerializer):
    variant_label = serializers.CharField(
        source="variant.spec_key", read_only=True
    )
    product_code = serializers.CharField(
        source="variant.product.code", read_only=True
    )
    product_name = serializers.CharField(
        source="variant.product.name", read_only=True
    )
    unit_code = serializers.CharField(source="variant.unit.code", read_only=True)
    shop_name = serializers.CharField(source="shop.name", read_only=True)
    shop_code = serializers.CharField(source="shop.code", read_only=True)

    class Meta:
        model = LowStockThreshold
        fields = [
            "id",
            "variant",
            "variant_label",
            "product_code",
            "product_name",
            "unit_code",
            "shop",
            "shop_name",
            "shop_code",
            "threshold_quantity",
            "is_active",
            "created_by",
            "updated_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "created_by",
            "updated_by",
            "created_at",
            "updated_at",
        ]


class DashboardQuerySerializer(serializers.Serializer):
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    shop = serializers.IntegerField(required=False)
    product = serializers.IntegerField(required=False)
    employee = serializers.IntegerField(required=False)


class ReportQuerySerializer(serializers.Serializer):
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    shop = serializers.IntegerField(required=False)
    product = serializers.IntegerField(required=False)
    employee = serializers.IntegerField(required=False)
    report_type = serializers.ChoiceField(
        choices=[
            "product-wise",
            "shop-wise",
            "employee-wise",
            "stock-movement",
        ],
        default="product-wise",
    )