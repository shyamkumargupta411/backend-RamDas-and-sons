from decimal import Decimal
from threading import Barrier, Thread

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import OperationalError, close_old_connections, connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import EmployeeShopAssignment, Shop
from catalog.models import Product, ProductCategory, ProductVariant, Unit

from .models import (
    ProductReturn,
    StockAdjustment,
    StockLevel,
    StockMovement,
    StockReceipt,
    StockTransfer,
    StockTransferItem,
)
from .services import (
    cancel_transfer,
    create_receipt,
    dispatch_transfer,
    next_receipt_code,
    next_transfer_code,
    receive_transfer,
)

User = get_user_model()


class InventoryFixtureMixin:
    def setUp(self):
        self.owner = User.objects.create_user(
            username="inv_owner", password="Str0ngPass!xyz", role=User.Role.OWNER
        )
        self.manager = User.objects.create_user(
            username="inv_mgr", password="Str0ngPass!xyz", role=User.Role.MANAGER
        )
        self.seller = User.objects.create_user(
            username="inv_seller", password="Str0ngPass!xyz", role=User.Role.SELLER
        )

        self.warehouse, _ = Shop.objects.get_or_create(
            code="inv-warehouse",
            defaults={"name": "Inv Warehouse", "shop_type": "WAREHOUSE"},
        )
        self.shop, _ = Shop.objects.get_or_create(
            code="inv-shop", defaults={"name": "Inv Shop", "shop_type": "SHOP"}
        )

        self.category, _ = ProductCategory.objects.get_or_create(
            code="metal-ingots",
            defaults={"name": "Metal Ingots", "form_factor": "INGOT"},
        )
        self.kg, _ = Unit.objects.get_or_create(
            code="KG", defaults={"name": "Kilogram", "unit_type": "WEIGHT"}
        )
        self.product = Product.objects.create(
            code="INV-PROD-1",
            name="Test Ingot",
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
        token = str(RefreshToken.for_user(user).access_token)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def anon(self):
        client = APIClient()
        client.credentials()
        return client

    def payload(self, **overrides):
        data = {
            "shop": self.warehouse.id,
            "supplier": "Steel Co",
            "note": "first load",
            "lines": [{"variant": self.variant.id, "quantity": "25.500"}],
        }
        data.update(overrides)
        return data

    def transfer_payload(self, **overrides):
        data = {
            "source": self.warehouse.id,
            "destination": self.shop.id,
            "note": "restock shop",
            "lines": [{"variant": self.variant.id, "quantity": "10"}],
        }
        data.update(overrides)
        return data

    def seed_stock(self, shop, quantity, variant=None):
        level, _ = StockLevel.objects.get_or_create(
            shop=shop,
            variant=variant or self.variant,
            defaults={"quantity": Decimal("0")},
        )
        level.quantity = Decimal(str(quantity))
        level.save(update_fields=["quantity", "updated_at"])
        return level

    def level_qty(self, shop, variant=None):
        return (
            StockLevel.objects.filter(
                shop=shop, variant=variant or self.variant
            ).first().quantity
            if StockLevel.objects.filter(
                shop=shop, variant=variant or self.variant
            ).exists()
            else Decimal("0")
        )


class SeedTests(TestCase):
    def test_central_warehouse_seeded(self):
        self.assertTrue(
            Shop.objects.filter(
                code="central-warehouse", shop_type="WAREHOUSE"
            ).exists()
        )


class ReceiptServiceTests(InventoryFixtureMixin, TestCase):
    def test_receipt_code_format(self):
        self.assertRegex(next_receipt_code(), r"^RCV-\d{4}$")

    def test_create_receipt_applies_stock(self):
        receipt = create_receipt(
            shop=self.warehouse,
            lines=[{"variant": self.variant, "quantity": Decimal("10")}],
            user=self.owner,
        )
        self.assertRegex(receipt.reference, r"^RCV-\d{4}$")
        level = StockLevel.objects.get(
            shop=self.warehouse, variant=self.variant
        )
        self.assertEqual(level.quantity, Decimal("10"))

    def test_second_receipt_accumulates(self):
        create_receipt(
            shop=self.warehouse,
            lines=[{"variant": self.variant, "quantity": Decimal("10")}],
        )
        receipt2 = create_receipt(
            shop=self.warehouse,
            lines=[{"variant": self.variant, "quantity": Decimal("5")}],
        )
        level = StockLevel.objects.get(
            shop=self.warehouse, variant=self.variant
        )
        self.assertEqual(level.quantity, Decimal("15"))
        self.assertNotEqual(receipt2.reference, "RCV-0001")

    def test_rejects_non_warehouse_destination(self):
        with self.assertRaises(ValidationError):
            create_receipt(
                shop=self.shop,
                lines=[{"variant": self.variant, "quantity": Decimal("1")}],
            )

    def test_rejects_zero_and_negative_qty(self):
        with self.assertRaises(ValidationError):
            create_receipt(
                shop=self.warehouse,
                lines=[{"variant": self.variant, "quantity": Decimal("0")}],
            )
        with self.assertRaises(ValidationError):
            create_receipt(
                shop=self.warehouse,
                lines=[{"variant": self.variant, "quantity": Decimal("-3")}],
            )

    def test_rejects_duplicate_variant_lines(self):
        with self.assertRaises(ValidationError):
            create_receipt(
                shop=self.warehouse,
                lines=[
                    {"variant": self.variant, "quantity": Decimal("1")},
                    {"variant": self.variant, "quantity": Decimal("2")},
                ],
            )

    def test_rejects_empty_lines(self):
        with self.assertRaises(ValidationError):
            create_receipt(shop=self.warehouse, lines=[])


class StockLevelAPITests(InventoryFixtureMixin, TestCase):
    def test_owner_sees_stock(self):
        create_receipt(
            shop=self.warehouse,
            lines=[{"variant": self.variant, "quantity": Decimal("12")}],
        )
        client = self.login_as(self.owner)
        res = client.get("/api/stock-levels/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()
        rows = data if isinstance(data, list) else data.get("results", [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["quantity"], "12.000")
        self.assertEqual(rows[0]["product_code"], "INV-PROD-1")
        self.assertEqual(rows[0]["shop_code"], "inv-warehouse")

    def test_manager_can_read_seller_scoped(self):
        client = self.login_as(self.manager)
        self.assertEqual(
            client.get("/api/stock-levels/").status_code, status.HTTP_200_OK
        )
        # Stage 6: sellers may read stock, scoped to assigned shops.
        create_receipt(
            shop=self.warehouse,
            lines=[{"variant": self.variant, "quantity": Decimal("12")}],
        )
        seller_client = self.login_as(self.seller)
        res = seller_client.get("/api/stock-levels/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        rows = res.json()
        rows = rows if isinstance(rows, list) else rows.get("results", [])
        self.assertEqual(rows, [])  # no assignment -> sees nothing

        EmployeeShopAssignment.objects.create(
            employee=self.seller,
            shop=self.shop,
            assigned_from=timezone.localdate(),
            created_by=self.owner,
        )
        StockLevel.objects.create(
            shop=self.shop, variant=self.variant, quantity=Decimal("7")
        )
        rows = seller_client.get("/api/stock-levels/").json()
        rows = rows if isinstance(rows, list) else rows.get("results", [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["shop_code"], "inv-shop")
        self.assertEqual(rows[0]["quantity"], "7.000")

    def test_anonymous_401(self):
        res = self.anon().get("/api/stock-levels/")
        self.assertIn(
            res.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

    def test_zero_stock_hidden(self):
        # level exists but quantity 0 -> filtered out
        StockLevel.objects.create(
            shop=self.warehouse, variant=self.variant, quantity=Decimal("0")
        )
        client = self.login_as(self.owner)
        rows = client.get("/api/stock-levels/").json()
        rows = rows if isinstance(rows, list) else rows.get("results", [])
        self.assertEqual(rows, [])

    def test_search_and_shop_filter(self):
        create_receipt(
            shop=self.warehouse,
            lines=[{"variant": self.variant, "quantity": Decimal("4")}],
        )
        client = self.login_as(self.owner)
        res = client.get("/api/stock-levels/?search=INV-PROD")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        rows = res.json()
        rows = rows if isinstance(rows, list) else rows.get("results", [])
        self.assertEqual(len(rows), 1)

        res = client.get(f"/api/stock-levels/?shop={self.shop.id}")
        rows = res.json()
        rows = rows if isinstance(rows, list) else rows.get("results", [])
        self.assertEqual(rows, [])


class StockReceiptAPITests(InventoryFixtureMixin, TestCase):
    def test_owner_creates_receipt_and_stock_updates(self):
        client = self.login_as(self.owner)
        res = client.post(
            "/api/stock-receipts/", self.payload(), format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        body = res.json()
        self.assertRegex(body["reference"], r"^RCV-\d{4}$")
        self.assertEqual(len(body["lines"]), 1)
        self.assertEqual(body["created_by_username"], "inv_owner")
        level = StockLevel.objects.get(
            shop=self.warehouse, variant=self.variant
        )
        self.assertEqual(level.quantity, Decimal("25.500"))

    def test_manager_can_receive_stock(self):
        client = self.login_as(self.manager)
        res = client.post(
            "/api/stock-receipts/", self.payload(), format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

    def test_seller_cannot_receive(self):
        client = self.login_as(self.seller)
        res = client.post(
            "/api/stock-receipts/", self.payload(), format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(StockReceipt.objects.count(), 0)

    def test_rejects_shop_destination(self):
        client = self.login_as(self.owner)
        res = client.post(
            "/api/stock-receipts/", self.payload(shop=self.shop.id), format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(StockReceipt.objects.count(), 0)

    def test_rejects_zero_qty(self):
        client = self.login_as(self.owner)
        res = client.post(
            "/api/stock-receipts/",
            self.payload(
                lines=[{"variant": self.variant.id, "quantity": "0"}]
            ),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(StockReceipt.objects.count(), 0)

    def test_rejects_duplicate_variant_lines(self):
        client = self.login_as(self.owner)
        res = client.post(
            "/api/stock-receipts/",
            self.payload(
                lines=[
                    {"variant": self.variant.id, "quantity": "1"},
                    {"variant": self.variant.id, "quantity": "2"},
                ]
            ),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(StockReceipt.objects.count(), 0)

    def test_rejects_unknown_variant(self):
        client = self.login_as(self.owner)
        res = client.post(
            "/api/stock-receipts/",
            self.payload(lines=[{"variant": 999999, "quantity": "1"}]),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_receipts_list_and_search(self):
        client = self.login_as(self.owner)
        client.post("/api/stock-receipts/", self.payload(), format="json")
        res = client.get("/api/stock-receipts/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        rows = res.json()
        rows = rows if isinstance(rows, list) else rows.get("results", [])
        self.assertEqual(len(rows), 1)

        res = client.get("/api/stock-receipts/?search=RCV")
        rows = res.json()
        rows = rows if isinstance(rows, list) else rows.get("results", [])
        self.assertEqual(len(rows), 1)


# ---------------------------------------------------------------------------
# STAGE 5 — Transfers
# ---------------------------------------------------------------------------


class TransferAPITests(InventoryFixtureMixin, TestCase):
    def test_create_transfer_draft(self):
        client = self.login_as(self.owner)
        res = client.post("/api/transfers/", self.transfer_payload(), format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        body = res.json()
        self.assertRegex(body["reference"], r"^TRF-\d{4}$")
        self.assertEqual(body["status"], "DRAFT")
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["pending_quantity"], "10.000")
        self.assertEqual(body["created_by_username"], "inv_owner")

    def test_manager_can_create_seller_cannot(self):
        client = self.login_as(self.manager)
        res = client.post("/api/transfers/", self.transfer_payload(), format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        seller_client = self.login_as(self.seller)
        res = seller_client.post(
            "/api/transfers/", self.transfer_payload(), format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_rejects_same_source_destination(self):
        client = self.login_as(self.owner)
        res = client.post(
            "/api/transfers/",
            self.transfer_payload(destination=self.warehouse.id),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rejects_in_transit_endpoints(self):
        in_transit = Shop.objects.get(code="in-transit")
        client = self.login_as(self.owner)
        res = client.post(
            "/api/transfers/",
            self.transfer_payload(destination=in_transit.id),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        res = client.post(
            "/api/transfers/",
            self.transfer_payload(source=in_transit.id),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rejects_duplicate_variant_lines(self):
        client = self.login_as(self.owner)
        res = client.post(
            "/api/transfers/",
            self.transfer_payload(
                lines=[
                    {"variant": self.variant.id, "quantity": "5"},
                    {"variant": self.variant.id, "quantity": "3"},
                ]
            ),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_search_and_status_filter(self):
        client = self.login_as(self.owner)
        client.post("/api/transfers/", self.transfer_payload(), format="json")
        res = client.get("/api/transfers/?search=TRF")
        rows = res.json()
        rows = rows if isinstance(rows, list) else rows.get("results", [])
        self.assertEqual(len(rows), 1)
        res = client.get("/api/transfers/?status=RECEIVED")
        rows = res.json()
        rows = rows if isinstance(rows, list) else rows.get("results", [])
        self.assertEqual(rows, [])


class TransferDispatchTests(InventoryFixtureMixin, TestCase):
    def test_dispatch_moves_stock_and_writes_ledger(self):
        self.seed_stock(self.warehouse, 20)
        client = self.login_as(self.owner)
        res = client.post("/api/transfers/", self.transfer_payload(), format="json")
        tid = res.json()["id"]

        res = client.post(f"/api/transfers/{tid}/dispatch/", {}, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)
        body = res.json()
        self.assertEqual(body["status"], "DISPATCHED")
        self.assertIsNotNone(body["dispatched_at"])
        self.assertEqual(body["dispatched_by_username"], "inv_owner")

        in_transit = Shop.objects.get(code="in-transit")
        self.assertEqual(self.level_qty(self.warehouse), Decimal("10"))
        self.assertEqual(self.level_qty(in_transit), Decimal("10"))

        # Out/in ledger rows share the transfer reference as transaction_id.
        moves = StockMovement.objects.filter(transaction_id=body["reference"])
        self.assertEqual(moves.count(), 2)
        kinds = set(moves.values_list("kind", flat=True))
        self.assertEqual(
            kinds, {"DISPATCH_OUT", "DISPATCH_IN"}
        )
        out = moves.get(kind="DISPATCH_OUT")
        inc = moves.get(kind="DISPATCH_IN")
        self.assertEqual(out.shop_id, self.warehouse.id)
        self.assertEqual(out.direction, -1)
        self.assertEqual(inc.shop_id, in_transit.id)
        self.assertEqual(inc.direction, 1)
        self.assertEqual(out.transfer_id, tid)

    def test_negative_stock_blocked(self):
        self.seed_stock(self.warehouse, 5)  # need 10, have 5
        client = self.login_as(self.owner)
        res = client.post("/api/transfers/", self.transfer_payload(), format="json")
        tid = res.json()["id"]

        res = client.post(f"/api/transfers/{tid}/dispatch/", {}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

        # No stock moved, still DRAFT, no ledger rows.
        self.assertEqual(self.level_qty(self.warehouse), Decimal("5"))
        in_transit = Shop.objects.get(code="in-transit")
        self.assertEqual(self.level_qty(in_transit), Decimal("0"))
        transfer = StockTransfer.objects.get(pk=tid)
        self.assertEqual(transfer.status, "DRAFT")
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_cannot_dispatch_twice(self):
        self.seed_stock(self.warehouse, 20)
        client = self.login_as(self.owner)
        res = client.post("/api/transfers/", self.transfer_payload(), format="json")
        tid = res.json()["id"]
        client.post(f"/api/transfers/{tid}/dispatch/", {}, format="json")
        res = client.post(f"/api/transfers/{tid}/dispatch/", {}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_dispatch_permissions(self):
        self.seed_stock(self.warehouse, 20)
        owner_client = self.login_as(self.owner)
        res = owner_client.post(
            "/api/transfers/", self.transfer_payload(), format="json"
        )
        tid = res.json()["id"]

        seller_client = self.login_as(self.seller)
        res = seller_client.post(f"/api/transfers/{tid}/dispatch/", {}, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

        manager_client = self.login_as(self.manager)
        res = manager_client.post(
            f"/api/transfers/{tid}/dispatch/", {}, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_shop_to_shop_transfer(self):
        # Stock lives at the shop; transfer shop -> warehouse.
        self.seed_stock(self.shop, 8)
        client = self.login_as(self.owner)
        res = client.post(
            "/api/transfers/",
            self.transfer_payload(source=self.shop.id, destination=self.warehouse.id,
                                 lines=[{"variant": self.variant.id, "quantity": "3"}]),
            format="json",
        )
        tid = res.json()["id"]
        res = client.post(f"/api/transfers/{tid}/dispatch/", {}, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(self.level_qty(self.shop), Decimal("5"))
        in_transit = Shop.objects.get(code="in-transit")
        self.assertEqual(self.level_qty(in_transit), Decimal("3"))


class TransferReceiveTests(InventoryFixtureMixin, TestCase):
    def _dispatched_transfer(self, qty="10"):
        self.seed_stock(self.warehouse, 50)
        client = self.login_as(self.owner)
        res = client.post(
            "/api/transfers/",
            self.transfer_payload(
                lines=[{"variant": self.variant.id, "quantity": qty}]
            ),
            format="json",
        )
        tid = res.json()["id"]
        client.post(f"/api/transfers/{tid}/dispatch/", {}, format="json")
        return client, tid

    def test_full_receive(self):
        client, tid = self._dispatched_transfer()
        res = client.post(
            f"/api/transfers/{tid}/receive/",
            {"lines": [{"variant": self.variant.id, "received_quantity": "10"}]},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)
        body = res.json()
        self.assertEqual(body["status"], "RECEIVED")
        self.assertIsNotNone(body["received_at"])
        self.assertEqual(body["received_by_username"], "inv_owner")

        in_transit = Shop.objects.get(code="in-transit")
        self.assertEqual(self.level_qty(in_transit), Decimal("0"))
        self.assertEqual(self.level_qty(self.shop), Decimal("10"))
        self.assertEqual(self.level_qty(self.warehouse), Decimal("40"))

        # Receive ledger rows share the same transaction id (reference).
        moves = StockMovement.objects.filter(
            transaction_id=body["reference"],
            kind__in=["RECEIVE_OUT", "RECEIVE_IN"],
        )
        self.assertEqual(moves.count(), 2)

    def test_partial_receipt_without_reason_stays_pending(self):
        client, tid = self._dispatched_transfer()
        res = client.post(
            f"/api/transfers/{tid}/receive/",
            {"lines": [{"variant": self.variant.id, "received_quantity": "6"}]},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)
        body = res.json()
        self.assertEqual(body["status"], "DISPATCHED")  # still open
        item = body["items"][0]
        self.assertEqual(item["received_quantity"], "6.000")
        self.assertEqual(item["pending_quantity"], "4.000")

        in_transit = Shop.objects.get(code="in-transit")
        self.assertEqual(self.level_qty(in_transit), Decimal("4"))
        self.assertEqual(self.level_qty(self.shop), Decimal("6"))

        # Receive the rest later.
        res = client.post(
            f"/api/transfers/{tid}/receive/",
            {"lines": [{"variant": self.variant.id, "received_quantity": "4"}]},
            format="json",
        )
        self.assertEqual(res.json()["status"], "RECEIVED")
        self.assertEqual(self.level_qty(in_transit), Decimal("0"))

    def test_partial_receipt_with_shortage_creates_adjustment(self):
        client, tid = self._dispatched_transfer()
        res = client.post(
            f"/api/transfers/{tid}/receive/",
            {
                "lines": [
                    {
                        "variant": self.variant.id,
                        "received_quantity": "7",
                        "shortage_reason": "2 damaged in transit",
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)
        body = res.json()
        # All accounted (7 received + 3 shortage) -> RECEIVED
        self.assertEqual(body["status"], "RECEIVED")
        item = body["items"][0]
        self.assertEqual(item["received_quantity"], "7.000")
        self.assertEqual(item["shortage_quantity"], "3.000")
        self.assertEqual(item["shortage_reason"], "2 damaged in transit")

        in_transit = Shop.objects.get(code="in-transit")
        self.assertEqual(self.level_qty(in_transit), Decimal("0"))
        self.assertEqual(self.level_qty(self.shop), Decimal("7"))

        adj = StockAdjustment.objects.get(transfer_id=tid)
        self.assertRegex(adj.reference, r"^ADJ-\d{4}$")
        self.assertEqual(adj.reason, "SHORTAGE")
        self.assertEqual(adj.quantity, Decimal("3"))
        self.assertEqual(adj.note, "2 damaged in transit")
        self.assertEqual(adj.shop_id, in_transit.id)

        # Shortage ledger row shares the transfer transaction id.
        short = StockMovement.objects.get(kind="SHORTAGE", transfer_id=tid)
        self.assertEqual(short.transaction_id, body["reference"])
        self.assertEqual(short.quantity, Decimal("3"))
        self.assertEqual(short.direction, -1)

    def test_rejects_over_receive(self):
        client, tid = self._dispatched_transfer()
        res = client.post(
            f"/api/transfers/{tid}/receive/",
            {"lines": [{"variant": self.variant.id, "received_quantity": "11"}]},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_receive_draft(self):
        client = self.login_as(self.owner)
        res = client.post(
            "/api/transfers/", self.transfer_payload(), format="json"
        )
        tid = res.json()["id"]
        res = client.post(
            f"/api/transfers/{tid}/receive/",
            {"lines": [{"variant": self.variant.id, "received_quantity": "10"}]},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_receive_twice_after_received_rejected(self):
        client, tid = self._dispatched_transfer()
        client.post(
            f"/api/transfers/{tid}/receive/",
            {"lines": [{"variant": self.variant.id, "received_quantity": "10"}]},
            format="json",
        )
        res = client.post(
            f"/api/transfers/{tid}/receive/",
            {"lines": [{"variant": self.variant.id, "received_quantity": "0"}]},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)


class TransferCancelTests(InventoryFixtureMixin, TestCase):
    def test_cancel_draft_no_stock_effect(self):
        client = self.login_as(self.owner)
        res = client.post(
            "/api/transfers/", self.transfer_payload(), format="json"
        )
        tid = res.json()["id"]
        res = client.post(f"/api/transfers/{tid}/cancel/", {}, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)
        self.assertEqual(res.json()["status"], "CANCELLED")
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_cancel_dispatched_reverses_stock(self):
        self.seed_stock(self.warehouse, 20)
        client = self.login_as(self.owner)
        res = client.post(
            "/api/transfers/", self.transfer_payload(), format="json"
        )
        tid = res.json()["id"]
        client.post(f"/api/transfers/{tid}/dispatch/", {}, format="json")

        res = client.post(f"/api/transfers/{tid}/cancel/", {}, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)
        body = res.json()
        self.assertEqual(body["status"], "CANCELLED")
        self.assertIsNotNone(body["cancelled_at"])

        in_transit = Shop.objects.get(code="in-transit")
        self.assertEqual(self.level_qty(self.warehouse), Decimal("20"))
        self.assertEqual(self.level_qty(in_transit), Decimal("0"))

        # Reversal ledger rows share the transfer reference.
        cancels = StockMovement.objects.filter(
            transaction_id=body["reference"],
            kind__in=["CANCEL_OUT", "CANCEL_IN"],
        )
        self.assertEqual(cancels.count(), 2)
        self.assertEqual(
            set(cancels.values_list("direction", flat=True)), {-1, 1}
        )

    def test_cancel_after_receive_rejected(self):
        self.seed_stock(self.warehouse, 20)
        client = self.login_as(self.owner)
        res = client.post(
            "/api/transfers/", self.transfer_payload(), format="json"
        )
        tid = res.json()["id"]
        client.post(f"/api/transfers/{tid}/dispatch/", {}, format="json")
        client.post(
            f"/api/transfers/{tid}/receive/",
            {"lines": [{"variant": self.variant.id, "received_quantity": "10"}]},
            format="json",
        )
        res = client.post(f"/api/transfers/{tid}/cancel/", {}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cancel_dispatched_after_partial_receive_reverses_only_pending(self):
        self.seed_stock(self.warehouse, 20)
        client = self.login_as(self.owner)
        res = client.post(
            "/api/transfers/", self.transfer_payload(), format="json"
        )
        tid = res.json()["id"]
        client.post(f"/api/transfers/{tid}/dispatch/", {}, format="json")
        # Receive 6 of 10 (no shortage) -> 4 still in transit.
        client.post(
            f"/api/transfers/{tid}/receive/",
            {"lines": [{"variant": self.variant.id, "received_quantity": "6"}]},
            format="json",
        )
        res = client.post(f"/api/transfers/{tid}/cancel/", {}, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)
        self.assertEqual(res.json()["status"], "CANCELLED")

        in_transit = Shop.objects.get(code="in-transit")
        # 4 pending units returned to source; 6 stay at destination.
        self.assertEqual(self.level_qty(in_transit), Decimal("0"))
        self.assertEqual(self.level_qty(self.warehouse), Decimal("14"))
        self.assertEqual(self.level_qty(self.shop), Decimal("6"))

    def test_seller_cannot_cancel(self):
        self.seed_stock(self.warehouse, 20)
        owner_client = self.login_as(self.owner)
        res = owner_client.post(
            "/api/transfers/", self.transfer_payload(), format="json"
        )
        tid = res.json()["id"]
        seller_client = self.login_as(self.seller)
        res = seller_client.post(f"/api/transfers/{tid}/cancel/", {}, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)


class TransferSellerScopeTests(InventoryFixtureMixin, TestCase):
    """Shop staff see incoming transfers and can confirm receipt."""

    def setUp(self):
        super().setUp()
        EmployeeShopAssignment.objects.create(
            employee=self.seller,
            shop=self.shop,
            assigned_from=timezone.localdate(),
            created_by=self.owner,
        )

    def _make_transfer(self, destination, qty="10"):
        self.seed_stock(self.warehouse, 50)
        client = self.login_as(self.owner)
        res = client.post(
            "/api/transfers/",
            self.transfer_payload(
                destination=destination.id,
                lines=[{"variant": self.variant.id, "quantity": qty}],
            ),
            format="json",
        )
        tid = res.json()["id"]
        client.post(f"/api/transfers/{tid}/dispatch/", {}, format="json")
        return tid

    def test_seller_sees_only_incoming(self):
        other_shop, _ = Shop.objects.get_or_create(
            code="other-shop", defaults={"name": "Other Shop"}
        )
        incoming = self._make_transfer(self.shop)
        self._make_transfer(other_shop)

        seller_client = self.login_as(self.seller)
        res = seller_client.get("/api/transfers/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        rows = res.json()
        rows = rows if isinstance(rows, list) else rows.get("results", [])
        refs = [r["reference"] for r in rows]
        self.assertEqual(len(refs), 1)
        transfer = StockTransfer.objects.get(pk=incoming)
        self.assertEqual(refs, [transfer.reference])

    def test_seller_can_confirm_receipt(self):
        tid = self._make_transfer(self.shop)
        seller_client = self.login_as(self.seller)
        res = seller_client.post(
            f"/api/transfers/{tid}/receive/",
            {"lines": [{"variant": self.variant.id, "received_quantity": "10"}]},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)
        self.assertEqual(res.json()["status"], "RECEIVED")
        self.assertEqual(res.json()["received_by_username"], "inv_seller")
        self.assertEqual(self.level_qty(self.shop), Decimal("10"))

    def test_seller_cannot_receive_other_shops_transfer(self):
        other_shop, _ = Shop.objects.get_or_create(
            code="other-shop2", defaults={"name": "Other Shop 2"}
        )
        tid = self._make_transfer(other_shop)
        seller_client = self.login_as(self.seller)
        res = seller_client.post(
            f"/api/transfers/{tid}/receive/",
            {"lines": [{"variant": self.variant.id, "received_quantity": "10"}]},
            format="json",
        )
        # Not in the seller's queryset -> 404 (hidden), never applied.
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.level_qty(other_shop), Decimal("0"))

    def test_seller_cannot_dispatch_or_create(self):
        tid = self._make_transfer(self.shop)
        seller_client = self.login_as(self.seller)
        res = seller_client.post(f"/api/transfers/{tid}/dispatch/", {}, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        res = seller_client.post(
            "/api/transfers/", self.transfer_payload(), format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)


class InTransitReportTests(InventoryFixtureMixin, TestCase):
    def test_report_lists_only_dispatched(self):
        self.seed_stock(self.warehouse, 50)
        client = self.login_as(self.owner)
        res = client.post(
            "/api/transfers/", self.transfer_payload(), format="json"
        )
        draft_id = res.json()["id"]
        res = client.post(
            "/api/transfers/",
            self.transfer_payload(
                lines=[{"variant": self.variant.id, "quantity": "5"}]
            ),
            format="json",
        )
        dispatched_id = res.json()["id"]
        client.post(f"/api/transfers/{dispatched_id}/dispatch/", {}, format="json")

        res = client.get("/api/transfers/in_transit/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        rows = res.json()
        rows = rows if isinstance(rows, list) else rows.get("results", [])
        ids = [r["id"] for r in rows]
        self.assertEqual(ids, [dispatched_id])
        self.assertEqual(rows[0]["in_transit_quantity"], "5.000")
        self.assertNotIn(draft_id, ids)

        # After full receive the report is empty.
        client.post(
            f"/api/transfers/{dispatched_id}/receive/",
            {"lines": [{"variant": self.variant.id, "received_quantity": "5"}]},
            format="json",
        )
        res = client.get("/api/transfers/in_transit/")
        rows = res.json()
        rows = rows if isinstance(rows, list) else rows.get("results", [])
        self.assertEqual(rows, [])


class ConcurrentDispatchTests(TransactionTestCase):
    """Two dispatches race for the last unit: exactly one wins.

    Uses TransactionTestCase so committed stock is visible across threads
    (SQLite shared-cache in-memory test DB).
    """

    def setUp(self):
        self.owner = User.objects.create_user(
            username="cc_owner", password="Str0ngPass!xyz", role=User.Role.OWNER
        )
        self.warehouse, _ = Shop.objects.get_or_create(
            code="cc-warehouse",
            defaults={"name": "CC Warehouse", "shop_type": "WAREHOUSE"},
        )
        self.shop, _ = Shop.objects.get_or_create(
            code="cc-shop", defaults={"name": "CC Shop", "shop_type": "SHOP"}
        )
        self.category, _ = ProductCategory.objects.get_or_create(
            code="metal-ingots",
            defaults={"name": "Metal Ingots", "form_factor": "INGOT"},
        )
        self.kg, _ = Unit.objects.get_or_create(
            code="KG", defaults={"name": "Kilogram", "unit_type": "WEIGHT"}
        )
        self.product = Product.objects.create(
            code="CC-PROD-1",
            name="CC Ingot",
            category=self.category,
            material="MS",
            created_by=self.owner,
        )
        self.variant = ProductVariant.objects.create(
            product=self.product,
            weight=Decimal("1"),
            unit=self.kg,
        )
        StockLevel.objects.create(
            shop=self.warehouse, variant=self.variant, quantity=Decimal("1")
        )

        from .services import next_transfer_code

        self.transfers = []
        for _ in range(2):
            t = StockTransfer.objects.create(
                reference=next_transfer_code(),
                source=self.warehouse,
                destination=self.shop,
                created_by=self.owner,
            )
            from .models import StockTransferItem

            StockTransferItem.objects.create(
                transfer=t, variant=self.variant, quantity=Decimal("1")
            )
            self.transfers.append(t)

    def tearDown(self):
        close_old_connections()

    def test_concurrent_dispatch_of_last_unit(self):
        barrier = Barrier(2)
        results = {"ok": 0, "fail": 0, "errors": []}

        def worker(transfer):
            # SQLite shared-cache raises "database table is locked" instead
            # of waiting like Postgres — retry briefly to simulate blocking.
            from time import sleep

            try:
                barrier.wait(timeout=10)
                last_exc = None
                for _ in range(40):
                    try:
                        transfer.refresh_from_db()
                        dispatch_transfer(transfer, user=self.owner)
                        results["ok"] += 1
                        return
                    except OperationalError as exc:
                        last_exc = exc
                        sleep(0.05)
                    except Exception as exc:  # noqa: BLE001
                        results["fail"] += 1
                        results["errors"].append(repr(exc))
                        return
                results["fail"] += 1
                results["errors"].append(repr(last_exc))
            finally:
                close_old_connections()

        threads = [
            Thread(target=worker, args=(t,)) for t in self.transfers
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)

        # Exactly one dispatch succeeded.
        self.assertEqual(results["ok"], 1, results)
        self.assertEqual(results["fail"], 1, results)

        # Stock never went negative: last unit went to exactly one transfer.
        level = StockLevel.objects.get(
            shop=self.warehouse, variant=self.variant
        )
        self.assertEqual(level.quantity, Decimal("0"))

        in_transit = Shop.objects.get(code="in-transit")
        it_level = StockLevel.objects.get(
            shop=in_transit, variant=self.variant
        )
        self.assertEqual(it_level.quantity, Decimal("1"))

        statuses = sorted(
            StockTransfer.objects.values_list("status", flat=True)
        )
        self.assertEqual(
            statuses, ["DISPATCHED", "DRAFT"]
        )


# ---------------------------------------------------------------------------
# Stage 7 — Returns
# ---------------------------------------------------------------------------


class ProductReturnTests(InventoryFixtureMixin, TestCase):
    def return_payload(self, **overrides):
        data = {
            "shop": self.shop.id,
            "variant": self.variant.id,
            "quantity": "2",
            "reason": "customer changed mind",
            "customer_name": "Walk-in",
        }
        data.update(overrides)
        return data

    def test_create_return_is_pending_without_stock_change(self):
        self.seed_stock(self.shop, 10)
        client = self.login_as(self.manager)
        res = client.post("/api/returns/", self.return_payload(), format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data["status"], "PENDING")
        self.assertRegex(res.data["reference"], r"^RET-\d{4}$")
        # No stock change while pending.
        self.assertEqual(self.level_qty(self.shop), Decimal("10"))
        self.assertFalse(
            StockMovement.objects.filter(
                transaction_id=res.data["reference"]
            ).exists()
        )

    def test_accept_return_increases_stock_with_ledger(self):
        self.seed_stock(self.shop, 10)
        client = self.login_as(self.manager)
        res = client.post("/api/returns/", self.return_payload(), format="json")
        pk = res.data["id"]

        res2 = client.post(f"/api/returns/{pk}/accept/", format="json")
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        self.assertEqual(res2.data["status"], "ACCEPTED")
        self.assertEqual(res2.data["decided_by_username"], "inv_mgr")

        # Stock went up exactly once.
        self.assertEqual(self.level_qty(self.shop), Decimal("12"))
        move = StockMovement.objects.get(
            transaction_id=res2.data["reference"]
        )
        self.assertEqual(move.kind, StockMovement.Kind.RETURN_IN)
        self.assertEqual(move.direction, 1)
        self.assertEqual(move.quantity, Decimal("2"))
        self.assertEqual(move.shop, self.shop)
        self.assertEqual(move.created_by, self.manager)

    def test_rejected_return_leaves_stock_untouched(self):
        self.seed_stock(self.shop, 10)
        client = self.login_as(self.manager)
        res = client.post("/api/returns/", self.return_payload(), format="json")

        res2 = client.post(
            f"/api/returns/{res.data['id']}/reject/",
            {"note": "used item"},
            format="json",
        )
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        self.assertEqual(res2.data["status"], "REJECTED")
        self.assertEqual(res2.data["decision_note"], "used item")
        self.assertEqual(self.level_qty(self.shop), Decimal("10"))
        self.assertFalse(
            StockMovement.objects.filter(
                transaction_id=res2.data["reference"]
            ).exists()
        )

    def test_only_pending_returns_can_be_decided(self):
        self.seed_stock(self.shop, 10)
        client = self.login_as(self.manager)
        res = client.post("/api/returns/", self.return_payload(), format="json")
        pk = res.data["id"]
        client.post(f"/api/returns/{pk}/accept/", format="json")

        again = client.post(f"/api/returns/{pk}/accept/", format="json")
        self.assertEqual(again.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.level_qty(self.shop), Decimal("12"))

        reject = client.post(f"/api/returns/{pk}/reject/", format="json")
        self.assertEqual(reject.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reason_and_quantity_required(self):
        client = self.login_as(self.manager)
        res = client.post(
            "/api/returns/", self.return_payload(reason="  "), format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

        res = client.post(
            "/api/returns/", self.return_payload(quantity="0"), format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_in_transit_location_rejected(self):
        in_transit = Shop.objects.get(code="in-transit")
        client = self.login_as(self.manager)
        res = client.post(
            "/api/returns/",
            self.return_payload(shop=in_transit.id),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_sale_link_must_match_location(self):
        from sales.models import Sale
        from sales.services import create_sale

        self.seed_stock(self.shop, 5)
        sale, _ = create_sale(
            shop=self.shop,
            lines=[{"variant": self.variant, "quantity": Decimal("1")}],
            user=self.manager,
        )
        self.assertIsInstance(sale, Sale)

        client = self.login_as(self.manager)
        # Wrong location for that sale -> rejected.
        res = client.post(
            "/api/returns/",
            self.return_payload(sale=sale.id, shop=self.warehouse.id),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

        # Matching location -> accepted, and the sale exposes the link.
        res = client.post(
            "/api/returns/",
            self.return_payload(sale=sale.id),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        sale_res = client.get(f"/api/sales/{sale.id}/")
        self.assertEqual(sale_res.status_code, status.HTTP_200_OK)
        linked = [
            r for r in sale_res.data["returns"]
            if r["reference"] == res.data["reference"]
        ]
        self.assertEqual(len(linked), 1)

    def test_seller_cannot_use_returns_api(self):
        client = self.login_as(self.seller)
        res = client.post("/api/returns/", self.return_payload(), format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        res = client.get("/api/returns/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)


# ---------------------------------------------------------------------------
# Stage 7 — Stock adjustments
# ---------------------------------------------------------------------------


class StockAdjustmentApiTests(InventoryFixtureMixin, TestCase):
    def adj_payload(self, **overrides):
        data = {
            "shop": self.shop.id,
            "variant": self.variant.id,
            "quantity": "3",
            "reason": "DAMAGED",
            "note": "crushed in storage",
        }
        data.update(overrides)
        return data

    def test_damaged_reduces_stock_and_writes_ledger(self):
        self.seed_stock(self.shop, 10)
        client = self.login_as(self.manager)
        res = client.post(
            "/api/stock-adjustments/", self.adj_payload(), format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertRegex(res.data["reference"], r"^ADJ-\d{4}$")
        self.assertEqual(res.data["direction"], -1)
        self.assertEqual(res.data["created_by_username"], "inv_mgr")

        self.assertEqual(self.level_qty(self.shop), Decimal("7"))
        move = StockMovement.objects.get(transaction_id=res.data["reference"])
        self.assertEqual(move.kind, StockMovement.Kind.ADJUSTMENT)
        self.assertEqual(move.direction, -1)
        self.assertEqual(move.quantity, Decimal("3"))

    def test_missing_reduces_stock(self):
        self.seed_stock(self.shop, 10)
        client = self.login_as(self.manager)
        res = client.post(
            "/api/stock-adjustments/",
            self.adj_payload(reason="MISSING", quantity="4",
                             note="cannot locate"),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.level_qty(self.shop), Decimal("6"))
        move = StockMovement.objects.get(transaction_id=res.data["reference"])
        self.assertEqual(move.direction, -1)

    def test_owner_correction_increase(self):
        self.seed_stock(self.warehouse, 20)
        owner = self.login_as(self.owner)
        res = owner.post(
            "/api/stock-adjustments/",
            self.adj_payload(shop=self.warehouse.id, reason="CORRECTION",
                             direction=1, quantity="5",
                             note="found miscounted pallet"),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data["direction"], 1)
        self.assertEqual(self.level_qty(self.warehouse), Decimal("25"))
        move = StockMovement.objects.get(transaction_id=res.data["reference"])
        self.assertEqual(move.kind, StockMovement.Kind.ADJUSTMENT)
        self.assertEqual(move.direction, 1)

    def test_owner_correction_decrease(self):
        self.seed_stock(self.shop, 10)
        owner = self.login_as(self.owner)
        res = owner.post(
            "/api/stock-adjustments/",
            self.adj_payload(reason="CORRECTION", direction=-1,
                             quantity="4", note="miscounted, too high"),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.level_qty(self.shop), Decimal("6"))

    def test_manager_cannot_record_correction(self):
        self.seed_stock(self.shop, 10)
        client = self.login_as(self.manager)
        res = client.post(
            "/api/stock-adjustments/",
            self.adj_payload(reason="CORRECTION", direction=1,
                             note="trying"),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("owner", str(res.data).lower())
        self.assertEqual(self.level_qty(self.shop), Decimal("10"))

    def test_correction_requires_direction(self):
        self.seed_stock(self.shop, 10)
        owner = self.login_as(self.owner)
        res = owner.post(
            "/api/stock-adjustments/",
            self.adj_payload(reason="CORRECTION", direction=None,
                             note="forgot direction"),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.level_qty(self.shop), Decimal("10"))

    def test_note_required(self):
        client = self.login_as(self.manager)
        res = client.post(
            "/api/stock-adjustments/", self.adj_payload(note=" "),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_shortage_not_allowed_manually(self):
        client = self.login_as(self.manager)
        res = client.post(
            "/api/stock-adjustments/",
            self.adj_payload(reason="SHORTAGE", note="nope"),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_decrease_below_zero_rejected(self):
        self.seed_stock(self.shop, 2)
        owner = self.login_as(self.owner)
        res = owner.post(
            "/api/stock-adjustments/",
            self.adj_payload(reason="CORRECTION", direction=-1,
                             quantity="5", note="too high"),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.level_qty(self.shop), Decimal("2"))

    def test_adjustments_are_immutable(self):
        self.seed_stock(self.shop, 10)
        client = self.login_as(self.manager)
        res = client.post(
            "/api/stock-adjustments/", self.adj_payload(), format="json"
        )
        pk = res.data["id"]
        for method in ("put", "patch", "delete"):
            r = getattr(client, method)(
                f"/api/stock-adjustments/{pk}/",
                self.adj_payload(note="edited"),
                format="json",
            )
            self.assertEqual(r.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        # Still exactly as created.
        adj = StockAdjustment.objects.get(pk=pk)
        self.assertEqual(adj.note, "crushed in storage")

    def test_reference_sequence(self):
        self.seed_stock(self.shop, 10)
        client = self.login_as(self.manager)
        r1 = client.post(
            "/api/stock-adjustments/", self.adj_payload(), format="json"
        )
        r2 = client.post(
            "/api/stock-adjustments/", self.adj_payload(), format="json"
        )
        self.assertEqual(r1.data["reference"], "ADJ-0001")
        self.assertEqual(r2.data["reference"], "ADJ-0002")

    def test_seller_cannot_use_adjustments_api(self):
        client = self.login_as(self.seller)
        res = client.post(
            "/api/stock-adjustments/", self.adj_payload(), format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        res = client.get("/api/stock-adjustments/")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_auto_shortage_adjustment_still_recorded(self):
        """Transfer shortage keeps its automatic SHORTAGE adjustment row."""
        self.seed_stock(self.warehouse, 10)
        t = StockTransfer.objects.create(
            reference=next_transfer_code(),
            source=self.warehouse, destination=self.shop,
            note="x", created_by=self.owner,
        )
        StockTransferItem.objects.create(
            transfer=t, variant=self.variant, quantity=Decimal("5")
        )
        dispatch_transfer(t, user=self.owner)
        receive_transfer(
            t,
            [{"item": t.items.get(),
              "received_quantity": Decimal("3"),
              "shortage_reason": "lost two"}],
            user=self.owner,
        )
        adj = StockAdjustment.objects.get(transfer=t)
        self.assertEqual(adj.reason, StockAdjustment.Reason.SHORTAGE)
        self.assertRegex(adj.reference, r"^ADJ-\d{4}$")
        # Manual API list shows it too (read-only history).
        client = self.login_as(self.owner)
        res = client.get("/api/stock-adjustments/")
        refs = [a["reference"] for a in res.data]
        self.assertIn(adj.reference, refs)


# ---------------------------------------------------------------------------
# Stage 7 — Ledger reconciliation
# ---------------------------------------------------------------------------


class LedgerReconciliationTests(InventoryFixtureMixin, TestCase):
    def assert_levels_match_ledger(self):
        """Every StockLevel must equal the signed sum of its ledger rows."""
        from django.db.models import F, Sum

        levels = list(StockLevel.objects.all())
        self.assertTrue(levels, "expected stock levels to exist")
        for level in levels:
            total = (
                StockMovement.objects.filter(
                    shop=level.shop, variant=level.variant
                )
                .aggregate(total=Sum(F("quantity") * F("direction")))["total"]
                or Decimal("0")
            )
            self.assertEqual(
                Decimal(str(total)),
                level.quantity,
                f"Ledger mismatch at {level.shop.code} / "
                f"{level.variant.product.code}",
            )

    def test_full_scenario_reconciles(self):
        """Receipts, transfers, shortage, sales + corrections, returns and
        every adjustment type end with levels == ledger."""
        from sales.services import correct_sale, create_sale

        # 1. Stock-in 25.5 at the warehouse.
        create_receipt(
            shop=self.warehouse,
            lines=[{"variant": self.variant,
                    "quantity": Decimal("25.500")}],
            user=self.owner,
        )

        # 2. Transfer 10 warehouse -> shop, fully received.
        t1 = StockTransfer.objects.create(
            reference=next_transfer_code(),
            source=self.warehouse, destination=self.shop,
            note="restock", created_by=self.owner,
        )
        StockTransferItem.objects.create(
            transfer=t1, variant=self.variant, quantity=Decimal("10")
        )
        dispatch_transfer(t1, user=self.owner)
        receive_transfer(
            t1,
            [{"item": t1.items.get(),
              "received_quantity": Decimal("10"),
              "shortage_reason": ""}],
            user=self.owner,
        )
        self.assertEqual(self.level_qty(self.shop), Decimal("10"))

        # 3. Manual adjustments of each type.
        manager = self.login_as(self.manager)
        owner = self.login_as(self.owner)
        r = manager.post(
            "/api/stock-adjustments/",
            {"shop": self.shop.id, "variant": self.variant.id,
             "quantity": "2", "reason": "DAMAGED",
             "note": "crushed"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        r = manager.post(
            "/api/stock-adjustments/",
            {"shop": self.shop.id, "variant": self.variant.id,
             "quantity": "1", "reason": "MISSING", "note": "lost"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        r = owner.post(
            "/api/stock-adjustments/",
            {"shop": self.warehouse.id, "variant": self.variant.id,
             "quantity": "5", "reason": "CORRECTION", "direction": 1,
             "note": "found extra pallet"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        r = owner.post(
            "/api/stock-adjustments/",
            {"shop": self.shop.id, "variant": self.variant.id,
             "quantity": "3", "reason": "CORRECTION", "direction": -1,
             "note": "miscount"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.level_qty(self.shop), Decimal("4"))
        self.assertEqual(self.level_qty(self.warehouse), Decimal("20.5"))

        # 4. Returns: one accepted (+2), one rejected (no change).
        r = manager.post(
            "/api/returns/",
            {"shop": self.shop.id, "variant": self.variant.id,
             "quantity": "2", "reason": "customer return"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        r2 = manager.post(
            f"/api/returns/{r.data['id']}/accept/", format="json"
        )
        self.assertEqual(r2.status_code, status.HTTP_200_OK)
        r = manager.post(
            "/api/returns/",
            {"shop": self.shop.id, "variant": self.variant.id,
             "quantity": "9", "reason": "not ours"},
            format="json",
        )
        manager.post(f"/api/returns/{r.data['id']}/reject/", format="json")
        self.assertEqual(self.level_qty(self.shop), Decimal("6"))

        # 5. Second transfer with a declared shortage (auto SHORTAGE
        #    adjustment written off from in-transit).
        t2 = StockTransfer.objects.create(
            reference=next_transfer_code(),
            source=self.warehouse, destination=self.shop,
            note="short shipment", created_by=self.owner,
        )
        StockTransferItem.objects.create(
            transfer=t2, variant=self.variant, quantity=Decimal("5")
        )
        dispatch_transfer(t2, user=self.owner)
        receive_transfer(
            t2,
            [{"item": t2.items.get(),
              "received_quantity": Decimal("3"),
              "shortage_reason": "two lost"}],
            user=self.owner,
        )
        self.assertEqual(self.level_qty(self.shop), Decimal("9"))

        # 6. Sale + correction (SALE_OUT / SALE_REVERSAL rows).
        sale, _ = create_sale(
            shop=self.shop,
            lines=[{"variant": self.variant, "quantity": Decimal("1")}],
            user=self.manager,
        )
        correct_sale(
            sale,
            lines=[{"variant": self.variant, "quantity": Decimal("2")}],
            reason="wrong quantity",
            user=self.owner,
        )
        self.assertEqual(self.level_qty(self.shop), Decimal("7"))

        # 7. THE assertion: levels equal signed ledger sums everywhere.
        self.assert_levels_match_ledger()

        # Sanity on kinds so the reconciliation is not vacuous.
        kinds = set(
            StockMovement.objects.values_list("kind", flat=True)
        )
        for expected in (
            StockMovement.Kind.STOCK_IN,
            StockMovement.Kind.DISPATCH_OUT,
            StockMovement.Kind.DISPATCH_IN,
            StockMovement.Kind.RECEIVE_OUT,
            StockMovement.Kind.RECEIVE_IN,
            StockMovement.Kind.SHORTAGE,
            StockMovement.Kind.ADJUSTMENT,
            StockMovement.Kind.RETURN_IN,
            StockMovement.Kind.SALE_OUT,
            StockMovement.Kind.SALE_REVERSAL,
        ):
            self.assertIn(expected, kinds)

        # In-transit fully cleared (received + shortage accounted).
        in_transit = Shop.objects.get(code="in-transit")
        self.assertEqual(
            self.level_qty(in_transit), Decimal("0")
        )

    def test_each_adjustment_type_writes_matching_ledger_row(self):
        # Seed through a real receipt so the opening balance is itself
        # in the ledger (seed_stock() bypasses it and would never match).
        create_receipt(
            shop=self.warehouse,
            lines=[{"variant": self.variant,
                    "quantity": Decimal("100")}],
            user=self.owner,
        )
        owner = self.login_as(self.owner)
        cases = [
            {"reason": "DAMAGED", "note": "bent", "direction": -1},
            {"reason": "MISSING", "note": "gone", "direction": -1},
            {"reason": "CORRECTION", "note": "up", "direction": 1},
            {"reason": "CORRECTION", "note": "down", "direction": -1},
            {"reason": "OTHER", "note": "misc", "direction": -1},
        ]
        for case in cases:
            res = owner.post(
                "/api/stock-adjustments/",
                {"shop": self.warehouse.id, "variant": self.variant.id,
                 "quantity": "1", **case},
                format="json",
            )
            self.assertEqual(
                res.status_code, status.HTTP_201_CREATED, res.data
            )
            move = StockMovement.objects.get(
                transaction_id=res.data["reference"]
            )
            self.assertEqual(move.kind, StockMovement.Kind.ADJUSTMENT)
            self.assertEqual(move.direction, case["direction"])
            self.assertEqual(
                StockAdjustment.objects.get(
                    reference=res.data["reference"]
                ).direction,
                case["direction"],
            )
        self.assert_levels_match_ledger()
