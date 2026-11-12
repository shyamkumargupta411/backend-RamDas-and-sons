from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from accounts.models import Shop, User
from catalog.models import ProductVariant, Unit

from .models import Sale, SaleAttachment, SaleItem
from .services import correct_sale, create_sale


def _spec_label(variant) -> str:
    from catalog.serializers import ProductVariantSerializer

    return ProductVariantSerializer(variant).data["spec_label"]


class SaleItemSerializer(serializers.ModelSerializer):
    product_code = serializers.CharField(
        source="variant.product.code", read_only=True
    )
    product_name = serializers.CharField(
        source="variant.product.name", read_only=True
    )
    spec_label = serializers.SerializerMethodField()
    unit_code = serializers.CharField(source="unit.code", read_only=True)
    variant_unit_code = serializers.CharField(
        source="variant.unit.code", read_only=True
    )

    class Meta:
        model = SaleItem
        fields = [
            "id",
            "variant",
            "product_code",
            "product_name",
            "spec_label",
            "quantity",
            "unit",
            "unit_code",
            "variant_unit_code",
        ]
        read_only_fields = fields

    def get_spec_label(self, obj) -> str:
        return _spec_label(obj.variant)


class SaleAttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = SaleAttachment
        fields = ["id", "file", "content_type", "size_bytes", "uploaded_at"]
        read_only_fields = fields


class SaleSerializer(serializers.ModelSerializer):
    shop_name = serializers.CharField(source="shop.name", read_only=True)
    shop_code = serializers.CharField(source="shop.code", read_only=True)
    employee_username = serializers.CharField(
        source="employee.username", read_only=True
    )
    created_by_username = serializers.CharField(
        source="created_by.username", read_only=True, default=None
    )
    corrected_by_username = serializers.CharField(
        source="corrected_by.username", read_only=True, default=None
    )
    corrects_reference = serializers.CharField(
        source="corrects.reference", read_only=True, default=None
    )
    items = SaleItemSerializer(many=True, read_only=True)
    attachments = SaleAttachmentSerializer(many=True, read_only=True)
    correction_chain = serializers.SerializerMethodField()
    returns = serializers.SerializerMethodField()

    class Meta:
        model = Sale
        fields = [
            "id",
            "reference",
            "shop",
            "shop_name",
            "shop_code",
            "employee",
            "employee_username",
            "customer_name",
            "status",
            "sold_at",
            "items",
            "attachments",
            "corrects",
            "corrects_reference",
            "correction_reason",
            "correction_chain",
            "corrected_by",
            "corrected_by_username",
            "corrected_at",
            "created_by",
            "created_by_username",
            "returns",
        ]
        read_only_fields = fields

    def get_correction_chain(self, obj):
        return [
            {"id": s.id, "reference": s.reference, "status": s.status}
            for s in obj.correction_chain
        ]

    def get_returns(self, obj):
        """Customer returns linked to this sale (history is viewable)."""
        return [
            {
                "id": r.id,
                "reference": r.reference,
                "status": r.status,
                "quantity": str(r.quantity),
            }
            for r in obj.returns.all()
        ]


class SaleLineMixin:
    """Shared line validation: resolve PKs -> objects, basic checks.

    Full stock/idempotency checks happen in the services (atomic).
    """

    def validate_lines(self, lines):
        if not lines:
            raise serializers.ValidationError("A sale needs at least one line.")
        resolved = []
        seen = set()
        for raw in lines:
            variant_id = raw.get("variant")
            try:
                variant = ProductVariant.objects.select_related(
                    "product", "unit"
                ).get(pk=variant_id, is_deleted=False)
            except (ProductVariant.DoesNotExist, TypeError, ValueError):
                raise serializers.ValidationError(
                    "Unknown or deleted product variant."
                )
            if variant.id in seen:
                raise serializers.ValidationError(
                    "The same product variant appears more than once."
                )
            seen.add(variant.id)

            try:
                qty = Decimal(str(raw.get("quantity")))
            except (TypeError, ValueError, ArithmeticError):
                raise serializers.ValidationError(
                    "Each line needs a numeric quantity."
                )
            if qty <= 0:
                raise serializers.ValidationError(
                    "Line quantities must be greater than zero."
                )

            unit = None
            unit_id = raw.get("unit")
            if unit_id:
                try:
                    unit = Unit.objects.get(pk=unit_id)
                except (Unit.DoesNotExist, TypeError, ValueError):
                    raise serializers.ValidationError("Unknown unit.")

            resolved.append(
                {"variant": variant, "quantity": qty, "unit": unit}
            )
        return resolved


class SaleWriteSerializer(SaleLineMixin, serializers.Serializer):
    """POST /api/sales/ — JSON or multipart (with optional slip photo)."""

    shop = serializers.PrimaryKeyRelatedField(queryset=Shop.objects.all())
    customer_name = serializers.CharField(
        required=False, allow_blank=True, default=""
    )
    idempotency_key = serializers.CharField(
        required=False, allow_null=True, default=None, max_length=64
    )
    lines = serializers.ListField(child=serializers.DictField())
    slip = serializers.FileField(
        required=False, allow_null=True, default=None
    )

    created = True  # set by create(): False when idempotent replay

    def validate_shop(self, shop):
        user = self.context["request"].user
        if user.is_seller and not user.active_shops().filter(pk=shop.pk).exists():
            raise serializers.ValidationError(
                "You can only record sales at your assigned shop."
            )
        try:
            from .services import _validate_shop

            _validate_shop(shop)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages)
        return shop

    def validate_slip(self, f):
        if not f:
            return None
        ctype = getattr(f, "content_type", "") or ""
        if ctype not in SaleAttachment.ALLOWED_TYPES:
            raise serializers.ValidationError(
                "Slip photo must be a JPEG, PNG or WebP image."
            )
        if f.size > SaleAttachment.MAX_SIZE:
            raise serializers.ValidationError(
                "Slip photo must be 5 MB or smaller."
            )
        return f

    def create(self, validated_data):
        slip = validated_data.pop("slip", None)
        files = [slip] if slip else []
        try:
            sale, created = create_sale(
                shop=validated_data["shop"],
                lines=validated_data["lines"],
                customer_name=validated_data.get("customer_name", ""),
                idempotency_key=validated_data.get("idempotency_key"),
                user=self.context["request"].user,
                files=files,
            )
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages)
        self.created = created
        return sale


class SaleCorrectSerializer(SaleLineMixin, serializers.Serializer):
    """POST /api/sales/{id}/correct/ — mandatory reason + new lines."""

    reason = serializers.CharField()
    lines = serializers.ListField(child=serializers.DictField())
    customer_name = serializers.CharField(
        required=False, allow_null=True, default=None, allow_blank=True
    )

    def validate_reason(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError(
                "A correction reason is required."
            )
        return value

    def create(self, validated_data):
        sale = self.context["sale"]
        try:
            return correct_sale(
                sale,
                lines=validated_data["lines"],
                reason=validated_data["reason"],
                customer_name=validated_data.get("customer_name"),
                user=self.context["request"].user,
            )
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages)
