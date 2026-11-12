from datetime import date, timedelta
from decimal import Decimal

from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import Shop
from catalog.models import Product, ProductCategory, ProductVariant, Unit
from inventory.models import StockAdjustment, StockLevel, StockMovement
from sales.models import Sale, SaleItem

from reports.models import LowStockThreshold

User = get_user_model()


class ReportsTestMixin:
    def setUp(self):
        self.owner = User.objects.create_user(
            username="r_owner", password="Str0ngPass!xyz", role=User.Role.OWNER
        )
        self.manager = User.objects.create_user(
            username="r_mgr", password="Str0ngPass!xyz", role=User.Role.MANAGER
        )
        self.seller = User.objects.create_user(
            username="r_seller", password="Str0ngPass!xyz", role=User.Role.SELLER
        )

        self.warehouse, _ = Shop.objects.get_or_create(
            code="r-wh",
            defaults={"name": "R Warehouse", "shop_type": "WAREHOUSE"},
        )
        self.shop, _ = Shop.objects.get_or_create(
            code="r-shop",
            defaults={"name": "R Shop", "shop_type": "SHOP"},
        )

        self.category, _ = ProductCategory.objects.get_or_create(
            code="r-ingots",
            defaults={"name": "R Ingots", "form_factor": "INGOT"},
        )
        self.kg, _ = Unit.objects.get_or_create(
            code="KG", defaults={"name": "Kilogram", "unit_type": "WEIGHT"}
        )
        self.product = Product.objects.create(
            code="R-PROD-1",
            name="R Ingot",
            category=self.category,
            material="MS",
            created_by=self.owner,
        )
        self.variant = ProductVariant.objects.create(
            product=self.product,
            weight=Decimal("5"),
            unit=self.kg,
            weight_per_piece=Decimal("5"),
        )

    def login_as(self, user):
        client = APIClient()
        from rest_framework_simplejwt.tokens import RefreshToken

        token = str(RefreshToken.for_user(user).access_token)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def seed_stock(self, shop, quantity, variant=None):
        level, _ = StockLevel.objects.get_or_create(
            shop=shop,
            variant=variant or self.variant,
            defaults={"quantity": Decimal("0")},
        )
        level.quantity = Decimal(str(quantity))
        level.save(update_fields=["quantity", "updated_at"])
        return level


