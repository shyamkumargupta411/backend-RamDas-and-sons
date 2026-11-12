"""Catalog helpers: product codes, variant spec keys, unit conversion."""

from decimal import Decimal, InvalidOperation

from django.db import transaction

from .models import Product

ZERO = Decimal("0")

# Base units per dimension type for conversion.
# WEIGHT base = KG, LENGTH base = FEET.
_WEIGHT_FACTORS = {
    "KG": Decimal("1"),
    "G": Decimal("0.001"),
    "MT": Decimal("1000"),
}
_LENGTH_FACTORS = {
    "FEET": Decimal("1"),
    "FT": Decimal("1"),
    "METER": Decimal("3.280839895"),
    "M": Decimal("3.280839895"),
    "INCH": Decimal("0.0833333333"),
    "IN": Decimal("0.0833333333"),
}


def _dec(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _fmt(value):
    """Normalize a Decimal for stable spec-key strings (no scientific notes)."""
    d = _dec(value)
    if d is None:
        return ""
    normalized = d.normalize()
    text = format(normalized, "f")
    if text in ("", "-"):
        return ""
    # normalize() can yield e.g. "1E+1" -> format f fixes to "10"; strip -0
    if text == "-0":
        return "0"
    return text


def make_spec_key(thickness, width, length, diameter, weight, unit_code):
    """Build a unique-per-product specification fingerprint.

    Empty dimensions become '' so None and 0 are distinguished, and
    different size sets never collide.
    """
    parts = [
        _fmt(thickness),
        _fmt(width),
        _fmt(length),
        _fmt(diameter),
        _fmt(weight),
        (unit_code or "").strip().upper(),
    ]
    return "|".join(parts)


def next_product_code():
    """Allocate the next PROD-0001 style code atomically."""
    with transaction.atomic():
        last = (
            Product.objects.select_for_update()
            .order_by("-id")
            .values_list("code", flat=True)
            .first()
        )
        next_n = 1
        if last:
            try:
                next_n = int(str(last).rsplit("-", 1)[-1]) + 1
            except (ValueError, IndexError):
                # Fall back to scanning if an unexpected code exists.
                next_n = 1
                for code in Product.objects.values_list("code", flat=True):
                    try:
                        n = int(str(code).rsplit("-", 1)[-1])
                        next_n = max(next_n, n + 1)
                    except (ValueError, IndexError):
                        continue
        return f"PROD-{next_n:04d}"


def convert_quantity(variant, quantity, from_unit_code, to_unit_code):
    """Convert ``quantity`` between units for a given variant.

    Rules:
      * Same unit -> unchanged.
      * WEIGHT <-> WEIGHT and LENGTH <-> LENGTH use fixed factors.
      * PIECE -> WEIGHT uses ``variant.weight_per_piece``.
      * PIECE -> LENGTH uses ``variant.length_per_piece``.
      * WEIGHT/LENGTH -> PIECE divide by the matching per-piece factor.
      * Anything else raises ValueError.
    """
    qty = _dec(quantity)
    if qty is None:
        raise ValueError("quantity must be numeric")
    src = (from_unit_code or "").strip().upper()
    dst = (to_unit_code or "").strip().upper()
    if not src or not dst:
        raise ValueError("unit codes are required")
    if src == dst:
        return qty

    from .models import Unit

    try:
        src_unit = Unit.objects.get(code__iexact=src)
        dst_unit = Unit.objects.get(code__iexact=dst)
    except Unit.DoesNotExist as exc:
        raise ValueError(f"unknown unit in {exc}") from exc

    # Direct factor conversion within the same dimension type.
    if src_unit.unit_type == dst_unit.unit_type == Unit.UnitType.WEIGHT:
        return qty * _WEIGHT_FACTORS[src] / _WEIGHT_FACTORS[dst]
    if src_unit.unit_type == dst_unit.unit_type == Unit.UnitType.LENGTH:
        return qty * _LENGTH_FACTORS[src] / _LENGTH_FACTORS[dst]

    # Piece <-> WEIGHT via weight_per_piece.
    if {
        src_unit.unit_type,
        dst_unit.unit_type,
    } == {Unit.UnitType.COUNT, Unit.UnitType.WEIGHT}:
        factor = _dec(variant.weight_per_piece) if variant else None
        if not factor or factor == ZERO:
            raise ValueError(
                "variant has no weight_per_piece for PIECE<->WEIGHT conversion"
            )
        if src_unit.unit_type == Unit.UnitType.COUNT:
            return qty * factor
        return qty / factor

    # Piece <-> LENGTH via length_per_piece.
    if {
        src_unit.unit_type,
        dst_unit.unit_type,
    } == {Unit.UnitType.COUNT, Unit.UnitType.LENGTH}:
        factor = _dec(variant.length_per_piece) if variant else None
        if not factor or factor == ZERO:
            raise ValueError(
                "variant has no length_per_piece for PIECE<->FEET conversion"
            )
        if src_unit.unit_type == Unit.UnitType.COUNT:
            return qty * factor
        return qty / factor

    raise ValueError(
        f"cannot convert {src_unit.unit_type} to {dst_unit.unit_type} "
        "without a shared base"
    )
