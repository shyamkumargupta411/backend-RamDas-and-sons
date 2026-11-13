import io
import re
from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.http import HttpResponse
from openpyxl import load_workbook
from openpyxl.styles import Font, Alignment, PatternFill
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import Shop, User
from accounts.permissions import IsOwnerOrManager
from catalog.models import ProductCategory, Product, ProductVariant, Unit
from inventory.models import (
    ProductReturn,
    StockAdjustment,
    StockLevel,
    StockMovement,
    StockReceipt,
    StockReceiptLine,
    StockTransfer,
    StockTransferItem,
)
from inventory.services import apply_movement
from sales.models import Sale, SaleItem

from .models import LowStockThreshold
from .serializers import (
    DashboardQuerySerializer,
    LowStockThresholdSerializer,
    ReportQuerySerializer,
)


def _parse_date(value):
    if isinstance(value, date):
        return value
    return None


def _filter_by_date(qs, date_field, date_from, date_to):
    if date_from:
        from datetime import datetime
        from django.utils import timezone

        start = timezone.make_aware(datetime.combine(date_from, datetime.min.time()))
        qs = qs.filter(**{f"{date_field}__gte": start})
    if date_to:
        from datetime import datetime
        from django.utils import timezone

        end = timezone.make_aware(datetime.combine(date_to, datetime.max.time()))
        qs = qs.filter(**{f"{date_field}__lte": end})
    return qs


def _user_shop_queryset(user):
    """Shops visible to the user based on role."""
    if user.is_owner:
        return Shop.objects.filter(is_deleted=False, is_active=True)
    if user.is_manager:
        assigned = user.shop_assignments.filter(is_active=True).values_list(
            "shop_id", flat=True
        )
        return Shop.objects.filter(
            id__in=assigned, is_deleted=False, is_active=True
        )
    if user.is_seller:
        return user.active_shops()
    return Shop.objects.none()


def _variant_filter(qs, variant_id, product_id):
    if variant_id:
        qs = qs.filter(variant_id=variant_id)
    if product_id:
        qs = qs.filter(variant__product_id=product_id)
    return qs


