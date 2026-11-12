from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F

from accounts.models import Shop
from .models import (
    ProductReturn,
    StockAdjustment,
    StockLevel,
    StockMovement,
    StockReceipt,
    StockReceiptLine,
    StockTransfer,
)


def get_in_transit_shop() -> Shop:
    return Shop.objects.get(code="in-transit", shop_type=Shop.ShopType.IN_TRANSIT)


def next_receipt_code() -> str:
    """Allocate the next RCV-0001 style reference under a row lock."""
    from django.db.models import Max

    last = StockReceipt.objects.select_for_update().aggregate(
        max_id=Max("id")
    )["max_id"]
    if last is None:
        return "RCV-0001"
    return f"RCV-{last + 1:04d}"


def next_transfer_code() -> str:
    from django.db.models import Max

    last = StockTransfer.objects.select_for_update().aggregate(
        max_id=Max("id")
    )["max_id"]
    if last is None:
        return "TRF-0001"
    return f"TRF-{last + 1:04d}"


def next_adjustment_code() -> str:
    from django.db.models import Max

    last = StockAdjustment.objects.select_for_update().aggregate(
        max_id=Max("id")
    )["max_id"]
    if last is None:
        return "ADJ-0001"
    return f"ADJ-{last + 1:04d}"


def next_return_code() -> str:
    from django.db.models import Max

    last = ProductReturn.objects.select_for_update().aggregate(
        max_id=Max("id")
    )["max_id"]
    if last is None:
        return "RET-0001"
    return f"RET-{last + 1:04d}"


def _adjust_level(shop, variant, delta):
    """Add ``delta`` (signed) to a stock level, creating it if needed.

    The level row is locked via select_for_update so concurrent writers
    serialize; negative results are rejected (no negative stock).
    """
    StockLevel.objects.get_or_create(
        shop=shop, variant=variant, defaults={"quantity": Decimal("0")}
    )
    level = StockLevel.objects.select_for_update().get(
        shop=shop, variant=variant
    )
    new_qty = level.quantity + delta
    if new_qty < 0:
        raise ValidationError(
            f"Insufficient stock for {variant} at {shop.code} "
            f"(have {level.quantity}, need {-delta})."
        )
    level.quantity = new_qty
    level.save(update_fields=["quantity", "updated_at"])
    return level


def _ledger(*, transaction_id, kind, shop, variant, quantity, direction,
            transfer=None, user=None):
    StockMovement.objects.create(
        transaction_id=transaction_id,
        kind=kind,
        shop=shop,
        variant=variant,
        quantity=quantity,
        direction=direction,
        transfer=transfer,
        created_by=user,
    )


def apply_movement(*, transaction_id, kind, shop, variant, quantity,
                   direction, transfer=None, user=None):
    """Apply one signed stock movement: level update + ledger row.

    Public entry point used by sales (and any future app). Raises
    ValidationError when the result would make the level negative.
    ``quantity`` is always positive; ``direction`` is +1 (add) or -1 (remove).
    """
    if quantity is None or quantity <= 0:
        raise ValidationError("Movement quantity must be greater than zero.")
    if direction not in (1, -1):
        raise ValidationError("Movement direction must be +1 or -1.")
    _adjust_level(shop, variant, quantity * direction)
    _ledger(
        transaction_id=transaction_id,
        kind=kind,
        shop=shop,
        variant=variant,
        quantity=quantity,
        direction=direction,
        transfer=transfer,
        user=user,
    )


@transaction.atomic
def create_receipt(*, shop, lines, supplier="", note="", user=None) -> StockReceipt:
    """Create a stock-in receipt and apply quantities to stock levels.

    ``lines`` is a list of dicts: {"variant": <ProductVariant>, "quantity": Decimal}.
    Destination must be a warehouse; quantities must be > 0 and unique per variant.
    """
    if shop.shop_type != shop.ShopType.WAREHOUSE:
        raise ValidationError("Stock-in destination must be a warehouse.")
    if not lines:
        raise ValidationError("A receipt needs at least one line.")

    seen = set()
    for line in lines:
        qty = line["quantity"]
        if qty is None or qty <= 0:
            raise ValidationError("Line quantities must be greater than zero.")
        variant = line["variant"]
        if variant.id in seen:
            raise ValidationError(
                f"Variant {variant} appears more than once in the receipt."
            )
        seen.add(variant.id)

    receipt = StockReceipt.objects.create(
        reference=next_receipt_code(),
        shop=shop,
        supplier=supplier,
        note=note,
        created_by=user,
    )
    for line in lines:
        StockReceiptLine.objects.create(
            receipt=receipt, variant=line["variant"], quantity=line["quantity"]
        )
        _adjust_level(shop, line["variant"], line["quantity"])
        _ledger(
            transaction_id=receipt.reference,
            kind=StockMovement.Kind.STOCK_IN,
            shop=shop,
            variant=line["variant"],
            quantity=line["quantity"],
            direction=1,
            user=user,
        )
    return receipt


