from django.core.exceptions import ValidationError
from rest_framework import serializers

from accounts.models import Shop
from catalog.models import ProductVariant

from .models import (
    ProductReturn,
    StockAdjustment,
    StockLevel,
    StockReceipt,
    StockReceiptLine,
    StockTransfer,
    StockTransferItem,
)
from .services import create_receipt


class StockLevelSerializer(serializers.ModelSerializer):
    shop_name = serializers.CharField(source="shop.name", read_only=True)
    shop_code = serializers.CharField(source="shop.code", read_only=True)
    product_id = serializers.IntegerField(source="variant.product_id", read_only=True)
    product_code = serializers.CharField(
        source="variant.product.code", read_only=True
    )
    product_name = serializers.CharField(
        source="variant.product.name", read_only=True
    )
    material = serializers.CharField(
        source="variant.product.material", read_only=True
    )
    spec_label = serializers.SerializerMethodField()
    unit_code = serializers.CharField(source="variant.unit.code", read_only=True)
    shop_type = serializers.CharField(source="shop.shop_type", read_only=True)

    class Meta:
        model = StockLevel
        fields = [
            "id",
            "shop",
            "shop_name",
            "shop_code",
            "shop_type",
            "variant",
            "product_id",
            "product_code",
            "product_name",
            "material",
            "spec_label",
            "unit_code",
            "quantity",
            "updated_at",
        ]
        read_only_fields = fields

    def get_spec_label(self, obj) -> str:
        from catalog.serializers import ProductVariantSerializer

        return ProductVariantSerializer(obj.variant).data["spec_label"]


class StockReceiptLineSerializer(serializers.ModelSerializer):
    product_code = serializers.CharField(
        source="variant.product.code", read_only=True
    )
    product_name = serializers.CharField(
        source="variant.product.name", read_only=True
    )
    spec_label = serializers.SerializerMethodField()
    unit_code = serializers.CharField(source="variant.unit.code", read_only=True)

    class Meta:
        model = StockReceiptLine
        fields = [
            "id",
            "variant",
            "product_code",
            "product_name",
            "spec_label",
            "unit_code",
            "quantity",
        ]
        read_only_fields = fields

    def get_spec_label(self, obj) -> str:
        from catalog.serializers import ProductVariantSerializer

        return ProductVariantSerializer(obj.variant).data["spec_label"]


class StockReceiptSerializer(serializers.ModelSerializer):
    lines = StockReceiptLineSerializer(many=True, read_only=True)
    shop_name = serializers.CharField(source="shop.name", read_only=True)
    shop_code = serializers.CharField(source="shop.code", read_only=True)
    created_by_username = serializers.CharField(
        source="created_by.username", read_only=True, default=None
    )

    class Meta:
        model = StockReceipt
        fields = [
            "id",
            "reference",
            "shop",
            "shop_name",
            "shop_code",
            "supplier",
            "note",
            "lines",
            "created_by",
            "created_by_username",
            "created_at",
        ]
        read_only_fields = fields


