from rest_framework import serializers

from .models import Product, ProductCategory, ProductVariant, Unit
from .services import make_spec_key


class UnitSerializer(serializers.ModelSerializer):
    class Meta:
        model = Unit
        fields = ["id", "code", "name", "unit_type", "is_active"]
        read_only_fields = fields


class ProductCategorySerializer(serializers.ModelSerializer):
    product_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = ProductCategory
        fields = [
            "id",
            "name",
            "code",
            "form_factor",
            "description",
            "sort_order",
            "is_active",
            "product_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class ProductVariantSerializer(serializers.ModelSerializer):
    unit = serializers.SlugRelatedField(
        slug_field="code", queryset=Unit.objects.all()
    )
    unit_code = serializers.CharField(source="unit.code", read_only=True)
    unit_name = serializers.CharField(source="unit.name", read_only=True)
    spec_label = serializers.SerializerMethodField()

    class Meta:
        model = ProductVariant
        fields = [
            "id",
            "product",
            "thickness",
            "width",
            "length",
            "diameter",
            "weight",
            "unit",
            "unit_code",
            "unit_name",
            "weight_per_piece",
            "length_per_piece",
            "spec_key",
            "spec_label",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "product",
            "spec_key",
            "created_at",
            "updated_at",
        ]

    def get_spec_label(self, obj):
        parts = []
        if obj.thickness is not None:
            parts.append(f"T {obj.thickness}")
        if obj.width is not None:
            parts.append(f"W {obj.width}")
        if obj.length is not None:
            parts.append(f"L {obj.length}")
        if obj.diameter is not None:
            parts.append(f"D {obj.diameter}")
        if obj.weight is not None:
            parts.append(f"Wt {obj.weight}")
        return " × ".join(parts) if parts else obj.unit.code

    def validate(self, attrs):
        instance = self.instance
        thickness = attrs.get("thickness", getattr(instance, "thickness", None))
        width = attrs.get("width", getattr(instance, "width", None))
        length = attrs.get("length", getattr(instance, "length", None))
        diameter = attrs.get("diameter", getattr(instance, "diameter", None))
        weight = attrs.get("weight", getattr(instance, "weight", None))
        unit = attrs.get("unit", getattr(instance, "unit", None))
        if all(v is None for v in (thickness, width, length, diameter, weight)):
            raise serializers.ValidationError(
                "Provide at least one of thickness, width, length, diameter "
                "or weight."
            )
        if unit is None:
            raise serializers.ValidationError({"unit": "This field is required."})
        return attrs


class ProductSerializer(serializers.ModelSerializer):
    """Read serializer: includes nested variants."""

    category_name = serializers.CharField(source="category.name", read_only=True)
    category_form_factor = serializers.CharField(
        source="category.form_factor", read_only=True
    )
    variants = ProductVariantSerializer(many=True, read_only=True)
    variant_count = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "id",
            "code",
            "name",
            "category",
            "category_name",
            "category_form_factor",
            "material",
            "grade",
            "status",
            "notes",
            "variants",
            "variant_count",
            "is_deleted",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "code",
            "created_at",
            "updated_at",
            "is_deleted",
        ]

    def get_variant_count(self, obj):
        return obj.variants.count()


class ProductWriteSerializer(serializers.ModelSerializer):
    """Create/update with nested variants (replace-all when provided)."""

    variants = ProductVariantSerializer(many=True, required=False)
    category_name = serializers.CharField(source="category.name", read_only=True)
    category_form_factor = serializers.CharField(
        source="category.form_factor", read_only=True
    )

    class Meta:
        model = Product
        fields = [
            "id",
            "code",
            "name",
            "category",
            "category_name",
            "category_form_factor",
            "material",
            "grade",
            "status",
            "notes",
            "variants",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "code", "created_at", "updated_at"]

    def validate_variants(self, value):
        if not value:
            return value
        seen = set()
        for item in value:
            unit = item.get("unit")
            unit_code = unit.code if hasattr(unit, "code") else str(unit)
            key = make_spec_key(
                thickness=item.get("thickness"),
                width=item.get("width"),
                length=item.get("length"),
                diameter=item.get("diameter"),
                weight=item.get("weight"),
                unit_code=unit_code,
            )
            if key in seen:
                raise serializers.ValidationError(
                    "Duplicate variant specification — each size must be unique."
                )
            seen.add(key)
        return value

    def _write_variants(self, product, variants_data, user):
        # Replace-all keeps the ledger of specs clean for Stage 3 master data.
        product.variants.all().delete()
        for item in variants_data:
            ProductVariant.objects.create(
                product=product,
                created_by=user,
                updated_by=user,
                **item,
            )

    def create(self, validated_data):
        from .services import next_product_code

        variants_data = validated_data.pop("variants", [])
        user = validated_data.pop("created_by", None)
        validated_data.pop("updated_by", None)
        if user is None:
            user = self.context["request"].user
        validated_data["code"] = next_product_code()
        product = Product.objects.create(
            created_by=user, updated_by=user, **validated_data
        )
        self._write_variants(product, variants_data, user)
        return product

    def update(self, instance, validated_data):
        variants_data = validated_data.pop("variants", None)
        user = validated_data.pop("updated_by", None)
        validated_data.pop("created_by", None)
        if user is None:
            user = self.context["request"].user
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.updated_by = user
        instance.save()
        if variants_data is not None:
            self._write_variants(instance, variants_data, user)
        return instance
