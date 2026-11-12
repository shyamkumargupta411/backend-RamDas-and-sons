import json
from datetime import timedelta
from decimal import Decimal
from threading import Barrier, Thread

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import OperationalError, close_old_connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import EmployeeShopAssignment, Shop
from catalog.models import Product, ProductCategory, ProductVariant, Unit
from inventory.models import StockLevel, StockMovement

from .models import Sale, SaleAttachment
from .services import create_sale

User = get_user_model()


class SalesFixtureMixin:
    """One user per role, three locations, stock at each."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username="sal_owner", password="Str0ngPass!xyz", role=User.Role.OWNER
        )
        self.manager = User.objects.create_user(
            username="sal_mgr", password="Str0ngPass!xyz", role=User.Role.MANAGER
        )
        self.seller = User.objects.create_user(
            username="sal_seller", password="Str0ngPass!xyz",
            role=User.Role.SELLER,
        )
        self.seller2 = User.objects.create_user(
            username="sal_seller2", password="Str0ngPass!xyz",
            role=User.Role.SELLER,
        )

        self.warehouse, _ = Shop.objects.get_or_create(
            code="sal-warehouse",
            defaults={"name": "Sal Warehouse", "shop_type": "WAREHOUSE"},
        )
        self.shop, _ = Shop.objects.get_or_create(
            code="sal-shop", defaults={"name": "Sal Shop", "shop_type": "SHOP"}
        )
        self.other_shop, _ = Shop.objects.get_or_create(
            code="sal-other",
            defaults={"name": "Sal Other Shop", "shop_type": "SHOP"},
        )

        today = timezone.localdate()
        EmployeeShopAssignment.objects.create(
            employee=self.seller, shop=self.shop,
            assigned_from=today, created_by=self.owner,
        )
        EmployeeShopAssignment.objects.create(
            employee=self.seller2, shop=self.other_shop,
            assigned_from=today, created_by=self.owner,
        )

        self.category, _ = ProductCategory.objects.get_or_create(
            code="metal-ingots",
            defaults={"name": "Metal Ingots", "form_factor": "INGOT"},
        )
        self.kg, _ = Unit.objects.get_or_create(
            code="KG", defaults={"name": "Kilogram", "unit_type": "WEIGHT"}
        )
        self.piece, _ = Unit.objects.get_or_create(
            code="PIECE", defaults={"name": "Piece", "unit_type": "COUNT"}
        )
        self.product = Product.objects.create(
            code="SAL-PROD-1",
            name="Sale Ingot",
            category=self.category,
            material="MS",
            created_by=self.owner,
        )
        self.variant = ProductVariant.objects.create(
            product=self.product, weight=Decimal("5"), unit=self.kg
        )

        StockLevel.objects.create(
            shop=self.shop, variant=self.variant, quantity=Decimal("50")
        )
        StockLevel.objects.create(
            shop=self.other_shop, variant=self.variant, quantity=Decimal("30")
        )
        StockLevel.objects.create(
            shop=self.warehouse, variant=self.variant, quantity=Decimal("100")
        )

    def login_as(self, user):
        client = APIClient()
        token = RefreshToken.for_user(user).access_token
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def sale_payload(self, **overrides):
        data = {
            "shop": self.shop.id,
            "customer_name": "Walk-in",
            "lines": [{"variant": self.variant.id, "quantity": "5"}],
        }
        data.update(overrides)
        return data

    def qty(self, shop, variant=None):
        return (
            StockLevel.objects.filter(
                shop=shop, variant=variant or self.variant
            )
            .values_list("quantity", flat=True)
            .first()
            or Decimal("0")
        )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class SaleCreateTests(SalesFixtureMixin, TestCase):
    def test_seller_creates_sale_at_own_shop(self):
        client = self.login_as(self.seller)
        res = client.post("/api/sales/", self.sale_payload(), format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        body = res.json()
        self.assertRegex(body["reference"], r"^SALE-\d{6}$")
        self.assertEqual(body["status"], "COMPLETED")
        self.assertEqual(body["shop_name"], "Sal Shop")
        self.assertEqual(body["employee_username"], "sal_seller")
        self.assertEqual(body["customer_name"], "Walk-in")
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["quantity"], "5.000")
        # entered unit stored (defaults to the variant unit)
        self.assertEqual(body["items"][0]["unit_code"], "KG")

        self.assertEqual(self.qty(self.shop), Decimal("45"))

        # Ledger: SALE_OUT rows share the sale reference.
        moves = StockMovement.objects.filter(
            transaction_id=body["reference"], kind="SALE_OUT"
        )
        self.assertEqual(moves.count(), 1)
        move = moves.first()
        self.assertEqual(move.shop_id, self.shop.id)
        self.assertEqual(move.direction, -1)
        self.assertEqual(move.quantity, Decimal("5"))
        self.assertEqual(move.created_by_id, self.seller.id)

    def test_seller_cannot_sale_at_other_shop(self):
        client = self.login_as(self.seller)
        res = client.post(
            "/api/sales/",
            self.sale_payload(shop=self.other_shop.id),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(self.qty(self.other_shop), Decimal("30"))

    def test_manager_can_sale_at_any_location(self):
        client = self.login_as(self.manager)
        res = client.post(
            "/api/sales/",
            self.sale_payload(shop=self.warehouse.id),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        self.assertEqual(res.json()["employee_username"], "sal_mgr")
        self.assertEqual(self.qty(self.warehouse), Decimal("95"))

    def test_insufficient_stock_blocked(self):
        client = self.login_as(self.seller)
        res = client.post(
            "/api/sales/",
            self.sale_payload(
                lines=[{"variant": self.variant.id, "quantity": "999"}]
            ),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(self.qty(self.shop), Decimal("50"))
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_duplicate_variant_lines_rejected(self):
        client = self.login_as(self.seller)
        res = client.post(
            "/api/sales/",
            self.sale_payload(
                lines=[
                    {"variant": self.variant.id, "quantity": "1"},
                    {"variant": self.variant.id, "quantity": "2"},
                ]
            ),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Sale.objects.count(), 0)

    def test_empty_lines_rejected(self):
        client = self.login_as(self.seller)
        res = client.post(
            "/api/sales/", self.sale_payload(lines=[]), format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_zero_quantity_rejected(self):
        client = self.login_as(self.seller)
        res = client.post(
            "/api/sales/",
            self.sale_payload(
                lines=[{"variant": self.variant.id, "quantity": "0"}]
            ),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_entered_unit_stored(self):
        client = self.login_as(self.seller)
        res = client.post(
            "/api/sales/",
            self.sale_payload(
                lines=[
                    {
                        "variant": self.variant.id,
                        "quantity": "2",
                        "unit": self.piece.id,
                    }
                ]
            ),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        item = res.json()["items"][0]
        self.assertEqual(item["unit_code"], "PIECE")
        self.assertEqual(item["variant_unit_code"], "KG")

    def test_anonymous_401(self):
        client = APIClient()
        res = client.post("/api/sales/", self.sale_payload(), format="json")
        self.assertIn(
            res.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )


# ---------------------------------------------------------------------------
# Duplicate request (idempotency)
# ---------------------------------------------------------------------------


class SaleIdempotencyTests(SalesFixtureMixin, TestCase):
    def test_duplicate_request_same_key_deducts_once(self):
        client = self.login_as(self.seller)
        payload = self.sale_payload(idempotency_key="dup-token-1")

        res1 = client.post("/api/sales/", payload, format="json")
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED, res1.content)
        res2 = client.post("/api/sales/", payload, format="json")
        self.assertEqual(res2.status_code, status.HTTP_200_OK, res2.content)

        self.assertEqual(res1.json()["reference"], res2.json()["reference"])
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(self.qty(self.shop), Decimal("45"))  # deducted once
        self.assertEqual(
            StockMovement.objects.filter(kind="SALE_OUT").count(), 1
        )

    def test_different_keys_create_separate_sales(self):
        client = self.login_as(self.seller)
        res1 = client.post(
            "/api/sales/",
            self.sale_payload(idempotency_key="key-a"),
            format="json",
        )
        res2 = client.post(
            "/api/sales/",
            self.sale_payload(idempotency_key="key-b"),
            format="json",
        )
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res2.status_code, status.HTTP_201_CREATED)
        self.assertNotEqual(
            res1.json()["reference"], res2.json()["reference"]
        )
        self.assertEqual(Sale.objects.count(), 2)
        self.assertEqual(self.qty(self.shop), Decimal("40"))


# ---------------------------------------------------------------------------
# Seller isolation / no edit / no delete
# ---------------------------------------------------------------------------


class SaleScopeTests(SalesFixtureMixin, TestCase):
    def _sale_at(self, shop, user, qty="3"):
        client = self.login_as(user)
        res = client.post(
            "/api/sales/",
            self.sale_payload(shop=shop.id,
                              lines=[{"variant": self.variant.id,
                                      "quantity": qty}]),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        return res.json()

    def test_seller_sees_only_own_shop_sales(self):
        mine = self._sale_at(self.shop, self.seller)
        self._sale_at(self.other_shop, self.seller2)

        client = self.login_as(self.seller)
        res = client.get("/api/sales/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        rows = res.json()
        rows = rows if isinstance(rows, list) else rows.get("results", [])
        refs = [r["reference"] for r in rows]
        self.assertEqual(refs, [mine["reference"]])

    def test_seller_cannot_retrieve_other_shop_sale(self):
        other = self._sale_at(self.other_shop, self.seller2)
        client = self.login_as(self.seller)
        res = client.get(f"/api/sales/{other['id']}/")
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_nobody_can_patch_or_delete(self):
        mine = self._sale_at(self.shop, self.seller)
        for user in (self.seller, self.manager, self.owner):
            client = self.login_as(user)
            res = client.patch(
                f"/api/sales/{mine['id']}/", {"customer_name": "X"},
                format="json",
            )
            self.assertEqual(res.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
            res = client.delete(f"/api/sales/{mine['id']}/")
            self.assertEqual(res.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(self.qty(self.shop), Decimal("47"))

    def test_seller_cannot_correct(self):
        mine = self._sale_at(self.shop, self.seller)
        client = self.login_as(self.seller)
        res = client.post(
            f"/api/sales/{mine['id']}/correct/",
            {"reason": "wrong qty", "lines": [
                {"variant": self.variant.id, "quantity": "1"}
            ]},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            Sale.objects.get(pk=mine["id"]).status, "COMPLETED"
        )


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------


class SaleCorrectionTests(SalesFixtureMixin, TestCase):
    def _sale(self, qty="5", user=None):
        client = self.login_as(user or self.seller)
        res = client.post(
            "/api/sales/",
            self.sale_payload(
                lines=[{"variant": self.variant.id, "quantity": qty}]
            ),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        return res.json()

    def test_correction_reverses_and_creates_linked_sale(self):
        original = self._sale("5")
        self.assertEqual(self.qty(self.shop), Decimal("45"))

        client = self.login_as(self.manager)
        res = client.post(
            f"/api/sales/{original['id']}/correct/",
            {
                "reason": "entered wrong quantity",
                "lines": [{"variant": self.variant.id, "quantity": "3"}],
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        corrected = res.json()

        # Original marked CORRECTED; new sale linked back with the reason.
        orig = Sale.objects.get(pk=original["id"])
        self.assertEqual(orig.status, Sale.Status.CORRECTED)
        self.assertEqual(orig.corrected_by_id, self.manager.id)
        self.assertIsNotNone(orig.corrected_at)

        self.assertEqual(corrected["corrects_reference"], orig.reference)
        self.assertEqual(
            corrected["correction_reason"], "entered wrong quantity"
        )
        self.assertEqual(corrected["created_by_username"], "sal_mgr")
        # Same shop and employee attribution kept.
        self.assertEqual(corrected["shop"], orig.shop_id)
        self.assertEqual(corrected["employee"], orig.employee_id)

        # Stock: -5 (original) +5 (reversal) -3 (corrected) = -3 net.
        self.assertEqual(self.qty(self.shop), Decimal("47"))

        # Ledger: reversal shares the ORIGINAL reference.
        rev = StockMovement.objects.filter(
            transaction_id=orig.reference, kind="SALE_REVERSAL"
        )
        self.assertEqual(rev.count(), 1)
        self.assertEqual(rev.first().direction, 1)
        # Corrected sale's deduction shares the NEW reference.
        out = StockMovement.objects.filter(
            transaction_id=corrected["reference"], kind="SALE_OUT"
        )
        self.assertEqual(out.count(), 1)
        self.assertEqual(out.first().quantity, Decimal("3"))

    def test_correction_requires_reason(self):
        original = self._sale()
        client = self.login_as(self.manager)
        for reason in ("", "   "):
            res = client.post(
                f"/api/sales/{original['id']}/correct/",
                {
                    "reason": reason,
                    "lines": [{"variant": self.variant.id, "quantity": "2"}],
                },
                format="json",
            )
            self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(Sale.objects.get().status, "COMPLETED")
        self.assertEqual(self.qty(self.shop), Decimal("45"))

    def test_owner_can_correct(self):
        original = self._sale()
        client = self.login_as(self.owner)
        res = client.post(
            f"/api/sales/{original['id']}/correct/",
            {
                "reason": "owner fix",
                "lines": [{"variant": self.variant.id, "quantity": "4"}],
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        self.assertEqual(res.json()["created_by_username"], "sal_owner")

    def test_cannot_correct_already_corrected(self):
        original = self._sale()
        client = self.login_as(self.manager)
        res1 = client.post(
            f"/api/sales/{original['id']}/correct/",
            {
                "reason": "first fix",
                "lines": [{"variant": self.variant.id, "quantity": "2"}],
            },
            format="json",
        )
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)
        res2 = client.post(
            f"/api/sales/{original['id']}/correct/",
            {
                "reason": "second fix",
                "lines": [{"variant": self.variant.id, "quantity": "1"}],
            },
            format="json",
        )
        self.assertEqual(res2.status_code, status.HTTP_400_BAD_REQUEST)

    def test_correction_chain_history_kept(self):
        # A -> B -> C chain with stock math at every step.
        client = self.login_as(self.seller)
        a = self._sale("5")                       # 50 - 5 = 45
        mgr = self.login_as(self.manager)
        b_res = mgr.post(
            f"/api/sales/{a['id']}/correct/",
            {
                "reason": "wrong item qty",
                "lines": [{"variant": self.variant.id, "quantity": "3"}],
            },
            format="json",
        )                                          # +5 -3 -> 47
        self.assertEqual(b_res.status_code, status.HTTP_201_CREATED)
        b = b_res.json()
        c_res = mgr.post(
            f"/api/sales/{b['id']}/correct/",
            {
                "reason": "customer swapped size",
                "lines": [{"variant": self.variant.id, "quantity": "10"}],
            },
            format="json",
        )                                          # +3 -10 -> 40
        self.assertEqual(c_res.status_code, status.HTTP_201_CREATED)
        c = c_res.json()
        self.assertEqual(self.qty(self.shop), Decimal("40"))

        # Chain endpoint data on C: A, B, C in order.
        chain = c["correction_chain"]
        self.assertEqual(
            [x["reference"] for x in chain],
            [a["reference"], b["reference"], c["reference"]],
        )
        self.assertEqual(
            [x["status"] for x in chain],
            ["CORRECTED", "CORRECTED", "COMPLETED"],
        )

        # B.corrects = A, C.corrects = B — history queryable end to end.
        b_obj = Sale.objects.get(reference=b["reference"])
        c_obj = Sale.objects.get(reference=c["reference"])
        self.assertEqual(b_obj.corrects_id, a["id"])
        self.assertEqual(c_obj.corrects_id, b_obj.id)
        self.assertEqual(
            [s.reference for s in c_obj.correction_chain],
            [a["reference"], b["reference"], c["reference"]],
        )

    def test_correction_cannot_exceed_stock(self):
        original = self._sale("5")
        mgr = self.login_as(self.manager)
        res = mgr.post(
            f"/api/sales/{original['id']}/correct/",
            {
                "reason": "oops",
                "lines": [{"variant": self.variant.id, "quantity": "999"}],
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        # Original untouched, no new sale, stock unchanged by the attempt.
        orig = Sale.objects.get(pk=original["id"])
        self.assertEqual(orig.status, "COMPLETED")
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(self.qty(self.shop), Decimal("45"))


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


class SaleFilterTests(SalesFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        client = self.login_as(self.seller)
        client.post("/api/sales/", self.sale_payload(), format="json")
        mgr = self.login_as(self.manager)
        mgr.post(
            "/api/sales/",
            self.sale_payload(
                shop=self.other_shop.id,
                customer_name="Other customer",
                lines=[{"variant": self.variant.id, "quantity": "2"}],
            ),
            format="json",
        )

    def _rows(self, res):
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        rows = res.json()
        return rows if isinstance(rows, list) else rows.get("results", [])

    def test_filter_by_shop(self):
        client = self.login_as(self.owner)
        rows = self._rows(client.get(f"/api/sales/?shop={self.shop.id}"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["shop"], self.shop.id)

        rows = self._rows(client.get(f"/api/sales/?shop={self.other_shop.id}"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["shop"], self.other_shop.id)

    def test_filter_by_employee(self):
        client = self.login_as(self.owner)
        rows = self._rows(
            client.get(f"/api/sales/?employee={self.seller.id}")
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["employee"], self.seller.id)

        rows = self._rows(
            client.get(f"/api/sales/?employee={self.manager.id}")
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["employee"], self.manager.id)

    def test_filter_by_product(self):
        other_product = Product.objects.create(
            code="SAL-PROD-2",
            name="Other Ingot",
            category=self.category,
            material="SS",
            created_by=self.owner,
        )
        other_variant = ProductVariant.objects.create(
            product=other_product, weight=Decimal("10"), unit=self.kg
        )
        StockLevel.objects.create(
            shop=self.shop, variant=other_variant, quantity=Decimal("20")
        )
        mgr = self.login_as(self.manager)
        mgr.post(
            "/api/sales/",
            self.sale_payload(
                shop=self.shop.id,
                lines=[{"variant": other_variant.id, "quantity": "1"}],
            ),
            format="json",
        )

        client = self.login_as(self.owner)
        rows = self._rows(
            client.get(f"/api/sales/?product={other_product.id}")
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["items"][0]["product_code"], "SAL-PROD-2")

        rows = self._rows(client.get(f"/api/sales/?product={self.product.id}"))
        self.assertEqual(len(rows), 2)

    def test_filter_by_date_range(self):
        client = self.login_as(self.owner)
        today = timezone.localdate()
        yesterday = today - timedelta(days=1)
        tomorrow = today + timedelta(days=1)

        rows = self._rows(
            client.get(
                f"/api/sales/?date_from={yesterday}&date_to={tomorrow}"
            )
        )
        self.assertEqual(len(rows), 2)

        rows = self._rows(client.get(f"/api/sales/?date_from={tomorrow}"))
        self.assertEqual(len(rows), 0)

        rows = self._rows(
            client.get(f"/api/sales/?date_to={yesterday}")
        )
        self.assertEqual(len(rows), 0)

    def test_search_by_reference_and_customer(self):
        client = self.login_as(self.owner)
        rows = self._rows(client.get("/api/sales/?search=Other customer"))
        self.assertEqual(len(rows), 1)
        rows = self._rows(client.get("/api/sales/?search=SALE-000001"))
        self.assertEqual(len(rows), 1)

    def test_employees_action(self):
        client = self.login_as(self.owner)
        res = client.get("/api/sales/employees/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        usernames = {e["username"] for e in res.json()}
        self.assertEqual(usernames, {"sal_seller", "sal_mgr"})


# ---------------------------------------------------------------------------
# Slip attachments
# ---------------------------------------------------------------------------


class SaleAttachmentTests(SalesFixtureMixin, TestCase):
    def _multipart(self, slip=None, **overrides):
        data = {
            "shop": str(self.shop.id),
            "customer_name": "Cash customer",
            "lines": json.dumps(
                [{"variant": self.variant.id, "quantity": "1"}]
            ),
        }
        data.update(overrides)
        if slip is not None:
            data["slip"] = slip
        return data

    def test_valid_slip_upload(self):
        client = self.login_as(self.seller)
        slip = SimpleUploadedFile(
            "slip.jpg", b"\xff\xd8fakejpeg", content_type="image/jpeg"
        )
        res = client.post(
            "/api/sales/", self._multipart(slip=slip), format="multipart"
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        body = res.json()
        self.assertEqual(len(body["attachments"]), 1)
        att = body["attachments"][0]
        self.assertEqual(att["content_type"], "image/jpeg")
        self.assertEqual(att["size_bytes"], len(b"\xff\xd8fakejpeg"))
        self.assertIn("/media/sales/slips/", att["file"])

        sale = Sale.objects.get(reference=body["reference"])
        self.assertEqual(sale.attachments.count(), 1)
        self.assertEqual(self.qty(self.shop), Decimal("49"))

    def test_wrong_type_rejected(self):
        client = self.login_as(self.seller)
        slip = SimpleUploadedFile(
            "slip.txt", b"not an image", content_type="text/plain"
        )
        res = client.post(
            "/api/sales/", self._multipart(slip=slip), format="multipart"
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(self.qty(self.shop), Decimal("50"))

    def test_oversize_rejected(self):
        client = self.login_as(self.seller)
        big = SimpleUploadedFile(
            "big.jpg",
            b"x" * (SaleAttachment.MAX_SIZE + 1),
            content_type="image/jpeg",
        )
        res = client.post(
            "/api/sales/", self._multipart(slip=big), format="multipart"
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Sale.objects.count(), 0)

    def test_multipart_without_slip(self):
        client = self.login_as(self.seller)
        res = client.post(
            "/api/sales/", self._multipart(), format="multipart"
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        self.assertEqual(res.json()["attachments"], [])


# ---------------------------------------------------------------------------
# Concurrency: two sales race for the last unit
# ---------------------------------------------------------------------------


class ConcurrentSaleTests(SalesFixtureMixin, TransactionTestCase):
    def test_concurrent_sales_of_last_unit(self):
        StockLevel.objects.filter(
            shop=self.shop, variant=self.variant
        ).update(quantity=Decimal("1"))

        barrier = Barrier(2)
        results = {"ok": 0, "fail": 0, "errors": []}

        def worker():
            # SQLite shared-cache returns SQLITE_LOCKED ("database table
            # is locked") instead of waiting like Postgres would — retry
            # briefly to simulate blocking on the row lock.
            from time import sleep

            try:
                barrier.wait(timeout=10)
                last_exc = None
                for attempt in range(40):
                    try:
                        create_sale(
                            shop=self.shop,
                            lines=[{"variant": self.variant,
                                    "quantity": Decimal("1")}],
                            user=self.seller,
                            employee=self.seller,
                        )
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

        threads = [Thread(target=worker) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)

        # Exactly one sale won the last unit.
        self.assertEqual(results["ok"], 1, results)
        self.assertEqual(results["fail"], 1, results)

        # Stock never went negative; exactly one sale recorded.
        self.assertEqual(self.qty(self.shop), Decimal("0"))
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(
            StockMovement.objects.filter(kind="SALE_OUT").count(), 1
        )
