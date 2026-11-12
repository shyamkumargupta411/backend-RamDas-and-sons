from io import StringIO

from django.core.management.base import BaseCommand
from django.db.models import Sum

from accounts.models import Shop
from inventory.models import StockLevel, StockMovement


class Command(BaseCommand):
    help = "Compare StockLevel balances to StockMovement ledger sums and report discrepancies."

    def add_arguments(self, parser):
        parser.add_argument("--shop", type=int, help="Reconcile only this shop ID.")
        parser.add_argument(
            "--fix",
            action="store_true",
            help="Do not fix levels; report only.",
        )

    def handle(self, *args, **options):
        shop_id = options.get("shop")
        shops = Shop.objects.filter(is_deleted=False, is_active=True).exclude(
            shop_type=Shop.ShopType.IN_TRANSIT
        )
        if shop_id:
            shops = shops.filter(id=shop_id)

        discrepancies = []
        checked = 0

        for shop in shops:
            levels = StockLevel.objects.filter(shop=shop)
            for lvl in levels.select_related(
                "variant__product", "variant__unit"
            ):
                checked += 1
                pos = (
                    StockMovement.objects.filter(
                        shop=shop, variant=lvl.variant, direction=1
                    ).aggregate(total=Sum("quantity"))["total"]
                    or 0
                )
                neg = (
                    StockMovement.objects.filter(
                        shop=shop, variant=lvl.variant, direction=-1
                    ).aggregate(total=Sum("quantity"))["total"]
                    or 0
                )
                ledger_qty = pos - neg
                balance_qty = lvl.quantity
                discrepancy = balance_qty - ledger_qty
                if discrepancy != 0:
                    discrepancies.append({
                        "shop": shop.code,
                        "product": lvl.variant.product.code,
                        "spec": lvl.variant.spec_key,
                        "unit": lvl.variant.unit.code,
                        "balance": balance_qty,
                        "ledger": ledger_qty,
                        "discrepancy": discrepancy,
                    })

        out = StringIO()
        out.write(f"Checked {checked} stock levels across {shops.count()} shop(s).\n")
        if discrepancies:
            out.write(
                self.style.WARNING(
                    f"Found {len(discrepancies)} discrepancy(ies):\n"
                )
            )
            for d in discrepancies:
                out.write(
                    f"  {d['shop']} | {d['product']} [{d['spec']}] | "
                    f"balance={d['balance']} ledger={d['ledger']} diff={d['discrepancy']}\n"
                )
        else:
            out.write(
                self.style.SUCCESS(
                    "No discrepancies found — balances match ledger sums.\n"
                )
            )
        return out.getvalue()