from decimal import Decimal

from django.db import models
from simple_history.models import HistoricalRecords

from accounts.models import Shop, User
from catalog.models import ProductVariant, Unit


class Sale(models.Model):
    """Inventory-outward record for a sale (no prices — Stage 6).

    Numbered SALE-000125. Corrections never edit in place: the original
    is marked CORRECTED (stock reversed) and a linked corrected sale is
    created with a mandatory reason, forming a correction chain via
    ``corrects``.
    """

    class Status(models.TextChoices):
        COMPLETED = "COMPLETED", "Completed"
        CORRECTED = "CORRECTED", "Corrected"

    reference = models.CharField(max_length=20, unique=True, editable=False)
    shop = models.ForeignKey(
        Shop, on_delete=models.PROTECT, related_name="sales"
    )
    employee = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="sales_made",
        help_text="Seller the sale is attributed to.",
    )
    customer_name = models.CharField(max_length=160, blank=True)
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.COMPLETED,
        db_index=True,
    )
    sold_at = models.DateTimeField(auto_now_add=True, db_index=True)

    # Correction chain: the new sale points at the one it corrects.
    corrects = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="corrections",
    )
    correction_reason = models.TextField(blank=True)
    corrected_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sales_corrected",
        help_text="User who corrected this (original) sale.",
    )
    corrected_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sales_created",
    )
    # Double-submit protection: an optional client token that makes
    # re-posting the same sale idempotent.
    idempotency_key = models.CharField(
        max_length=64, null=True, blank=True, unique=True
    )

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        return f"{self.reference} @ {self.shop.code}"

    history = HistoricalRecords()

    @property
    def correction_chain(self):
        """Ancestors first: every sale in this correction chain."""
        chain = []
        node = self
        seen = set()
        while node is not None and node.id not in seen:
            seen.add(node.id)
            chain.append(node)
            node = node.corrects
        chain.reverse()
        return chain


class SaleItem(models.Model):
    """One variant line. Stores the entered quantity and unit as recorded."""

    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="items")
    variant = models.ForeignKey(
        ProductVariant, on_delete=models.PROTECT, related_name="sale_items"
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    unit = models.ForeignKey(
        Unit,
        on_delete=models.PROTECT,
        related_name="sale_items",
        help_text="Unit as entered by the seller (defaults to the variant unit).",
    )

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.sale.reference} x{self.quantity} ({self.variant_id})"


class SaleAttachment(models.Model):
    """Optional slip photo attached to a sale.

    Type and size are validated in the serializer: JPEG/PNG/WebP, <= 5 MB.
    """

    ALLOWED_TYPES = ("image/jpeg", "image/png", "image/webp")
    MAX_SIZE = 5 * 1024 * 1024  # 5 MB

    sale = models.ForeignKey(
        Sale, on_delete=models.CASCADE, related_name="attachments"
    )
    file = models.FileField(upload_to="sales/slips/%Y/%m/")
    content_type = models.CharField(max_length=60, blank=True)
    size_bytes = models.PositiveIntegerField(default=0)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.sale.reference} slip ({self.content_type})"