class StockReceiptWriteSerializer(serializers.Serializer):
    """POST payload: { shop, supplier?, note?, lines: [{variant, quantity}] }."""

    shop = serializers.IntegerField()
    supplier = serializers.CharField(required=False, allow_blank=True, default="")
    note = serializers.CharField(required=False, allow_blank=True, default="")
    lines = serializers.ListField(
        child=serializers.DictField(), allow_empty=False
    )

    def validate_shop(self, value):
        try:
            shop = Shop.objects.get(pk=value, is_deleted=False)
        except Shop.DoesNotExist:
            raise serializers.ValidationError("Shop not found.")
        if shop.shop_type != Shop.ShopType.WAREHOUSE:
            raise serializers.ValidationError(
                "Stock-in destination must be a warehouse."
            )
        return shop

    def validate_lines(self, value):
        seen = set()
        cleaned = []
        for i, raw in enumerate(value):
            variant_id = raw.get("variant")
            qty = raw.get("quantity")
            try:
                variant = ProductVariant.objects.select_related(
                    "product", "unit"
                ).get(pk=variant_id, is_deleted=False)
            except (ProductVariant.DoesNotExist, TypeError, ValueError):
                raise serializers.ValidationError(
                    f"Line {i + 1}: variant not found."
                )
            if not variant.is_active:
                raise serializers.ValidationError(
                    f"Line {i + 1}: variant is inactive."
                )
            try:
                qty = serializers.DecimalField(
                    max_digits=14, decimal_places=3
                ).to_internal_value(qty)
            except Exception:
                raise serializers.ValidationError(
                    f"Line {i + 1}: quantity must be a number."
                )
            if qty <= 0:
                raise serializers.ValidationError(
                    f"Line {i + 1}: quantity must be greater than zero."
                )
            if variant.id in seen:
                raise serializers.ValidationError(
                    f"Line {i + 1}: duplicate variant "
                    f"({variant.product.code} [{variant.spec_key}])."
                )
            seen.add(variant.id)
            cleaned.append({"variant": variant, "quantity": qty})
        return cleaned

    def create(self, validated_data):
        user = self.context["request"].user
        try:
            return create_receipt(
                shop=validated_data["shop"],
                lines=validated_data["lines"],
                supplier=validated_data.get("supplier", ""),
                note=validated_data.get("note", ""),
                user=user,
            )
        except ValidationError as exc:
            raise serializers.ValidationError(exc.messages)


# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------


def _variant_label(obj) -> str:
    from catalog.serializers import ProductVariantSerializer

    return ProductVariantSerializer(obj.variant).data["spec_label"]


class StockTransferItemSerializer(serializers.ModelSerializer):
    product_code = serializers.CharField(
        source="variant.product.code", read_only=True
    )
    product_name = serializers.CharField(
        source="variant.product.name", read_only=True
    )
    spec_label = serializers.SerializerMethodField()
    unit_code = serializers.CharField(source="variant.unit.code", read_only=True)
    pending_quantity = serializers.DecimalField(
        max_digits=14, decimal_places=3, read_only=True
    )

    class Meta:
        model = StockTransferItem
        fields = [
            "id",
            "variant",
            "product_code",
            "product_name",
            "spec_label",
            "unit_code",
            "quantity",
            "received_quantity",
            "shortage_quantity",
            "shortage_reason",
            "pending_quantity",
        ]
        read_only_fields = fields

    def get_spec_label(self, obj) -> str:
        return _variant_label(obj)


class StockTransferSerializer(serializers.ModelSerializer):
    items = StockTransferItemSerializer(many=True, read_only=True)
    source_name = serializers.CharField(source="source.name", read_only=True)
    source_code = serializers.CharField(source="source.code", read_only=True)
    destination_name = serializers.CharField(
        source="destination.name", read_only=True
    )
    destination_code = serializers.CharField(
        source="destination.code", read_only=True
    )
    created_by_username = serializers.CharField(
        source="created_by.username", read_only=True, default=None
    )
    dispatched_by_username = serializers.CharField(
        source="dispatched_by.username", read_only=True, default=None
    )
    received_by_username = serializers.CharField(
        source="received_by.username", read_only=True, default=None
    )
    cancelled_by_username = serializers.CharField(
        source="cancelled_by.username", read_only=True, default=None
    )
    in_transit_quantity = serializers.DecimalField(
        max_digits=14, decimal_places=3, read_only=True
    )

    class Meta:
        model = StockTransfer
        fields = [
            "id",
            "reference",
            "source",
            "source_name",
            "source_code",
            "destination",
            "destination_name",
            "destination_code",
            "status",
            "note",
            "items",
            "in_transit_quantity",
            "created_by",
            "created_by_username",
            "dispatched_by",
            "dispatched_by_username",
            "received_by",
            "received_by_username",
            "cancelled_by",
            "cancelled_by_username",
            "created_at",
            "dispatched_at",
            "received_at",
            "cancelled_at",
        ]
        read_only_fields = fields