@transaction.atomic
def dispatch_transfer(transfer: StockTransfer, user=None) -> StockTransfer:
    """DRAFT -> DISPATCHED: deduct from source, add to the in-transit location.

    Out/in ledger rows share the transfer reference as transaction_id.
    """
    if transfer.status != StockTransfer.Status.DRAFT:
        raise ValidationError("Only draft transfers can be dispatched.")
    if transfer.source_id == transfer.destination_id:
        raise ValidationError("Source and destination must be different.")
    in_transit = get_in_transit_shop()

    items = list(transfer.items.select_related("variant"))
    if not items:
        raise ValidationError("Transfer has no items.")

    # Lock/validate ALL source levels first (all-or-nothing), then apply.
    for item in items:
        StockLevel.objects.get_or_create(
            shop=transfer.source,
            variant=item.variant,
            defaults={"quantity": Decimal("0")},
        )
        level = StockLevel.objects.select_for_update().get(
            shop=transfer.source, variant=item.variant
        )
        if level.quantity < item.quantity:
            raise ValidationError(
                f"Insufficient stock for {item.variant} at "
                f"{transfer.source.code}: have {level.quantity}, "
                f"need {item.quantity}."
            )

    for item in items:
        _adjust_level(transfer.source, item.variant, -item.quantity)
        _adjust_level(in_transit, item.variant, item.quantity)
        _ledger(
            transaction_id=transfer.reference,
            kind=StockMovement.Kind.DISPATCH_OUT,
            shop=transfer.source,
            variant=item.variant,
            quantity=item.quantity,
            direction=-1,
            transfer=transfer,
            user=user,
        )
        _ledger(
            transaction_id=transfer.reference,
            kind=StockMovement.Kind.DISPATCH_IN,
            shop=in_transit,
            variant=item.variant,
            quantity=item.quantity,
            direction=1,
            transfer=transfer,
            user=user,
        )

    transfer.status = StockTransfer.Status.DISPATCHED
    transfer.dispatched_by = user
    from django.utils import timezone

    transfer.dispatched_at = timezone.now()
    transfer.save(
        update_fields=[
            "status", "dispatched_by", "dispatched_at",
        ]
    )
    return transfer