class DashboardTests(ReportsTestMixin, TestCase):
    def test_owner_sees_all_dashboard_sections(self):
        self.seed_stock(self.warehouse, 100)
        self.seed_stock(self.shop, 50)
        client = self.login_as(self.owner)
        res = client.get("/api/dashboard/")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)
        data = res.json()
        for section in [
            "total_inventory",
            "warehouse_stock",
            "shop_stock",
            "today_sales",
            "employee_sales",
            "product_sales",
            "recent_movements",
            "low_stock_alerts",
            "damaged_returned_stock",
            "stock_discrepancy",
        ]:
            self.assertIn(section, data, f"Missing section: {section}")
        self.assertEqual(data["total_inventory"]["total_quantity"], Decimal("150.000"))

    def test_manager_sees_dashboard(self):
        self.seed_stock(self.warehouse, 100)
        client = self.login_as(self.manager)
        res = client.get("/api/dashboard/")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)

    def test_seller_sees_only_assigned_shop(self):
        from accounts.models import EmployeeShopAssignment
        from django.utils import timezone

        EmployeeShopAssignment.objects.create(
            employee=self.seller,
            shop=self.shop,
            assigned_from=timezone.localdate(),
            created_by=self.owner,
        )
        self.seed_stock(self.shop, 50)
        self.seed_stock(self.warehouse, 100)
        client = self.login_as(self.seller)
        res = client.get("/api/dashboard/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()
        shop_codes = [s["shop_code"] for s in data["shop_stock"]]
        self.assertIn("r-shop", shop_codes)
        self.assertNotIn("r-wh", shop_codes)

    def test_dashboard_date_filter(self):
        self.seed_stock(self.shop, 50)
        client = self.login_as(self.owner)
        res = client.get(
            "/api/dashboard/",
            {"date_from": date.today().isoformat()},
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)


class ReportTests(ReportsTestMixin, TestCase):
    def test_product_wise_report(self):
        self.seed_stock(self.shop, 50)
        client = self.login_as(self.owner)
        res = client.get("/api/reports/?report_type=product-wise")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()
        self.assertEqual(data["report_type"], "product-wise")
        self.assertIn("results", data)

    def test_shop_wise_report(self):
        self.seed_stock(self.shop, 50)
        client = self.login_as(self.owner)
        res = client.get("/api/reports/?report_type=shop-wise")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()
        self.assertEqual(data["report_type"], "shop-wise")

    def test_employee_wise_report(self):
        self.seed_stock(self.shop, 50)
        client = self.login_as(self.owner)
        res = client.get("/api/reports/?report_type=employee-wise")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_stock_movement_report(self):
        self.seed_stock(self.shop, 50)
        client = self.login_as(self.owner)
        res = client.get("/api/reports/?report_type=stock-movement")
        self.assertEqual(res.status_code, status.HTTP_200_OK)


class ExcelExportTests(ReportsTestMixin, TestCase):
    def test_excel_export_product_wise(self):
        self.seed_stock(self.shop, 50)
        client = self.login_as(self.owner)
        res = client.get(
            "/api/reports/excel/?report_type=product-wise"
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn("application/vnd.openxmlformats", res["Content-Type"])
        self.assertIn("attachment", res["Content-Disposition"])

    def test_excel_export_shop_wise(self):
        client = self.login_as(self.owner)
        res = client.get(
            "/api/reports/excel/?report_type=shop-wise"
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_excel_export_employee_wise(self):
        client = self.login_as(self.owner)
        res = client.get(
            "/api/reports/excel/?report_type=employee-wise"
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_excel_export_stock_movement(self):
        client = self.login_as(self.owner)
        res = client.get(
            "/api/reports/excel/?report_type=stock-movement"
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)


class ThresholdTests(ReportsTestMixin, TestCase):
    def test_owner_can_create_threshold(self):
        client = self.login_as(self.owner)
        res = client.post(
            "/api/thresholds/",
            {
                "variant": self.variant.id,
                "shop": self.shop.id,
                "threshold_quantity": "10",
                "is_active": True,
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)

    def test_manager_can_create_threshold(self):
        client = self.login_as(self.manager)
        res = client.post(
            "/api/thresholds/",
            {
                "variant": self.variant.id,
                "shop": self.shop.id,
                "threshold_quantity": "10",
                "is_active": True,
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)

    def test_seller_cannot_create_threshold(self):
        client = self.login_as(self.seller)
        res = client.post(
            "/api/thresholds/",
            {
                "variant": self.variant.id,
                "shop": self.shop.id,
                "threshold_quantity": "10",
                "is_active": True,
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)


class ReconciliationTests(ReportsTestMixin, TestCase):
    def test_reconcile_no_discrepancy(self):
        self.seed_stock(self.warehouse, 100)
        StockMovement.objects.create(
            transaction_id="REC-0001",
            kind=StockMovement.Kind.STOCK_IN,
            shop=self.warehouse,
            variant=self.variant,
            quantity=Decimal("100"),
            direction=1,
            created_by=self.owner,
        )
        out = call_command("reconcile_stock", "--shop", self.warehouse.id, stdout=StringIO())
        self.assertIn("No discrepancies", out)

    def test_reconcile_with_discrepancy(self):
        self.seed_stock(self.warehouse, 100)
        StockMovement.objects.create(
            transaction_id="REC-0002",
            kind=StockMovement.Kind.STOCK_IN,
            shop=self.warehouse,
            variant=self.variant,
            quantity=Decimal("80"),
            direction=1,
            created_by=self.owner,
        )
        out = call_command("reconcile_stock", "--shop", self.warehouse.id, stdout=StringIO())
        self.assertIn("Found", out)
        self.assertIn("discrepancy", out.lower())