class StockTransferWriteSerializer(serializers.ModelSerializer):
    """POST payload: { source, destination, note?, lines: [{variant, quantity}] }."""

    lines = serializers.ListField(
        child=serializers.DictField(), allow_empty=False
    )

    class Meta:
        model = StockTransfer
        fields = ["source", "destination", "note", "lines"]

    def validate_source(self, value):
        if value.shop_type == value.ShopType.IN_TRANSIT:
            raise serializers.ValidationError(
                "In-Transit cannot be a transfer source."
            )
        if value.is_deleted or not value.is_active:
            raise serializers.ValidationError("Source location is inactive.")
        return value

    def validate_destination(self, value):
        if value.shop_type == value.ShopType.IN_TRANSIT:
            raise serializers.ValidationError(
                "In-Transit cannot be a transfer destination."
            )
        if value.is_deleted or not value.is_active:
            raise serializers.ValidationError("Destination is inactive.")
        return value

    def validate(self, attrs):
        if attrs["source"] == attrs["destination"]:
            raise serializers.ValidationError(
                "Source and destination must be different."
            )
        return attrs

    def validate_lines(self, value):
        seen = set()
        cleaned = []
        for i, raw in enumerate(value):
            variant_id = raw.get("variant")
            qty = raw.get("quantity")
            try:
                variant = ProductVariant.objects.select_related(
                    "product", "unit"
                ).get(pk=variant_id, is_deleted=False)
            except (ProductVariant.DoesNotExist, TypeError, ValueError):
                raise serializers.ValidationError(
                    f"Line {i + 1}: variant not found."
                )
            if not variant.is_active:
                raise serializers.ValidationError(
                    f"Line {i + 1}: variant is inactive."
                )
            try:
                qty = serializers.DecimalField(
                    max_digits=14, decimal_places=3
                ).to_internal_value(qty)
            except Exception:
                raise serializers.ValidationError(
                    f"Line {i + 1}: quantity must be a number."
                )
            if qty <= 0:
                raise serializers.ValidationError(
                    f"Line {i + 1}: quantity must be greater than zero."
                )
            if variant.id in seen:
                raise serializers.ValidationError(
                    f"Line {i + 1}: duplicate variant "
                    f"({variant.product.code} [{variant.spec_key}])."
                )
            seen.add(variant.id)
            cleaned.append({"variant": variant, "quantity": qty})
        return cleaned

    def create(self, validated_data):
        from django.db import transaction

        from .services import next_transfer_code

        user = self.context["request"].user
        with transaction.atomic():
            transfer = StockTransfer.objects.create(
                reference=next_transfer_code(),
                source=validated_data["source"],
                destination=validated_data["destination"],
                note=validated_data.get("note", ""),
                created_by=user,
            )
            for line in validated_data["lines"]:
                StockTransferItem.objects.create(
                    transfer=transfer,
                    variant=line["variant"],
                    quantity=line["quantity"],
                )
        return transfer


class StockTransferReceiveLineSerializer(serializers.Serializer):
    """{ variant (or item), received_quantity, shortage_reason? }"""

    variant = serializers.IntegerField(required=False)
    item = serializers.IntegerField(required=False)
    received_quantity = serializers.DecimalField(
        max_digits=14, decimal_places=3, min_value=0, required=False,
        default=0,
    )
    shortage_reason = serializers.CharField(
        required=False, allow_blank=True, default=""
    )