@transaction.atomic
def receive_transfer(transfer: StockTransfer, lines, user=None) -> StockTransfer:
    """DISPATCHED -> RECEIVED (when fully accounted).

    ``lines``: [{"item": <StockTransferItem>, "received_quantity": Decimal,
    "shortage_reason": str}].

    - received units: in-transit -> destination
    - shortfall (pending - received) with a shortage_reason: written off
      from in-transit and recorded as an automatic StockAdjustment.
    - shortfall without a reason: left pending (partial receipt; call
      receive again later).
    """
    if transfer.status != StockTransfer.Status.DISPATCHED:
        raise ValidationError("Transfer is not dispatched.")
    in_transit = get_in_transit_shop()

    seen = set()
    for line in lines:
        item = line["item"]
        if item.transfer_id != transfer.id:
            raise ValidationError("Line does not belong to this transfer.")
        if item.id in seen:
            raise ValidationError("Duplicate transfer item in receive lines.")
        seen.add(item.id)

        received = line.get("received_quantity") or Decimal("0")
        reason = (line.get("shortage_reason") or "").strip()
        pending = item.pending_quantity
        if received < 0 or received > pending:
            raise ValidationError(
                f"Received quantity for {item.variant} must be between "
                f"0 and {pending} (pending)."
            )
        shortage = pending - received
        if shortage > 0 and not reason:
            # Partial receipt without shortage declaration: just record
            # what arrived; the rest stays pending for a later receive.
            pass

        # Move received units: in-transit -> destination.
        if received > 0:
            _adjust_level(in_transit, item.variant, -received)
            _adjust_level(transfer.destination, item.variant, received)
            _ledger(
                transaction_id=transfer.reference,
                kind=StockMovement.Kind.RECEIVE_OUT,
                shop=in_transit,
                variant=item.variant,
                quantity=received,
                direction=-1,
                transfer=transfer,
                user=user,
            )
            _ledger(
                transaction_id=transfer.reference,
                kind=StockMovement.Kind.RECEIVE_IN,
                shop=transfer.destination,
                variant=item.variant,
                quantity=received,
                direction=1,
                transfer=transfer,
                user=user,
            )
            item.received_quantity = F("received_quantity") + received

        # Shortage with a reason: write off from in-transit + auto adjustment.
        if shortage > 0 and reason:
            _adjust_level(in_transit, item.variant, -shortage)
            _ledger(
                transaction_id=transfer.reference,
                kind=StockMovement.Kind.SHORTAGE,
                shop=in_transit,
                variant=item.variant,
                quantity=shortage,
                direction=-1,
                transfer=transfer,
                user=user,
            )
            StockAdjustment.objects.create(
                reference=next_adjustment_code(),
                shop=in_transit,
                variant=item.variant,
                quantity=shortage,
                reason=StockAdjustment.Reason.SHORTAGE,
                note=reason,
                transfer=transfer,
                created_by=user,
            )
            item.shortage_quantity = F("shortage_quantity") + shortage
            item.shortage_reason = reason

        if received > 0 or (shortage > 0 and reason):
            item.save()
            item.refresh_from_db()

    transfer.refresh_from_db()
    transfer._prefetched_objects_cache = {}
    if all(item.pending_quantity == 0 for item in transfer.items.all()):
        transfer.status = StockTransfer.Status.RECEIVED
        transfer.received_by = user
        from django.utils import timezone

        transfer.received_at = timezone.now()
        transfer.save(
            update_fields=["status", "received_by", "received_at"]
        )
    return transfer


@transaction.atomic
def cancel_transfer(transfer: StockTransfer, user=None) -> StockTransfer:
    """Cancel via reversal only.

    - DRAFT: no stock has moved; just mark CANCELLED.
    - DISPATCHED: reverse remaining in-transit units back to the source
      (CANCEL_OUT at in-transit / CANCEL_IN at source, same transaction id).
      Already-received units stay at the destination.
    """
    if transfer.status == StockTransfer.Status.DRAFT:
        transfer.status = StockTransfer.Status.CANCELLED
        transfer.cancelled_by = user
        from django.utils import timezone

        transfer.cancelled_at = timezone.now()
        transfer.save(
            update_fields=["status", "cancelled_by", "cancelled_at"]
        )
        return transfer

    if transfer.status != StockTransfer.Status.DISPATCHED:
        raise ValidationError(
            "Only draft or dispatched transfers can be cancelled."
        )

    in_transit = get_in_transit_shop()
    items = list(transfer.items.select_related("variant"))
    for item in items:
        pending = item.pending_quantity
        if pending <= 0:
            continue
        _adjust_level(in_transit, item.variant, -pending)
        _adjust_level(transfer.source, item.variant, pending)
        _ledger(
            transaction_id=transfer.reference,
            kind=StockMovement.Kind.CANCEL_OUT,
            shop=in_transit,
            variant=item.variant,
            quantity=pending,
            direction=-1,
            transfer=transfer,
            user=user,
        )
        _ledger(
            transaction_id=transfer.reference,
            kind=StockMovement.Kind.CANCEL_IN,
            shop=transfer.source,
            variant=item.variant,
            quantity=pending,
            direction=1,
            transfer=transfer,
            user=user,
        )

    transfer.status = StockTransfer.Status.CANCELLED
    transfer.cancelled_by = user
    from django.utils import timezone

    transfer.cancelled_at = timezone.now()
    transfer.save(update_fields=["status", "cancelled_by", "cancelled_at"])
    return transfer


def _validate_location(shop):
    if shop.shop_type == Shop.ShopType.IN_TRANSIT:
        raise ValidationError(
            "Returns and adjustments cannot target the in-transit location."
        )


# ---------------------------------------------------------------------
# Product returns
# ---------------------------------------------------------------------


