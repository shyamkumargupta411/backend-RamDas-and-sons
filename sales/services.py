from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.models import Shop
from inventory.models import StockLevel, StockMovement
from inventory.services import apply_movement

from .models import Sale, SaleAttachment, SaleItem


def next_sale_code() -> str:
    """Allocate the next SALE-000125 style reference under a row lock."""
    from django.db.models import Max

    last = Sale.objects.select_for_update().aggregate(max_id=Max("id"))["max_id"]
    if last is None:
        return "SALE-000001"
    return f"SALE-{last + 1:06d}"


def _validate_shop(shop: Shop):
    if shop is None:
        raise ValidationError("A sale needs a shop.")
    if shop.shop_type == Shop.ShopType.IN_TRANSIT:
        raise ValidationError("Sales cannot be recorded at the in-transit location.")
    if shop.is_deleted or not shop.is_active:
        raise ValidationError("This location is inactive.")


def _validate_lines(lines):
    """Normalize + validate sale lines. Returns [{variant, quantity, unit}]."""
    if not lines:
        raise ValidationError("A sale needs at least one line.")
    normalized = []
    seen = set()
    for line in lines:
        variant = line.get("variant")
        if variant is None:
            raise ValidationError("Each line needs a product variant.")
        if variant.id in seen:
            raise ValidationError(
                f"Variant {variant} appears more than once in the sale."
            )
        seen.add(variant.id)
        qty = line.get("quantity")
        if qty is None or qty <= 0:
            raise ValidationError("Line quantities must be greater than zero.")
        unit = line.get("unit") or variant.unit
        normalized.append({"variant": variant, "quantity": qty, "unit": unit})
    return normalized


def _check_stock(shop, normalized):
    """All-or-nothing availability check before any deduction."""
    for line in normalized:
        qty = StockLevel.objects.filter(
            shop=shop, variant=line["variant"]
        ).values_list("quantity", flat=True).first() or Decimal("0")
        if qty < line["quantity"]:
            raise ValidationError(
                f"Insufficient stock for {line['variant']} at {shop.code} "
                f"(have {qty}, need {line['quantity']})."
            )


def _apply_sale_lines(sale, normalized, user):
    for line in normalized:
        SaleItem.objects.create(
            sale=sale,
            variant=line["variant"],
            quantity=line["quantity"],
            unit=line["unit"],
        )
        apply_movement(
            transaction_id=sale.reference,
            kind=StockMovement.Kind.SALE_OUT,
            shop=sale.shop,
            variant=line["variant"],
            quantity=line["quantity"],
            direction=-1,
            user=user,
        )


def _store_attachments(sale, files):
    for f in files or []:
        if not f:
            continue
        ctype = getattr(f, "content_type", "") or ""
        if ctype not in SaleAttachment.ALLOWED_TYPES:
            raise ValidationError(
                "Slip photo must be a JPEG, PNG or WebP image."
            )
        if f.size > SaleAttachment.MAX_SIZE:
            raise ValidationError("Slip photo must be 5 MB or smaller.")
        SaleAttachment.objects.create(
            sale=sale,
            file=f,
            content_type=ctype,
            size_bytes=f.size,
        )


@transaction.atomic
def create_sale(*, shop, lines, employee=None, customer_name="",
                user=None, idempotency_key=None, files=None):
    """Record a sale: validate stock, deduct via apply_movement.

    Returns (sale, created). When ``idempotency_key`` matches an existing
    sale the existing row is returned with created=False (no deduction).
    """
    if idempotency_key:
        existing = Sale.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            return existing, False

    _validate_shop(shop)
    normalized = _validate_lines(lines)
    employee = employee or user
    if employee is None:
        raise ValidationError("A sale needs an employee.")
    _check_stock(shop, normalized)

    try:
        # Savepoint so a concurrent double-submit hitting the unique
        # idempotency_key constraint can recover instead of poisoning
        # the outer transaction.
        with transaction.atomic():
            sale = Sale.objects.create(
                reference=next_sale_code(),
                shop=shop,
                employee=employee,
                customer_name=(customer_name or "").strip(),
                created_by=user,
                idempotency_key=idempotency_key or None,
            )
    except IntegrityError:
        if idempotency_key:
            existing = Sale.objects.filter(
                idempotency_key=idempotency_key
            ).first()
            if existing:
                return existing, False
        raise
    _apply_sale_lines(sale, normalized, user)
    _store_attachments(sale, files)
    return sale, True


@transaction.atomic
def correct_sale(sale, *, lines, reason, user=None, customer_name=None):
    """Reverse the original sale and create a linked corrected sale.

    Manager/Owner only (enforced by the view). The mandatory reason is
    stored on the new sale; the original is marked CORRECTED. Full history
    is kept through the ``corrects`` chain.
    """
    if sale.status != Sale.Status.COMPLETED:
        raise ValidationError("Only completed sales can be corrected.")
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("A correction reason is required.")
    normalized = _validate_lines(lines)

    # 1. Reverse the original sale: stock goes back to the shop.
    for item in sale.items.select_related("variant"):
        apply_movement(
            transaction_id=sale.reference,
            kind=StockMovement.Kind.SALE_REVERSAL,
            shop=sale.shop,
            variant=item.variant,
            quantity=item.quantity,
            direction=1,
            user=user,
        )

    # 2. Mark the original as corrected.
    sale.status = Sale.Status.CORRECTED
    sale.corrected_by = user
    sale.corrected_at = timezone.now()
    sale.save(update_fields=["status", "corrected_by", "corrected_at"])

    # 3. Create the corrected sale (same shop/employee; new lines).
    corrected = Sale.objects.create(
        reference=next_sale_code(),
        shop=sale.shop,
        employee=sale.employee,
        customer_name=(
            customer_name if customer_name is not None else sale.customer_name
        ),
        corrects=sale,
        correction_reason=reason,
        created_by=user,
    )
    _check_stock(corrected.shop, normalized)
    _apply_sale_lines(corrected, normalized, user)
    return corrected
