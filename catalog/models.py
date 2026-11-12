from django.db import models
from simple_history.models import HistoricalRecords

from accounts.models import User


class Unit(models.Model):
    """Measurement unit (KG, PIECE, FEET, ...)."""

    class UnitType(models.TextChoices):
        WEIGHT = "WEIGHT", "Weight"
        LENGTH = "LENGTH", "Length"
        COUNT = "COUNT", "Count"

    code = models.SlugField(max_length=20, unique=True)
    name = models.CharField(max_length=60)
    unit_type = models.CharField(max_length=10, choices=UnitType.choices)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["unit_type", "code"]

    def __str__(self):
        return self.code


class ProductCategory(models.Model):
    """Category drives which variant dimension fields the UI shows.

    form_factor values:
      INGOT - grade/weight/dimensions
      FLAT  - thickness/width/length (Patiya/Flat)
      PIPE  - diameter/thickness/length
      OTHER - rods, angles, channels (diameter/thickness/length/width)
    """

    class FormFactor(models.TextChoices):
        INGOT = "INGOT", "Ingot"
        FLAT = "FLAT", "Flat / Patiya"
        PIPE = "PIPE", "Pipe"
        OTHER = "OTHER", "Other metal"

    name = models.CharField(max_length=120, unique=True)
    code = models.SlugField(max_length=40, unique=True)
    form_factor = models.CharField(
        max_length=10,
        choices=FormFactor.choices,
        default=FormFactor.OTHER,
    )
    description = models.TextField(blank=True)
    sort_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)

    created_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="categories_created",
    )
    updated_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="categories_updated",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class Product(models.Model):
    """Product master. Code is auto PROD-0001."""

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"
        DISCONTINUED = "DISCONTINUED", "Discontinued"

    code = models.CharField(max_length=20, unique=True, editable=False)
    name = models.CharField(max_length=200)
    category = models.ForeignKey(
        ProductCategory,
        on_delete=models.PROTECT,
        related_name="products",
    )
    material = models.CharField(
        max_length=80,
        help_text="e.g. Mild Steel, Stainless Steel, Copper, Aluminium",
    )
    grade = models.CharField(
        max_length=80,
        blank=True,
        help_text="e.g. Fe500, SS304, TMT",
    )
    status = models.CharField(
        max_length=15,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
    )
    notes = models.TextField(blank=True)
    is_deleted = models.BooleanField(default=False)

    created_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="products_created",
    )
    updated_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="products_updated",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} - {self.name}"

    history = HistoricalRecords()


class ProductVariant(models.Model):
    """One sellable specification of a product (size / dimension set).

    spec_key guarantees the same specification is never duplicated for a
    product, so different sizes are never mixed.
    """

    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="variants",
    )
    thickness = models.DecimalField(
        max_digits=14, decimal_places=3, null=True, blank=True
    )
    width = models.DecimalField(
        max_digits=14, decimal_places=3, null=True, blank=True
    )
    length = models.DecimalField(
        max_digits=14, decimal_places=3, null=True, blank=True
    )
    diameter = models.DecimalField(
        max_digits=14, decimal_places=3, null=True, blank=True
    )
    weight = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        null=True,
        blank=True,
        help_text="Weight per piece in the variant's unit when unit is WEIGHT; "
        "otherwise nominal weight in KG.",
    )
    unit = models.ForeignKey(
        Unit,
        on_delete=models.PROTECT,
        related_name="variants",
    )
    # Conversion helpers: 1 piece = X KG / X FEET (enables unit conversion).
    weight_per_piece = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        null=True,
        blank=True,
        help_text="KG per piece (for PIECE <-> KG conversion).",
    )
    length_per_piece = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        null=True,
        blank=True,
        help_text="FEET per piece (for PIECE <-> FEET conversion).",
    )
    spec_key = models.CharField(max_length=255, editable=False)
    is_active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)

    created_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="variants_created",
    )
    updated_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="variants_updated",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["product__code", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["product", "spec_key"],
                name="unique_variant_spec_per_product",
            ),
        ]

    def __str__(self):
        return f"{self.product.code} [{self.spec_key}]"

    history = HistoricalRecords()

    def clean(self):
        from django.core.exceptions import ValidationError

        dims = [
            self.thickness,
            self.width,
            self.length,
            self.diameter,
            self.weight,
        ]
        if all(d is None for d in dims):
            raise ValidationError(
                "A variant needs at least one of thickness, width, length, "
                "diameter or weight."
            )

    def save(self, *args, **kwargs):
        from .services import make_spec_key

        self.spec_key = make_spec_key(
            thickness=self.thickness,
            width=self.width,
            length=self.length,
            diameter=self.diameter,
            weight=self.weight,
            unit_code=self.unit.code if self.unit_id else "",
        )
        super().save(*args, **kwargs)
