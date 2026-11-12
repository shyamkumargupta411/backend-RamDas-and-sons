from decimal import Decimal

from django.db import models
from simple_history.models import HistoricalRecords

from accounts.models import Shop, User
from catalog.models import ProductVariant


class StockTransfer(models.Model):
    """Transfer of stock between locations (warehouse->shop or shop->shop).

    Lifecycle: DRAFT -> DISPATCHED -> RECEIVED (or CANCELLED).
    Dispatch moves stock source -> IN_TRANSIT; receive moves it
    IN_TRANSIT -> destination. Cancel of a dispatched transfer is a
    reversal (IN_TRANSIT -> source).
    """

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        DISPATCHED = "DISPATCHED", "Dispatched"
        RECEIVED = "RECEIVED", "Received"
        CANCELLED = "CANCELLED", "Cancelled"

    reference = models.CharField(max_length=20, unique=True, editable=False)
    source = models.ForeignKey(
        Shop,
        on_delete=models.PROTECT,
        related_name="transfers_out",
    )
    destination = models.ForeignKey(
        Shop,
        on_delete=models.PROTECT,
        related_name="transfers_in",
    )
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    note = models.TextField(blank=True)

    created_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="transfers_created",
    )
    dispatched_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="transfers_dispatched",
    )
    received_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="transfers_received",
    )
    cancelled_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="transfers_cancelled",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        return f"{self.reference}: {self.source.code} -> {self.destination.code}"

    history = HistoricalRecords()

    @property
    def in_transit_quantity(self):
        """Units currently in transit (only meaningful for DISPATCHED)."""
        if self.status != self.Status.DISPATCHED:
            return Decimal("0")
        return sum(
            item.quantity - item.received_quantity - item.shortage_quantity
            for item in self.items.all()
        )


class StockTransferItem(models.Model):
    """One variant line on a transfer, with partial-receipt bookkeeping."""

    transfer = models.ForeignKey(
        StockTransfer, on_delete=models.CASCADE, related_name="items"
    )
    variant = models.ForeignKey(
        ProductVariant, on_delete=models.PROTECT, related_name="transfer_items"
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    received_quantity = models.DecimalField(
        max_digits=14, decimal_places=3, default=Decimal("0")
    )
    shortage_quantity = models.DecimalField(
        max_digits=14, decimal_places=3, default=Decimal("0")
    )
    shortage_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["transfer", "variant"], name="unique_transfer_item"
            ),
        ]

    def __str__(self):
        return f"{self.transfer.reference} x{self.quantity} ({self.variant_id})"

    @property
    def pending_quantity(self):
        return (
            self.quantity - self.received_quantity - self.shortage_quantity
        )