class DashboardView(APIView):
    """Aggregate dashboard with 10 sections.

    Sections:
      1. total_inventory
      2. warehouse_stock
      3. shop_stock
      4. today_sales
      5. employee_sales
      6. product_sales
      7. recent_movements
      8. low_stock_alerts
      9. damaged_returned_stock
      10. stock_discrepancy
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        serializer = DashboardQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        f = serializer.validated_data

        date_from = f.get("date_from")
        date_to = f.get("date_to")
        shop_id = f.get("shop")
        product_id = f.get("product")
        employee_id = f.get("employee")

        user = request.user
        shops = _user_shop_queryset(user)
        if shop_id:
            shops = shops.filter(id=shop_id)

        # Exclude in-transit from stock views.
        shops = shops.exclude(shop_type=Shop.ShopType.IN_TRANSIT)

        warehouse = shops.filter(shop_type=Shop.ShopType.WAREHOUSE).first()

        # ------------------------------------------------------------------
        # 1. Total inventory
        # ------------------------------------------------------------------
        levels = StockLevel.objects.filter(shop__in=shops)
        levels = _variant_filter(levels, None, product_id)
        total_quantity = levels.aggregate(total=Sum("quantity"))["total"] or 0
        total_products = (
            ProductVariant.objects.filter(
                id__in=levels.values_list("variant_id", flat=True)
            )
            .values_list("product_id", flat=True)
            .distinct()
            .count()
        )
        total_variants = levels.values_list("variant_id", flat=True).distinct().count()

        # ------------------------------------------------------------------
        # 2. Warehouse stock
        # ------------------------------------------------------------------
        warehouse_items = []
        if warehouse:
            wh_levels = StockLevel.objects.filter(shop=warehouse)
            wh_levels = _variant_filter(wh_levels, None, product_id)
            for lvl in wh_levels.select_related(
                "variant__product", "variant__unit"
            ):
                warehouse_items.append({
                    "variant_id": lvl.variant_id,
                    "product_code": lvl.variant.product.code,
                    "product_name": lvl.variant.product.name,
                    "spec_label": lvl.variant.spec_key,
                    "unit_code": lvl.variant.unit.code,
                    "quantity": lvl.quantity,
                })
        warehouse_total = sum(i["quantity"] for i in warehouse_items)

        # ------------------------------------------------------------------
        # 3. Individual shop stock
        # ------------------------------------------------------------------
        shop_stock = []
        for shop in shops:
            lvl_qs = StockLevel.objects.filter(shop=shop)
            lvl_qs = _variant_filter(lvl_qs, None, product_id)
            items = []
            for lvl in lvl_qs.select_related(
                "variant__product", "variant__unit"
            ):
                items.append({
                    "variant_id": lvl.variant_id,
                    "product_code": lvl.variant.product.code,
                    "product_name": lvl.variant.product.name,
                    "spec_label": lvl.variant.spec_key,
                    "unit_code": lvl.variant.unit.code,
                    "quantity": lvl.quantity,
                })
            shop_stock.append({
                "shop_id": shop.id,
                "shop_name": shop.name,
                "shop_code": shop.code,
                "shop_type": shop.shop_type,
                "items": items,
                "total_quantity": sum(i["quantity"] for i in items),
            })

        # ------------------------------------------------------------------
        # 4. Today's sales quantity
        # ------------------------------------------------------------------
        today = date.today()
        sales_qs = Sale.objects.filter(shop__in=shops, sold_at__date=today)
        if employee_id:
            sales_qs = sales_qs.filter(employee_id=employee_id)
        today_sales = sales_qs.aggregate(
            count=Sum("id", output_field=None),
            total_qty=Sum("items__quantity"),
        )
        today_sales_count = sales_qs.count()
        today_sales_qty = (
            SaleItem.objects.filter(sale__in=sales_qs).aggregate(
                total=Sum("quantity")
            )["total"]
            or 0
        )

        # ------------------------------------------------------------------
        # 5. Employee-wise sales
        # ------------------------------------------------------------------
        employee_sales_qs = Sale.objects.filter(shop__in=shops)
        employee_sales_qs = _filter_by_date(
            employee_sales_qs, "sold_at", date_from, date_to
        )
        if employee_id:
            employee_sales_qs = employee_sales_qs.filter(employee_id=employee_id)
        employee_sales = []
        for emp in User.objects.filter(
            id__in=employee_sales_qs.values_list("employee_id", flat=True).distinct()
        ):
            emp_sales = employee_sales_qs.filter(employee=emp)
            qty = (
                SaleItem.objects.filter(sale__in=emp_sales).aggregate(
                    total=Sum("quantity")
                )["total"]
                or 0
            )
            employee_sales.append({
                "employee_id": emp.id,
                "employee_name": emp.get_full_name() or emp.username,
                "employee_role": emp.role,
                "sale_count": emp_sales.count(),
                "total_quantity": qty,
            })
        employee_sales.sort(key=lambda x: x["total_quantity"], reverse=True)

        # ------------------------------------------------------------------
        # 6. Product-wise sales
        # ------------------------------------------------------------------
        product_sales_qs = SaleItem.objects.filter(sale__shop__in=shops)
        product_sales_qs = _filter_by_date(
            product_sales_qs, "sale__sold_at", date_from, date_to
        )
        product_sales_qs = _variant_filter(
            product_sales_qs, None, product_id
        )
        if employee_id:
            product_sales_qs = product_sales_qs.filter(sale__employee_id=employee_id)
        product_sales = []
        for item in product_sales_qs.select_related(
            "variant__product", "variant__unit"
        ):
            product_sales.append({
                "product_code": item.variant.product.code,
                "product_name": item.variant.product.name,
                "spec_label": item.variant.spec_key,
                "unit_code": item.variant.unit.code,
                "quantity": item.quantity,
                "sale_reference": item.sale.reference,
                "sold_at": item.sale.sold_at,
            })

        # ------------------------------------------------------------------
        # 7. Recent stock movements
        # ------------------------------------------------------------------
        movements_qs = StockMovement.objects.filter(shop__in=shops)
        movements_qs = _filter_by_date(
            movements_qs, "created_at", date_from, date_to
        )
        movements_qs = _variant_filter(movements_qs, None, product_id)
        if employee_id:
            movements_qs = movements_qs.filter(created_by_id=employee_id)
        recent_movements = []
        for m in movements_qs.select_related(
            "variant__product", "variant__unit", "created_by", "shop"
        ).order_by("-created_at")[:100]:
            recent_movements.append({
                "transaction_id": m.transaction_id,
                "kind": m.kind,
                "shop_code": m.shop.code,
                "shop_name": m.shop.name,
                "variant_id": m.variant_id,
                "product_code": m.variant.product.code,
                "spec_label": m.variant.spec_key,
                "unit_code": m.variant.unit.code,
                "quantity": m.quantity,
                "direction": m.direction,
                "user": m.created_by.username if m.created_by else None,
                "created_at": m.created_at,
            })

        # ------------------------------------------------------------------
        # 8. Low-stock alerts
        # ------------------------------------------------------------------
        thresholds = LowStockThreshold.objects.filter(
            is_active=True, shop__in=shops
        ).select_related("variant__product", "variant__unit", "shop")
        low_stock_alerts = []
        for th in thresholds:
            level = StockLevel.objects.filter(
                shop=th.shop, variant=th.variant
            ).first()
            current = level.quantity if level else 0
            if current < th.threshold_quantity:
                low_stock_alerts.append({
                    "shop_id": th.shop.id,
                    "shop_name": th.shop.name,
                    "variant_id": th.variant_id,
                    "product_code": th.variant.product.code,
                    "spec_label": th.variant.spec_key,
                    "unit_code": th.variant.unit.code,
                    "current_quantity": current,
                    "threshold_quantity": th.threshold_quantity,
                    "shortfall": th.threshold_quantity - current,
                })

        # ------------------------------------------------------------------
        # 9. Damaged and returned stock
        # ------------------------------------------------------------------
        adjustments_qs = StockAdjustment.objects.filter(
            shop__in=shops,
            reason__in=[
                StockAdjustment.Reason.DAMAGED,
                StockAdjustment.Reason.MISSING,
                StockAdjustment.Reason.OTHER,
            ],
        )
        adjustments_qs = _filter_by_date(
            adjustments_qs, "created_at", date_from, date_to
        )
        adjustments_qs = _variant_filter(adjustments_qs, None, product_id)
        damaged_qty = 0
        damaged_items = []
        for adj in adjustments_qs.select_related(
            "variant__product", "variant__unit", "shop"
        ):
            if adj.direction < 0:
                damaged_qty += adj.quantity
                damaged_items.append({
                    "reference": adj.reference,
                    "shop_code": adj.shop.code,
                    "variant_id": adj.variant_id,
                    "spec_label": adj.variant.spec_key,
                    "unit_code": adj.variant.unit.code,
                    "quantity": adj.quantity,
                    "reason": adj.reason,
                    "note": adj.note,
                    "created_at": adj.created_at,
                })

        returns_qs = ProductReturn.objects.filter(
            shop__in=shops, status=ProductReturn.Status.ACCEPTED
        )
        returns_qs = _filter_by_date(
            returns_qs, "decided_at", date_from, date_to
        )
        returns_qs = _variant_filter(returns_qs, None, product_id)
        returned_qty = 0
        returned_items = []
        for ret in returns_qs.select_related(
            "variant__product", "variant__unit", "shop"
        ):
            returned_qty += ret.quantity
            returned_items.append({
                "reference": ret.reference,
                "shop_code": ret.shop.code,
                "variant_id": ret.variant_id,
                "spec_label": ret.variant.spec_key,
                "unit_code": ret.variant.unit.code,
                "quantity": ret.quantity,
                "reason": ret.reason,
                "accepted_at": ret.accepted_at,
            })

        # ------------------------------------------------------------------
        # 10. Stock discrepancy report
        # ------------------------------------------------------------------
        stock_discrepancy = []
        for shop in shops:
            shop_levels = StockLevel.objects.filter(shop=shop)
            shop_levels = _variant_filter(shop_levels, None, product_id)
            for lvl in shop_levels.select_related(
                "variant__product", "variant__unit"
            ):
                # Compute signed sum from movements
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
                    stock_discrepancy.append({
                        "shop_id": shop.id,
                        "shop_name": shop.name,
                        "shop_code": shop.code,
                        "variant_id": lvl.variant_id,
                        "product_code": lvl.variant.product.code,
                        "spec_label": lvl.variant.spec_key,
                        "unit_code": lvl.variant.unit.code,
                        "balance_quantity": balance_qty,
                        "ledger_quantity": ledger_qty,
                        "discrepancy": discrepancy,
                    })

        return Response({
            "total_inventory": {
                "total_products": total_products,
                "total_variants": total_variants,
                "total_quantity": total_quantity,
            },
            "warehouse_stock": {
                "warehouse_id": warehouse.id if warehouse else None,
                "warehouse_name": warehouse.name if warehouse else None,
                "total_quantity": warehouse_total,
                "items": warehouse_items,
            },
            "shop_stock": shop_stock,
            "today_sales": {
                "date": today.isoformat(),
                "sale_count": today_sales_count,
                "total_quantity": today_sales_qty,
            },
            "employee_sales": employee_sales,
            "product_sales": product_sales,
            "recent_movements": recent_movements,
            "low_stock_alerts": low_stock_alerts,
            "damaged_returned_stock": {
                "damaged_quantity": damaged_qty,
                "returned_quantity": returned_qty,
                "damaged_items": damaged_items,
                "returned_items": returned_items,
            },
            "stock_discrepancy": stock_discrepancy,
        })


class ReportView(APIView):
    """Product-wise, shop-wise, employee-wise, and stock-movement reports."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        serializer = ReportQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        f = serializer.validated_data

        date_from = f.get("date_from")
        date_to = f.get("date_to")
        shop_id = f.get("shop")
        product_id = f.get("product")
        employee_id = f.get("employee")
        report_type = f.get("report_type", "product-wise")

        user = request.user
        shops = _user_shop_queryset(user)
        if shop_id:
            shops = shops.filter(id=shop_id)
        shops = shops.exclude(shop_type=Shop.ShopType.IN_TRANSIT)

        def dfilter(qs, field):
            return _filter_by_date(qs, field, date_from, date_to)

        if report_type == "product-wise":
            qs = SaleItem.objects.filter(sale__shop__in=shops)
            qs = dfilter(qs, "sale__sold_at")
            qs = _variant_filter(qs, None, product_id)
            if employee_id:
                qs = qs.filter(sale__employee_id=employee_id)
            results = []
            from collections import defaultdict
            agg = defaultdict(lambda: {"quantity": 0, "sales": 0})
            for item in qs.select_related("variant__product", "variant__unit"):
                key = (item.variant_id, item.variant.product.code, item.variant.spec_key, item.variant.unit.code)
                agg[key]["quantity"] += item.quantity
                agg[key]["sales"] += 1
            for (variant_id, product_code, spec_label, unit_code), vals in agg.items():
                results.append({
                    "variant_id": variant_id,
                    "product_code": product_code,
                    "spec_label": spec_label,
                    "unit_code": unit_code,
                    "total_quantity": vals["quantity"],
                    "sale_count": vals["sales"],
                })
            results.sort(key=lambda x: x["total_quantity"], reverse=True)

        elif report_type == "shop-wise":
            results = []
            for shop in shops:
                items = SaleItem.objects.filter(sale__shop=shop)
                items = dfilter(items, "sale__sold_at")
                if product_id:
                    items = items.filter(variant_id=product_id)
                if employee_id:
                    items = items.filter(sale__employee_id=employee_id)
                total_qty = items.aggregate(total=Sum("quantity"))["total"] or 0
                results.append({
                    "shop_id": shop.id,
                    "shop_name": shop.name,
                    "shop_code": shop.code,
                    "total_quantity": total_qty,
                    "sale_count": items.count(),
                })
            results.sort(key=lambda x: x["total_quantity"], reverse=True)

        elif report_type == "employee-wise":
            results = []
            qs = Sale.objects.filter(shop__in=shops)
            qs = dfilter(qs, "sold_at")
            if employee_id:
                qs = qs.filter(employee_id=employee_id)
            if product_id:
                qs = qs.filter(items__variant_id=product_id).distinct()
            for emp in User.objects.filter(
                id__in=qs.values_list("employee_id", flat=True).distinct()
            ):
                emp_sales = qs.filter(employee=emp)
                total_qty = (
                    SaleItem.objects.filter(sale__in=emp_sales).aggregate(
                        total=Sum("quantity")
                    )["total"]
                    or 0
                )
                results.append({
                    "employee_id": emp.id,
                    "employee_name": emp.get_full_name() or emp.username,
                    "employee_role": emp.role,
                    "total_quantity": total_qty,
                    "sale_count": emp_sales.count(),
                })
            results.sort(key=lambda x: x["total_quantity"], reverse=True)

        elif report_type == "stock-movement":
            qs = StockMovement.objects.filter(shop__in=shops)
            qs = dfilter(qs, "created_at")
            qs = _variant_filter(qs, None, product_id)
            if employee_id:
                qs = qs.filter(created_by_id=employee_id)
            results = []
            for m in qs.select_related(
                "variant__product", "variant__unit", "shop", "created_by"
            ).order_by("-created_at"):
                results.append({
                    "transaction_id": m.transaction_id,
                    "kind": m.kind,
                    "shop_code": m.shop.code,
                    "variant_id": m.variant_id,
                    "product_code": m.variant.product.code,
                    "spec_label": m.variant.spec_key,
                    "unit_code": m.variant.unit.code,
                    "quantity": m.quantity,
                    "direction": m.direction,
                    "user": m.created_by.username if m.created_by else None,
                    "created_at": m.created_at,
                })

        return Response({
            "report_type": report_type,
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
            "count": len(results),
            "results": results,
        })