class StockTransferReceiveSerializer(serializers.Serializer):
    """POST payload: { lines: [{variant|item, received_quantity, shortage_reason}] }."""

    lines = StockTransferReceiveLineSerializer(many=True, allow_empty=False)

    def validate_lines(self, value):
        transfer = self.context["transfer"]
        item_map = {
            item.variant_id: item for item in transfer.items.all()
        }
        by_id = {item.id: item for item in item_map.values()}
        seen = set()
        cleaned = []
        for i, raw in enumerate(value):
            item = None
            if raw.get("item") is not None:
                item = by_id.get(raw["item"])
            elif raw.get("variant") is not None:
                item = item_map.get(raw["variant"])
            if item is None:
                raise serializers.ValidationError(
                    f"Line {i + 1}: transfer item not found."
                )
            if item.id in seen:
                raise serializers.ValidationError(
                    f"Line {i + 1}: duplicate transfer item."
                )
            seen.add(item.id)
            received = raw.get("received_quantity") or 0
            reason = (raw.get("shortage_reason") or "").strip()
            if received > item.pending_quantity:
                raise serializers.ValidationError(
                    f"Line {i + 1}: received {received} exceeds pending "
                    f"{item.pending_quantity}."
                )
            if received < item.pending_quantity and not reason:
                # Partial without reason: allowed only if received > 0
                # (rest stays pending). received=0 with no reason is a no-op
                # unless they intend shortage -> require reason via shortage.
                pass
            cleaned.append(
                {
                    "item": item,
                    "received_quantity": received,
                    "shortage_reason": reason,
                }
            )
        return cleaned

    def save(self, **kwargs):
        from .services import receive_transfer

        transfer = self.context["transfer"]
        user = self.context["request"].user
        try:
            return receive_transfer(
                transfer, self.validated_data["lines"], user=user
            )
        except ValidationError as exc:
            raise serializers.ValidationError(exc.messages)


# ---------------------------------------------------------------------------
# Returns (Stage 7)
# ---------------------------------------------------------------------------


class ProductReturnSerializer(serializers.ModelSerializer):
    shop_name = serializers.CharField(source="shop.name", read_only=True)
    shop_code = serializers.CharField(source="shop.code", read_only=True)
    product_code = serializers.CharField(
        source="variant.product.code", read_only=True
    )
    product_name = serializers.CharField(
        source="variant.product.name", read_only=True
    )
    spec_label = serializers.SerializerMethodField()
    unit_code = serializers.CharField(source="variant.unit.code", read_only=True)
    sale_reference = serializers.CharField(
        source="sale.reference", read_only=True, default=None
    )
    created_by_username = serializers.CharField(
        source="created_by.username", read_only=True, default=None
    )
    decided_by_username = serializers.CharField(
        source="decided_by.username", read_only=True, default=None
    )

    class Meta:
        model = ProductReturn
        fields = [
            "id",
            "reference",
            "shop",
            "shop_name",
            "shop_code",
            "variant",
            "product_code",
            "product_name",
            "spec_label",
            "unit_code",
            "quantity",
            "customer_name",
            "sale",
            "sale_reference",
            "reason",
            "status",
            "created_by",
            "created_by_username",
            "created_at",
            "decided_by",
            "decided_by_username",
            "decided_at",
            "decision_note",
        ]
        read_only_fields = fields

    def get_spec_label(self, obj) -> str:
        return _variant_label(obj)


class ProductReturnWriteSerializer(serializers.Serializer):
    """POST /api/returns/ - registers a PENDING return (no stock yet)."""

    shop = serializers.IntegerField()
    variant = serializers.IntegerField()
    quantity = serializers.DecimalField(max_digits=14, decimal_places=3)
    reason = serializers.CharField()
    customer_name = serializers.CharField(
        required=False, allow_blank=True, default=""
    )
    sale = serializers.IntegerField(required=False, allow_null=True, default=None)

    def validate_shop(self, value):
        try:
            return Shop.objects.get(pk=value, is_deleted=False)
        except Shop.DoesNotExist:
            raise serializers.ValidationError("Shop not found.")

    def validate_variant(self, value):
        try:
            variant = ProductVariant.objects.select_related(
                "product", "unit"
            ).get(pk=value, is_deleted=False)
        except ProductVariant.DoesNotExist:
            raise serializers.ValidationError("Variant not found.")
        if not variant.is_active:
            raise serializers.ValidationError("Variant is inactive.")
        return variant

    def validate_reason(self, value):
        if not value.strip():
            raise serializers.ValidationError("A return reason is required.")
        return value.strip()

    def validate_sale(self, value):
        if not value:
            return None
        from sales.models import Sale

        try:
            return Sale.objects.get(pk=value)
        except Sale.DoesNotExist:
            raise serializers.ValidationError("Sale not found.")

    def create(self, validated_data):
        from .services import create_return

        user = self.context["request"].user
        try:
            return create_return(
                shop=validated_data["shop"],
                variant=validated_data["variant"],
                quantity=validated_data["quantity"],
                reason=validated_data["reason"],
                customer_name=validated_data.get("customer_name", ""),
                sale=validated_data.get("sale"),
                user=user,
            )
        except ValidationError as exc:
            raise serializers.ValidationError(exc.messages)