class StockMovement(models.Model):
    """Immutable stock ledger row. Out/in rows of one action share a
    transaction_id (the transfer reference)."""

    class Kind(models.TextChoices):
        STOCK_IN = "STOCK_IN", "Stock In"
        DISPATCH_OUT = "DISPATCH_OUT", "Dispatch Out"
        DISPATCH_IN = "DISPATCH_IN", "Dispatch In (In-Transit)"
        RECEIVE_OUT = "RECEIVE_OUT", "Receive Out (In-Transit)"
        RECEIVE_IN = "RECEIVE_IN", "Receive In"
        CANCEL_OUT = "CANCEL_OUT", "Cancel Out (In-Transit)"
        CANCEL_IN = "CANCEL_IN", "Cancel In (Source)"
        SHORTAGE = "SHORTAGE", "Shortage Adjustment"
        SALE_OUT = "SALE_OUT", "Sale Out"
        SALE_REVERSAL = "SALE_REVERSAL", "Sale Reversal (Correction)"
        RETURN_IN = "RETURN_IN", "Return Accepted (In)"
        ADJUSTMENT = "ADJUSTMENT", "Manual Adjustment"

    transaction_id = models.CharField(
        max_length=20,
        help_text="Shared by the out/in ledger rows of one action "
        "(transfer reference or receipt reference).",
        db_index=True,
    )
    kind = models.CharField(max_length=16, choices=Kind.choices, db_index=True)
    shop = models.ForeignKey(
        Shop, on_delete=models.PROTECT, related_name="movements"
    )
    variant = models.ForeignKey(
        ProductVariant, on_delete=models.PROTECT, related_name="movements"
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    # +1 adds to the shop, -1 removes from the shop.
    direction = models.SmallIntegerField()
    transfer = models.ForeignKey(
        StockTransfer,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="movements",
    )
    created_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="movements_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.kind} {self.transaction_id} {self.quantity}"


class StockAdjustment(models.Model):
    """Manual or automatic stock correction (shortage, damage, missing,
    owner correction). Immutable once created — corrections are made by
    adding a new adjustment, never by editing this row.
    """

    class Reason(models.TextChoices):
        SHORTAGE = "SHORTAGE", "Shortage on receipt"
        DAMAGED = "DAMAGED", "Damaged"
        MISSING = "MISSING", "Missing / lost"
        CORRECTION = "CORRECTION", "Owner correction"
        OTHER = "OTHER", "Other"

    reference = models.CharField(max_length=20, unique=True, editable=False)
    shop = models.ForeignKey(
        Shop, on_delete=models.PROTECT, related_name="adjustments"
    )
    variant = models.ForeignKey(
        ProductVariant, on_delete=models.PROTECT, related_name="adjustments"
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    # +1 adds stock at the location, -1 removes it (damaged/missing).
    direction = models.SmallIntegerField(default=-1)
    reason = models.CharField(
        max_length=12, choices=Reason.choices, default=Reason.OTHER
    )
    note = models.TextField(blank=True)
    transfer = models.ForeignKey(
        StockTransfer,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="adjustments",
    )
    created_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="adjustments_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        sign = "-" if self.direction < 0 else "+"
        return f"{self.reference} {self.variant_id} {sign}{self.quantity}"

    history = HistoricalRecords()


class ProductReturn(models.Model):
    """Goods coming back from a customer.

    A return only increases stock when it is ACCEPTED; PENDING and
    REJECTED returns never touch stock or the ledger. Immutable after
    the decision (no edits, no deletes through the API).
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        ACCEPTED = "ACCEPTED", "Accepted"
        REJECTED = "REJECTED", "Rejected"

    reference = models.CharField(max_length=20, unique=True, editable=False)
    shop = models.ForeignKey(
        Shop, on_delete=models.PROTECT, related_name="product_returns"
    )
    variant = models.ForeignKey(
        ProductVariant, on_delete=models.PROTECT, related_name="product_returns"
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    customer_name = models.CharField(max_length=160, blank=True)
    sale = models.ForeignKey(
        "sales.Sale",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="returns",
    )
    reason = models.TextField()
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    created_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="returns_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    decided_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="returns_decided",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        return f"{self.return_id} {self.variant_id} x{self.quantity} ({self.status})"

    history = HistoricalRecords()


class StockLevel(models.Model):
    """Current quantity of one variant at one location (shop or warehouse)."""

    shop = models.ForeignKey(
        Shop, on_delete=models.PROTECT, related_name="stock_levels"
    )
    variant = models.ForeignKey(
        ProductVariant, on_delete=models.PROTECT, related_name="stock_levels"
    )
    quantity = models.DecimalField(
        max_digits=14, decimal_places=3, default=Decimal("0")
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["shop", "variant"], name="unique_stock_per_shop_variant"
            ),
        ]
        ordering = ["shop__name", "variant__product__code"]

    def __str__(self):
        return f"{self.shop.code} {self.variant_id}: {self.quantity}"

    history = HistoricalRecords()


class StockReceipt(models.Model):
    """Goods received into a location (stock-in document, e.g. RCV-0001)."""

    reference = models.CharField(max_length=20, unique=True, editable=False)
    shop = models.ForeignKey(
        Shop,
        on_delete=models.PROTECT,
        related_name="stock_receipts",
        help_text="Destination location (warehouse).",
    )
    supplier = models.CharField(max_length=160, blank=True)
    note = models.TextField(blank=True)

    created_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="stock_receipts_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        return f"{self.reference} -> {self.shop.code}"


class StockReceiptLine(models.Model):
    receipt = models.ForeignKey(
        StockReceipt, on_delete=models.CASCADE, related_name="lines"
    )
    variant = models.ForeignKey(
        ProductVariant, on_delete=models.PROTECT, related_name="receipt_lines"
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=3)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.receipt.reference} x{self.quantity} ({self.variant_id})"