class ReportExcelView(APIView):
    """Export a report to Excel (.xlsx)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        serializer = ReportQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        f = serializer.validated_data

        date_from = f.get("date_from")
        date_to = f.get("date_to")
        shop_id = f.get("shop")
        product_id = f.get("product")
        employee_id = f.get("employee")
        report_type = f.get("report_type", "product-wise")

        user = request.user
        shops = _user_shop_queryset(user)
        if shop_id:
            shops = shops.filter(id=shop_id)
        shops = shops.exclude(shop_type=Shop.ShopType.IN_TRANSIT)

        def dfilter(qs, field):
            return _filter_by_date(qs, field, date_from, date_to)

        wb = Workbook()
        ws = wb.active
        ws.title = report_type

        header_fill = PatternFill(start_color="1F3864", end_color="1F3864", fill_type="solid")
        header_font = Font(color="FFFFFF", bold=True)

        if report_type == "product-wise":
            headers = ["Product Code", "Spec Label", "Unit", "Total Quantity", "Sale Count"]
            ws.append(headers)
            for c in ws[1]:
                c.fill = header_fill
                c.font = header_font
            qs = SaleItem.objects.filter(sale__shop__in=shops)
            qs = dfilter(qs, "sale__sold_at")
            qs = _variant_filter(qs, None, product_id)
            if employee_id:
                qs = qs.filter(sale__employee_id=employee_id)
            from collections import defaultdict
            agg = defaultdict(lambda: {"quantity": 0, "sales": 0})
            for item in qs.select_related("variant__product", "variant__unit"):
                key = item.variant_id
                agg[key]["quantity"] += item.quantity
                agg[key]["sales"] += 1
                agg[key]["_label"] = (
                    item.variant.product.code,
                    item.variant.spec_key,
                    item.variant.unit.code,
                )
            for vid, vals in agg.items():
                pc, sl, uc = vals["_label"]
                ws.append([pc, sl, uc, vals["quantity"], vals["sales"]])

        elif report_type == "shop-wise":
            headers = ["Shop Code", "Shop Name", "Total Quantity", "Sale Count"]
            ws.append(headers)
            for c in ws[1]:
                c.fill = header_fill
                c.font = header_font
            for shop in shops:
                items = SaleItem.objects.filter(sale__shop=shop)
                items = dfilter(items, "sale__sold_at")
                if product_id:
                    items = items.filter(variant_id=product_id)
                if employee_id:
                    items = items.filter(sale__employee_id=employee_id)
                total_qty = items.aggregate(total=Sum("quantity"))["total"] or 0
                ws.append([shop.code, shop.name, total_qty, items.count()])

        elif report_type == "employee-wise":
            headers = ["Employee", "Role", "Total Quantity", "Sale Count"]
            ws.append(headers)
            for c in ws[1]:
                c.fill = header_fill
                c.font = header_font
            qs = Sale.objects.filter(shop__in=shops)
            qs = dfilter(qs, "sold_at")
            if employee_id:
                qs = qs.filter(employee_id=employee_id)
            if product_id:
                qs = qs.filter(items__variant_id=product_id).distinct()
            for emp in User.objects.filter(
                id__in=qs.values_list("employee_id", flat=True).distinct()
            ):
                emp_sales = qs.filter(employee=emp)
                total_qty = (
                    SaleItem.objects.filter(sale__in=emp_sales).aggregate(
                        total=Sum("quantity")
                    )["total"]
                    or 0
                )
                ws.append([emp.get_full_name() or emp.username, emp.role, total_qty, emp_sales.count()])

        elif report_type == "stock-movement":
            headers = ["Transaction ID", "Kind", "Shop", "Product", "Spec", "Unit", "Quantity", "Direction", "User", "Created At"]
            ws.append(headers)
            for c in ws[1]:
                c.fill = header_fill
                c.font = header_font
            qs = StockMovement.objects.filter(shop__in=shops)
            qs = dfilter(qs, "created_at")
            qs = _variant_filter(qs, None, product_id)
            if employee_id:
                qs = qs.filter(created_by_id=employee_id)
            for m in qs.select_related(
                "variant__product", "variant__unit", "shop", "created_by"
            ).order_by("-created_at"):
                ws.append([
                    m.transaction_id,
                    m.kind,
                    m.shop.code,
                    m.variant.product.code,
                    m.variant.spec_key,
                    m.variant.unit.code,
                    m.quantity,
                    m.direction,
                    m.created_by.username if m.created_by else "",
                    m.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                ])

        # Auto-size columns
        for col in ws.columns:
            max_len = 0
            col_letter = col[0].column_letter
            for cell in col:
                try:
                    v = str(cell.value) if cell.value is not None else ""
                    if len(v) > max_len:
                        max_len = len(v)
                except Exception:
                    pass
            ws.column_dimensions[col_letter].width = min(max(max_len + 2, 8), 40)

        buffer = io.BytesIO()
        wb.save(buffer)
        buffer.seek(0)

        filename = f"report_{report_type}_{date.today().isoformat()}.xlsx"
        response = HttpResponse(
            buffer.read(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


class LowStockThresholdViewSet(APIView):
    """CRUD for low-stock thresholds (owner/manager)."""

    permission_classes = [IsOwnerOrManager]

    def get(self, request):
        user = request.user
        shops = _user_shop_queryset(user)
        qs = LowStockThreshold.objects.filter(shop__in=shops, is_active=True)
        shop_id = request.query_params.get("shop")
        if shop_id:
            qs = qs.filter(shop_id=shop_id)
        data = LowStockThresholdSerializer(qs, many=True).data
        return Response(data)

    def post(self, request):
        ser = LowStockThresholdSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        ser.save(created_by=request.user, updated_by=request.user)
        return Response(ser.data, status=status.HTTP_201_CREATED)

    def put(self, request, pk=None):
        user = request.user
        shops = _user_shop_queryset(user)
        try:
            th = LowStockThreshold.objects.get(pk=pk, shop__in=shops)
        except LowStockThreshold.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        ser = LowStockThresholdSerializer(th, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save(updated_by=request.user)
        return Response(ser.data)

    def delete(self, request, pk=None):
        user = request.user
        shops = _user_shop_queryset(user)
        try:
            th = LowStockThreshold.objects.get(pk=pk, shop__in=shops)
        except LowStockThreshold.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        th.is_active = False
        th.updated_by = request.user
        th.save()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Bulk material upload (Excel)
# ---------------------------------------------------------------------------

import re  # noqa: E402


def _parse_material_name(raw_name, default_unit="KG"):
    """
    Parse a raw material string like:
      'GI SATTER PATTI (HAVY) 14 FUT'
      '3/4"GRIL PATTI(LIGHT)'
      '40*10 PATIYA'
      '1" ANGLE (LIGHT)'
      'HR SHEET 50KG (BOKARO)'
    Returns (product_name, variant_spec, unit_code).
    Best-effort; frontend lets user edit before confirm.
    """
    if not raw_name:
        raise ValueError("Empty material name")
    s = str(raw_name).strip()
    if not s:
        raise ValueError("Empty material name")

    unit_code = default_unit
    measurement = ""

    # Try trailing measurement: number + unit word at end
    # Examples: 14 FUT, 2.5 KG, 3/4", 40*10, 20MM, 5.6KG
    trail = re.search(
        r"(.+?)\s+([\d./" + r"\"”’" + r"]+(?:\s*[\d./" + r"\"”’" + r"]+)*\s*"
        r"(?:FUT|FEET|FT|INCH|INCHES|KG|KGS|MM|CM|MTR|METER|METERS|GOL|PILA|HARA|NILA|SQ|SFT))\s*$",
        s,
        re.IGNORECASE,
    )
    if trail:
        s = trail.group(1).strip()
        measurement = trail.group(2).strip()
        ml = measurement.lower()
        if "kg" in ml:
            unit_code = "KG"
        elif any(u in ml for u in ("fut", "feet", "ft")):
            unit_code = "FEET"
        elif "inch" in ml:
            unit_code = "INCH"
        elif "mm" in ml:
            unit_code = "MM"
        elif "cm" in ml:
            unit_code = "CM"
        elif any(u in ml for u in ("mtr", "meter", "meters")):
            unit_code = "FEET"

    # Extract parenthetical content
    paren = ""
    pm = re.search(r"\(([^)]+)\)", s)
    if pm:
        paren = pm.group(1).strip()
        s = s.replace(pm.group(0), "").strip()

    product_name = s.strip()

    # Build variant spec from parenthetical + measurement
    variant_parts = [p for p in [paren, measurement] if p]
    variant_spec = " ".join(variant_parts) if variant_parts else product_name

    return product_name, variant_spec, unit_code


def _get_or_create_unit(code, name=None, unit_type="COUNT"):
    code = (code or "KG").strip().upper()
    name = name or code
    unit_type = unit_type or "COUNT"
    unit, _ = Unit.objects.get_or_create(
        code=code,
        defaults={"name": name, "unit_type": unit_type, "is_active": True},
    )
    return unit


def _get_or_create_product(name, material, user):
    name = name.strip()
    category, _ = ProductCategory.objects.get_or_create(
        code="other-metal",
        defaults={
            "name": "Other Metal",
            "form_factor": ProductCategory.FormFactor.OTHER,
            "is_active": True,
        },
    )
    product, _ = Product.objects.get_or_create(
        code=name.upper().replace(" ", "-")[:20],
        defaults={
            "name": name,
            "category": category,
            "material": material or "MS",
            "grade": "",
            "status": Product.Status.ACTIVE,
            "created_by": user,
        },
    )
    return product


def _get_or_create_variant(product, spec_key, unit, user):
    variant, _ = ProductVariant.objects.get_or_create(
        product=product,
        spec_key=spec_key,
        defaults={
            "unit": unit,
            "thickness": None,
            "width": None,
            "length": None,
            "diameter": None,
            "weight": None,
            "weight_per_piece": None,
            "length_per_piece": None,
            "is_active": True,
            "created_by": user,
        },
    )
    return variant


class BulkUploadPreviewView(APIView):
    """POST Excel file -> return parsed rows with suggested product/variant/unit.
    Frontend can edit before confirming.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        file = request.FILES.get("file")
        if not file:
            return Response({"error": "No file provided."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            wb = openpyxl.load_workbook(io.BytesIO(file.read()), data_only=True)
        except Exception as exc:
            return Response({"error": f"Invalid Excel file: {exc}"}, status=status.HTTP_400_BAD_REQUEST)

        ws = wb.active
        rows = []
        for idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            if not any(v is not None and str(v).strip() != "" for v in row):
                continue
            raw_name = str(row[0]).strip() if row[0] is not None else ""
            if not raw_name:
                continue
            qty = row[1] if len(row) > 1 and row[1] is not None else 0
            try:
                qty = float(qty)
            except (TypeError, ValueError):
                qty = 0.0
            unit_code = str(row[2]).strip().upper() if len(row) > 2 and row[2] else "KG"
            shop_id = row[3] if len(row) > 3 and row[3] else None

            try:
                pname, vspec, ucode = _parse_material_name(raw_name, unit_code)
            except ValueError as exc:
                rows.append(
                    {
                        "row": idx,
                        "raw_name": raw_name,
                        "error": str(exc),
                        "product_name": raw_name,
                        "variant_spec": raw_name,
                        "quantity": qty,
                        "unit_code": "KG",
                        "shop_id": shop_id,
                    }
                )
                continue

            rows.append(
                {
                    "row": idx,
                    "raw_name": raw_name,
                    "product_name": pname,
                    "variant_spec": vspec,
                    "quantity": qty,
                    "unit_code": ucode,
                    "shop_id": shop_id,
                }
            )

        return Response({"count": len(rows), "rows": rows}, status=status.HTTP_200_OK)


class BulkUploadConfirmView(APIView):
    """POST confirmed rows -> create products/variants/stock levels + ledger."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        rows = request.data.get("rows", [])
        shop_id = request.data.get("shop_id")
        user = request.user

        if not rows:
            return Response({"error": "No rows to confirm."}, status=status.HTTP_400_BAD_REQUEST)

        # Resolve shop
        shop = None
        if shop_id:
            try:
                shop = Shop.objects.get(pk=shop_id, is_deleted=False)
            except Shop.DoesNotExist:
                shop = None
        if not shop:
            shop = (
                Shop.objects.filter(
                    shop_type=Shop.ShopType.WAREHOUSE, is_deleted=False, is_active=True
                ).first()
                or Shop.objects.filter(is_deleted=False, is_active=True).first()
            )
        if not shop:
            return Response(
                {"error": "No valid shop found."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        created = 0
        errors = []
        results = []

        with transaction.atomic():
            for entry in rows:
                try:
                    pname = str(entry.get("product_name", "")).strip()
                    vspec = str(entry.get("variant_spec", "")).strip()
                    qty = float(entry.get("quantity", 0) or 0)
                    ucode = str(entry.get("unit_code", "KG")).strip().upper()
                    shop_pk = entry.get("shop_id", shop_id)

                    if not pname or not vspec:
                        raise ValueError("product_name and variant_spec required")

                    # Shop for this row (override default)
                    row_shop = shop
                    if shop_pk:
                        try:
                            row_shop = Shop.objects.get(pk=shop_pk, is_deleted=False)
                        except Shop.DoesNotExist:
                            pass

                    unit = _get_or_create_unit(ucode, ucode, "COUNT")
                    product = _get_or_create_product(pname, ucode, user)
                    spec_key = f"{pname}|{vspec}|{ucode}"
                    variant = _get_or_create_variant(product, spec_key, unit, user)

                    # Stock level
                    level, _ = StockLevel.objects.get_or_create(
                        shop=row_shop,
                        variant=variant,
                        defaults={"quantity": Decimal("0")},
                    )
                    level.quantity = (level.quantity or Decimal("0")) + Decimal(str(qty))
                    level.save(update_fields=["quantity", "updated_at"])

                    # Ledger row
                    txn_id = f"BULK-{product.code}-{variant.id}"
                    apply_movement(
                        transaction_id=txn_id,
                        kind=StockMovement.Kind.STOCK_IN,
                        shop=row_shop,
                        variant=variant,
                        quantity=Decimal(str(qty)),
                        direction=1,
                        user=user,
                    )

                    created += 1
                    results.append(
                        {
                            "raw_name": entry.get("raw_name", ""),
                            "product": product.code,
                            "product_name": product.name,
                            "variant": variant.spec_key,
                            "quantity": str(level.quantity),
                            "unit": unit.code,
                            "shop": row_shop.code,
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        {"raw_name": entry.get("raw_name", ""), "error": str(exc)}
                    )

        return Response(
            {
                "created": created,
                "errors": len(errors),
                "results": results,
                "error_details": errors,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_400_BAD_REQUEST,
        )