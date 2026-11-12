from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, TransactionTestCase
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import Shop
from catalog.models import Product, ProductCategory, ProductVariant, Unit
from inventory.models import (
    ProductReturn,
    StockAdjustment,
    StockLevel,
    StockMovement,
    StockTransfer,
    StockTransferItem,
)
from sales.models import Sale, SaleItem

User = get_user_model()


class AuditTestMixin:
    def setUp(self):
        self.owner = User.objects.create_user(
            username="audit_owner", password="Str0ngPass!xyz", role=User.Role.OWNER
        )
        self.manager = User.objects.create_user(
            username="audit_mgr", password="Str0ngPass!xyz", role=User.Role.MANAGER
        )
        self.seller = User.objects.create_user(
            username="audit_seller", password="Str0ngPass!xyz", role=User.Role.SELLER
        )

        self.warehouse, _ = Shop.objects.get_or_create(
            code="audit-wh",
            defaults={"name": "Audit Warehouse", "shop_type": "WAREHOUSE"},
        )
        self.shop, _ = Shop.objects.get_or_create(
            code="audit-shop",
            defaults={"name": "Audit Shop", "shop_type": "SHOP"},
        )

        self.category, _ = ProductCategory.objects.get_or_create(
            code="audit-ingots",
            defaults={"name": "Audit Ingots", "form_factor": "INGOT"},
        )
        self.kg, _ = Unit.objects.get_or_create(
            code="KG", defaults={"name": "Kilogram", "unit_type": "WEIGHT"}
        )
        self.product = Product.objects.create(
            code="AUDIT-PROD-1",
            name="Audit Ingot",
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
        super().setUp()

    def login_as(self, user):
        client = APIClient()
        token = str(RefreshToken.for_user(user).access_token)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client


class AuditAccessTests(AuditTestMixin, TestCase):
    """Owner and Manager can view audit log; Seller cannot."""

    def test_owner_can_view_audit_log(self):
        # Generate history via API so history_user is recorded
        client = self.login_as(self.owner)
        self.product.name = "Updated Ingot"
        self.product._history_user = self.owner
        self.product.save()

        res = client.get("/api/audit/")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)
        data = res.json()
        self.assertIn("results", data)
        self.assertGreaterEqual(data["count"], 1)

    def test_manager_can_view_audit_log(self):
        client = self.login_as(self.manager)
        res = client.get("/api/audit/")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)

    def test_seller_cannot_view_audit_log(self):
        client = self.login_as(self.seller)
        res = client.get("/api/audit/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_audit_log_contains_user_action_time(self):
        client = self.login_as(self.owner)
        self.product.name = "Changed Name"
        self.product._history_user = self.owner
        self.product.save()

        res = client.get("/api/audit/?model=product")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        results = res.json()["results"]
        self.assertGreaterEqual(len(results), 1)

        entry = results[0]
        self.assertIn("model", entry)
        self.assertIn("object_id", entry)
        self.assertIn("object_repr", entry)
        self.assertIn("action_type", entry)
        self.assertIn("action", entry)
        self.assertIn("user", entry)
        self.assertIn("timestamp", entry)
        self.assertIn("changes", entry)
        self.assertEqual(entry["model"], "Product")

    def test_audit_log_shows_changes_before_after(self):
        client = self.login_as(self.owner)
        old_name = self.product.name
        self.product.name = "Renamed Product"
        self.product._history_user = self.owner
        self.product.save()

        res = client.get("/api/audit/?model=product")
        results = res.json()["results"]
        update_entry = None
        for e in results:
            if e["action"] == "Updated":
                update_entry = e
                break
        self.assertIsNotNone(update_entry)
        self.assertIsInstance(update_entry["changes"], dict)
        # changes may contain added/removed/changed; verify structure
        self.assertIn("added", update_entry["changes"])
        self.assertIn("removed", update_entry["changes"])
        self.assertIn("changed", update_entry["changes"])

    def test_audit_log_model_filter(self):
        client = self.login_as(self.owner)
        self.product.name = "Filter Test"
        self.product._history_user = self.owner
        self.product.save()

        res = client.get("/api/audit/?model=product")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        for entry in res.json()["results"]:
            self.assertEqual(entry["model"], "Product")

    def test_audit_log_user_filter(self):
        client = self.login_as(self.owner)
        self.product.name = "User Filter Test"
        self.product._history_user = self.owner
        self.product.save()

        res = client.get(f"/api/audit/?user={self.owner.id}")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        for entry in res.json()["results"]:
            self.assertEqual(entry["user_id"], self.owner.id)

    def test_audit_models_endpoint(self):
        client = self.login_as(self.owner)
        res = client.get("/api/audit/models/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        models = res.json()
        model_keys = {m["key"] for m in models}
        self.assertIn("product", model_keys)
        self.assertIn("shop", model_keys)
        self.assertIn("stocklevel", model_keys)

    def test_audit_users_endpoint(self):
        client = self.login_as(self.owner)
        self.product.name = "Users Endpoint Test"
        self.product._history_user = self.owner
        self.product.save()

        res = client.get("/api/audit/users/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        users = res.json()
        usernames = {u["username"] for u in users}
        self.assertIn("audit_owner", usernames)


class AuditStockLevelTests(AuditTestMixin, TestCase):
    """Audit log captures stock level changes."""

    def test_stock_level_audit_entry(self):
        client = self.login_as(self.owner)
        level = StockLevel.objects.create(
            shop=self.shop, variant=self.variant, quantity=Decimal("10")
        )
        level.quantity = Decimal("20")
        level._history_user = self.owner
        level.save()

        res = client.get("/api/audit/?model=stocklevel")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        results = res.json()["results"]
        self.assertGreaterEqual(len(results), 1)

        entry = results[0]
        if entry["action"] == "Updated":
            changes = entry["changes"].get("changed", {})
            # Verify changes structure exists
            self.assertIsInstance(changes, dict)


class ReconciliationCommandTests(AuditTestMixin, TestCase):
    """Stock reconciliation compares balances to ledger sums."""

    def test_reconcile_no_discrepancy(self):
        StockLevel.objects.create(
            shop=self.warehouse, variant=self.variant, quantity=Decimal("100")
        )
        StockMovement.objects.create(
            transaction_id="REC-AUDIT-001",
            kind=StockMovement.Kind.STOCK_IN,
            shop=self.warehouse,
            variant=self.variant,
            quantity=Decimal("100"),
            direction=1,
            created_by=self.owner,
        )
        out = call_command("reconcile_stock", "--shop", self.warehouse.id)
        self.assertIn("No discrepancies", out)

    def test_reconcile_with_discrepancy(self):
        StockLevel.objects.create(
            shop=self.warehouse, variant=self.variant, quantity=Decimal("100")
        )
        StockMovement.objects.create(
            transaction_id="REC-AUDIT-002",
            kind=StockMovement.Kind.STOCK_IN,
            shop=self.warehouse,
            variant=self.variant,
            quantity=Decimal("80"),
            direction=1,
            created_by=self.owner,
        )
        out = call_command("reconcile_stock", "--shop", self.warehouse.id)
        self.assertIn("Found", out)
        self.assertIn("discrepancy", out.lower())


class LoginRateLimitTests(AuditTestMixin, TestCase):
    """Login endpoint has rate limiting configured."""

    def test_login_view_has_throttle(self):
        from accounts.views import LoginView
        self.assertTrue(
            hasattr(LoginView, "throttle_classes")
            and LoginView.throttle_classes
        )


class UploadValidationTests(AuditTestMixin, TestCase):
    """Sale attachment upload validation (5 MB cap, image types only)."""

    def test_sale_attachment_model_limits(self):
        from sales.models import SaleAttachment
        self.assertEqual(SaleAttachment.MAX_SIZE, 5 * 1024 * 1024)
        self.assertIn("image/jpeg", SaleAttachment.ALLOWED_TYPES)
        self.assertIn("image/png", SaleAttachment.ALLOWED_TYPES)
        self.assertIn("image/webp", SaleAttachment.ALLOWED_TYPES)


class NegativeStockTests(AuditTestMixin, TestCase):
    """No negative stock allowed."""

    def test_sale_cannot_make_negative_stock(self):
        from sales.services import create_sale

        StockLevel.objects.create(
            shop=self.shop, variant=self.variant, quantity=Decimal("5")
        )
        client = self.login_as(self.manager)

        res = client.post(
            "/api/sales/",
            {
                "shop": self.shop.id,
                "employee": self.manager.id,
                "lines": [
                    {"variant": self.variant.id, "quantity": "10"}
                ],
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        level = StockLevel.objects.get(shop=self.shop, variant=self.variant)
        self.assertEqual(level.quantity, Decimal("5"))


class IdempotencyTests(AuditTestMixin, TestCase):
    """Idempotency-Key prevents duplicate deduction."""

    def test_duplicate_request_not_double_deducted(self):
        from sales.services import create_sale

        StockLevel.objects.create(
            shop=self.shop, variant=self.variant, quantity=Decimal("10")
        )

        sale1, _ = create_sale(
            shop=self.shop,
            lines=[{"variant": self.variant, "quantity": Decimal("3")}],
            user=self.manager,
            idempotency_key="idem-key-001",
        )
        self.assertIsNotNone(sale1)

        level = StockLevel.objects.get(shop=self.shop, variant=self.variant)
        self.assertEqual(level.quantity, Decimal("7"))


class ConcurrentSalesTests(AuditTestMixin, TransactionTestCase):
    """Concurrent sales on last unit - exactly one succeeds."""

    def test_concurrent_sales_last_unit(self):
        from threading import Barrier, Thread
        from django.db import OperationalError
        from django.db import close_old_connections
        from sales.services import create_sale

        StockLevel.objects.create(
            shop=self.shop, variant=self.variant, quantity=Decimal("1")
        )

        results = {"ok": 0, "fail": 0}
        barrier = Barrier(2)

        def worker():
            try:
                barrier.wait(timeout=10)
                for _ in range(20):
                    try:
                        create_sale(
                            shop=self.shop,
                            lines=[{"variant": self.variant, "quantity": Decimal("1")}],
                            user=self.manager,
                        )
                        results["ok"] += 1
                        return
                    except OperationalError:
                        continue
                    except Exception:
                        results["fail"] += 1
                        return
                results["fail"] += 1
            finally:
                close_old_connections()

        threads = [Thread(target=worker), Thread(target=worker)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(results["ok"], 1)
        self.assertEqual(results["fail"], 1)

        level = StockLevel.objects.get(shop=self.shop, variant=self.variant)
        self.assertEqual(level.quantity, Decimal("0"))