# ---------------------------------------------------------------------------
# Stock adjustments (Stage 7)
# ---------------------------------------------------------------------------


class StockAdjustmentSerializer(serializers.ModelSerializer):
    shop_name = serializers.CharField(source="shop.name", read_only=True)
    shop_code = serializers.CharField(source="shop.code", read_only=True)
    product_code = serializers.CharField(
        source="variant.product.code", read_only=True
    )
    product_name = serializers.CharField(
        source="variant.product.name", read_only=True
    )
    spec_label = serializers.SerializerMethodField()
    unit_code = serializers.CharField(source="variant.unit.code", read_only=True)
    created_by_username = serializers.CharField(
        source="created_by.username", read_only=True, default=None
    )

    class Meta:
        model = StockAdjustment
        fields = [
            "id",
            "reference",
            "shop",
            "shop_name",
            "shop_code",
            "variant",
            "product_code",
            "product_name",
            "spec_label",
            "unit_code",
            "quantity",
            "direction",
            "reason",
            "note",
            "transfer",
            "created_by",
            "created_by_username",
            "created_at",
        ]
        read_only_fields = fields

    def get_spec_label(self, obj) -> str:
        return _variant_label(obj)


class StockAdjustmentWriteSerializer(serializers.Serializer):
    """POST /api/stock-adjustments/ - immutable manual adjustment.

    reason: DAMAGED | MISSING | CORRECTION | OTHER (SHORTAGE is
    automatic from receipts). CORRECTION is owner-only and needs an
    explicit direction; everything else removes stock.
    """

    shop = serializers.IntegerField()
    variant = serializers.IntegerField()
    quantity = serializers.DecimalField(max_digits=14, decimal_places=3)
    reason = serializers.ChoiceField(
        choices=[
            StockAdjustment.Reason.DAMAGED,
            StockAdjustment.Reason.MISSING,
            StockAdjustment.Reason.CORRECTION,
            StockAdjustment.Reason.OTHER,
        ]
    )
    note = serializers.CharField()
    direction = serializers.IntegerField(
        required=False, allow_null=True, default=None
    )

    def validate_shop(self, value):
        try:
            return Shop.objects.get(pk=value, is_deleted=False)
        except Shop.DoesNotExist:
            raise serializers.ValidationError("Shop not found.")

    def validate_variant(self, value):
        try:
            variant = ProductVariant.objects.select_related(
                "product", "unit"
            ).get(pk=value, is_deleted=False)
        except ProductVariant.DoesNotExist:
            raise serializers.ValidationError("Variant not found.")
        if not variant.is_active:
            raise serializers.ValidationError("Variant is inactive.")
        return variant

    def validate_note(self, value):
        if not value.strip():
            raise serializers.ValidationError(
                "An adjustment reason (note) is required."
            )
        return value.strip()

    def validate_direction(self, value):
        if value is None:
            return value
        if value not in (1, -1):
            raise serializers.ValidationError("Direction must be +1 or -1.")
        return value

    def validate(self, attrs):
        if (
            attrs.get("reason") == StockAdjustment.Reason.CORRECTION
            and not self.context["request"].user.is_owner
        ):
            raise serializers.ValidationError(
                "Only the owner can record corrections."
            )
        return attrs

    def create(self, validated_data):
        from .services import create_adjustment

        user = self.context["request"].user
        try:
            return create_adjustment(
                shop=validated_data["shop"],
                variant=validated_data["variant"],
                quantity=validated_data["quantity"],
                reason=validated_data["reason"],
                note=validated_data["note"],
                direction=validated_data.get("direction"),
                user=user,
            )
        except ValidationError as exc:
            raise serializers.ValidationError(exc.messages)