@transaction.atomic
def create_return(*, shop, variant, quantity, reason, customer_name="",
                  sale=None, user=None) -> ProductReturn:
    """Register a customer return as PENDING (no stock change yet)."""
    if quantity is None or quantity <= 0:
        raise ValidationError("Return quantity must be greater than zero.")
    if not (reason or "").strip():
        raise ValidationError("A return reason is required.")
    _validate_location(shop)
    if sale is not None and sale.shop_id != shop.pk:
        raise ValidationError(
            "The linked sale belongs to a different location."
        )

    return ProductReturn.objects.create(
        reference=next_return_code(),
        shop=shop,
        variant=variant,
        quantity=quantity,
        customer_name=(customer_name or "").strip(),
        sale=sale,
        reason=reason.strip(),
        created_by=user,
    )


@transaction.atomic
def accept_return(ret: ProductReturn, *, user=None) -> ProductReturn:
    """ACCEPTED: restock the location (+1 ledger row). Pending only."""
    if ret.status != ProductReturn.Status.PENDING:
        raise ValidationError("Only pending returns can be accepted.")
    apply_movement(
        transaction_id=ret.reference,
        kind=StockMovement.Kind.RETURN_IN,
        shop=ret.shop,
        variant=ret.variant,
        quantity=ret.quantity,
        direction=1,
        user=user,
    )
    from django.utils import timezone

    ret.status = ProductReturn.Status.ACCEPTED
    ret.decided_by = user
    ret.decided_at = timezone.now()
    ret.save(update_fields=["status", "decided_by", "decided_at"])
    return ret


@transaction.atomic
def reject_return(ret: ProductReturn, *, user=None, note="") -> ProductReturn:
    """REJECTED: no stock change, decision recorded."""
    if ret.status != ProductReturn.Status.PENDING:
        raise ValidationError("Only pending returns can be rejected.")
    from django.utils import timezone

    ret.status = ProductReturn.Status.REJECTED
    ret.decided_by = user
    ret.decided_at = timezone.now()
    ret.decision_note = (note or "").strip()
    ret.save(
        update_fields=[
            "status", "decided_by", "decided_at", "decision_note",
        ]
    )
    return ret


# ---------------------------------------------------------------------
# Stock adjustments (owner/manager, never edited afterwards)
# ---------------------------------------------------------------------

# Manual adjustments may be recorded with these reasons. SHORTAGE is
# reserved for automatic transfer-receipt write-offs.
MANUAL_ADJUSTMENT_REASONS = (
    StockAdjustment.Reason.DAMAGED,
    StockAdjustment.Reason.MISSING,
    StockAdjustment.Reason.CORRECTION,
    StockAdjustment.Reason.OTHER,
)


@transaction.atomic
def create_adjustment(*, shop, variant, quantity, reason, note,
                      direction=None, user=None) -> StockAdjustment:
    """Record a manual adjustment + its ledger row (kind=ADJUSTMENT).

    - DAMAGED / MISSING / OTHER always remove stock (direction -1).
    - CORRECTION requires an explicit direction (+1 or -1) — this is
      the owner's authorized correction of an inventory record.
    The row is immutable afterwards.
    """
    if quantity is None or quantity <= 0:
        raise ValidationError("Adjustment quantity must be greater than zero.")
    if not (note or "").strip():
        raise ValidationError(
            "An adjustment reason (note) is required."
        )
    if reason not in MANUAL_ADJUSTMENT_REASONS:
        raise ValidationError(
            "Manual adjustments must be DAMAGED, MISSING, CORRECTION or OTHER."
        )
    _validate_location(shop)

    if reason == StockAdjustment.Reason.CORRECTION:
        if direction not in (1, -1):
            raise ValidationError(
                "A correction must state its direction: increase (+1) "
                "or decrease (-1)."
            )
    else:
        direction = -1  # damage/missing/other only ever reduce stock

    adj = StockAdjustment.objects.create(
        reference=next_adjustment_code(),
        shop=shop,
        variant=variant,
        quantity=quantity,
        direction=direction,
        reason=reason,
        note=note.strip(),
        created_by=user,
    )
    apply_movement(
        transaction_id=adj.reference,
        kind=StockMovement.Kind.ADJUSTMENT,
        shop=shop,
        variant=variant,
        quantity=quantity,
        direction=direction,
        user=user,
    )
    return adj
