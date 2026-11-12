from django.db import models
from django.utils import timezone

from accounts.models import Shop, User
from catalog.models import ProductVariant


class LowStockThreshold(models.Model):
    """Minimum acceptable quantity per variant at a shop. Below this
    triggers a low-stock alert on the dashboard."""

    variant = models.ForeignKey(
        ProductVariant,
        on_delete=models.CASCADE,
        related_name="low_stock_thresholds",
    )
    shop = models.ForeignKey(
        Shop,
        on_delete=models.CASCADE,
        related_name="low_stock_thresholds",
    )
    threshold_quantity = models.DecimalField(
        max_digits=14, decimal_places=3, default=0
    )
    is_active = models.BooleanField(default=True)

    created_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="thresholds_created",
    )
    updated_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="thresholds_updated",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["variant", "shop"],
                name="unique_threshold_per_variant_shop",
            )
        ]
        ordering = ["shop__name", "variant__product__code"]

    def __str__(self):
        return f"{self.shop.code} / {self.variant.spec_key} -> {self.threshold_quantity}"