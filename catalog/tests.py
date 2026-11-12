from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import EmployeeShopAssignment, Shop
from .models import Product, ProductCategory, ProductVariant, Unit
from .services import convert_quantity, make_spec_key, next_product_code

User = get_user_model()


class CatalogFixtureMixin:
    def setUp(self):
        self.owner = User.objects.create_user(
            username="cat_owner", password="Str0ngPass!xyz", role=User.Role.OWNER
        )
        self.manager_no = User.objects.create_user(
            username="cat_mgr_no",
            password="Str0ngPass!xyz",
            role=User.Role.MANAGER,
            can_manage_products=False,
        )
        self.manager_yes = User.objects.create_user(
            username="cat_mgr_yes",
            password="Str0ngPass!xyz",
            role=User.Role.MANAGER,
            can_manage_products=True,
        )
        self.seller = User.objects.create_user(
            username="cat_seller", password="Str0ngPass!xyz", role=User.Role.SELLER
        )
        self.category, _ = ProductCategory.objects.get_or_create(
            code="metal-ingots",
            defaults={
                "name": "Metal Ingots",
                "form_factor": "INGOT",
            },
        )
        self.flat_category, _ = ProductCategory.objects.get_or_create(
            code="patiya-flat",
            defaults={
                "name": "Patiya/Flat",
                "form_factor": "FLAT",
            },
        )
        self.kg, _ = Unit.objects.get_or_create(
            code="KG", defaults={"name": "Kilogram", "unit_type": "WEIGHT"}
        )
        self.piece, _ = Unit.objects.get_or_create(
            code="PIECE", defaults={"name": "Piece", "unit_type": "COUNT"}
        )
        self.feet, _ = Unit.objects.get_or_create(
            code="FEET", defaults={"name": "Feet", "unit_type": "LENGTH"}
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


class SeedDataTests(TestCase):
    def test_default_categories_seeded(self):
        codes = set(
            ProductCategory.objects.filter(is_deleted=False).values_list(
                "code", flat=True
            )
        )
        self.assertTrue(
            {"metal-ingots", "patiya-flat", "metal-pipes", "other-metal"}.issubset(
                codes
            )
        )

    def test_default_units_seeded(self):
        codes = set(Unit.objects.values_list("code", flat=True))
        self.assertTrue({"KG", "PIECE", "FEET"}.issubset(codes))

    def test_form_factors(self):
        self.assertEqual(
            ProductCategory.objects.get(code="metal-ingots").form_factor, "INGOT"
        )
        self.assertEqual(
            ProductCategory.objects.get(code="patiya-flat").form_factor, "FLAT"
        )
        self.assertEqual(
            ProductCategory.objects.get(code="metal-pipes").form_factor, "PIPE"
        )
        self.assertEqual(
            ProductCategory.objects.get(code="other-metal").form_factor, "OTHER"
        )


class ProductCodeTests(CatalogFixtureMixin, TestCase):
    def test_codes_are_sequential_and_padded(self):
        p1 = Product.objects.create(
            code=next_product_code(),
            name="Ingot A",
            category=self.category,
            material="MS",
            created_by=self.owner,
        )
        p2 = Product.objects.create(
            code=next_product_code(),
            name="Ingot B",
            category=self.category,
            material="MS",
            created_by=self.owner,
        )
        self.assertRegex(p1.code, r"^PROD-\d{4}$")
        n1 = int(p1.code.split("-")[1])
        n2 = int(p2.code.split("-")[1])
        self.assertEqual(n2, n1 + 1)


class VariantSpecTests(CatalogFixtureMixin, TestCase):
    def _product(self):
        return Product.objects.create(
            code=next_product_code(),
            name="Flat bar",
            category=self.flat_category,
            material="MS",
            created_by=self.owner,
        )

    def test_spec_key_stable(self):
        key = make_spec_key(
            thickness="10", width="50", length="1000", diameter=None,
            weight=None, unit_code="KG",
        )
        key2 = make_spec_key(
            thickness="10.000", width="50.000", length="1000.000",
            diameter=None, weight=None, unit_code="KG",
        )
        self.assertEqual(key, key2)

    def test_duplicate_spec_rejected(self):
        product = self._product()
        ProductVariant.objects.create(
            product=product,
            thickness=10,
            width=50,
            length=1000,
            unit=self.kg,
            created_by=self.owner,
        )
        with self.assertRaises(Exception):
            ProductVariant.objects.create(
                product=product,
                thickness=10,
                width=50,
                length=1000,
                unit=self.kg,
                created_by=self.owner,
            )

    def test_different_sizes_allowed(self):
        product = self._product()
        ProductVariant.objects.create(
            product=product,
            thickness=10,
            width=50,
            length=1000,
            unit=self.kg,
            created_by=self.owner,
        )
        ProductVariant.objects.create(
            product=product,
            thickness=12,
            width=50,
            length=1000,
            unit=self.kg,
            created_by=self.owner,
        )
        self.assertEqual(product.variants.count(), 2)

    def test_variant_requires_a_dimension(self):
        product = self._product()
        variant = ProductVariant(
            product=product, unit=self.kg, thickness=None, width=None,
            length=None, diameter=None, weight=None,
        )
        with self.assertRaises(ValidationError):
            variant.full_clean()


class UnitConversionTests(CatalogFixtureMixin, TestCase):
    def test_same_unit_identity(self):
        product = Product.objects.create(
            code=next_product_code(), name="X", category=self.category,
            material="MS",
        )
        v = ProductVariant.objects.create(
            product=product, unit=self.kg, weight=100,
        )
        self.assertEqual(convert_quantity(v, "50", "KG", "KG"), 50)

    def test_kg_to_g(self):
        product = Product.objects.create(
            code=next_product_code(), name="X", category=self.category,
            material="MS",
        )
        v = ProductVariant.objects.create(product=product, unit=self.kg, weight=1)
        result = convert_quantity(v, "2", "KG", "G")
        self.assertEqual(result, 2000)

    def test_piece_to_kg_via_weight_per_piece(self):
        product = Product.objects.create(
            code=next_product_code(), name="X", category=self.category,
            material="MS",
        )
        v = ProductVariant.objects.create(
            product=product, unit=self.piece, weight=25, weight_per_piece=25,
        )
        self.assertEqual(convert_quantity(v, "4", "PIECE", "KG"), 100)

    def test_kg_to_piece_via_weight_per_piece(self):
        product = Product.objects.create(
            code=next_product_code(), name="X", category=self.category,
            material="MS",
        )
        v = ProductVariant.objects.create(
            product=product, unit=self.piece, weight_per_piece=25,
        )
        self.assertEqual(convert_quantity(v, "100", "KG", "PIECE"), 4)

    def test_piece_to_feet_via_length_per_piece(self):
        product = Product.objects.create(
            code=next_product_code(), name="X", category=self.category,
            material="MS",
        )
        v = ProductVariant.objects.create(
            product=product, unit=self.piece, length_per_piece=12,
        )
        self.assertEqual(convert_quantity(v, "3", "PIECE", "FEET"), 36)

    def test_missing_factor_raises(self):
        product = Product.objects.create(
            code=next_product_code(), name="X", category=self.category,
            material="MS",
        )
        v = ProductVariant.objects.create(product=product, unit=self.piece)
        with self.assertRaises(ValueError):
            convert_quantity(v, "1", "PIECE", "KG")


class ProductAPIPermissionTests(CatalogFixtureMixin, TestCase):
    list_url = reverse("product-list")
    cat_list_url = reverse("product-category-list")

    def _payload(self, **overrides):
        base = {
            "name": "MS Ingot",
            "category": self.category.id,
            "material": "Mild Steel",
            "grade": "Fe500",
            "status": "ACTIVE",
            "variants": [
                {
                    "thickness": None,
                    "width": None,
                    "length": None,
                    "diameter": None,
                    "weight": 25,
                    "unit": "KG",
                }
            ],
        }
        base.update(overrides)
        return base

    def test_anon_get_401(self):
        resp = self.anon().get(self.list_url)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
        resp = self.anon().get(self.cat_list_url)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_seller_can_read_but_not_write(self):
        client = self.login_as(self.seller)
        resp = client.get(self.list_url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        resp = client.get(self.cat_list_url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        resp = client.post(self.list_url, self._payload(), format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        resp = client.post(
            self.cat_list_url, {"name": "X", "code": "x"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_manager_without_flag_cannot_write(self):
        client = self.login_as(self.manager_no)
        resp = client.get(self.list_url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        resp = client.post(self.list_url, self._payload(), format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        resp = client.post(
            self.cat_list_url,
            {"name": "New cat", "code": "new-cat", "form_factor": "OTHER"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_manager_with_flag_can_write(self):
        client = self.login_as(self.manager_yes)
        resp = client.post(self.list_url, self._payload(), format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertTrue(resp.data["code"].startswith("PROD-"))
        resp = client.post(
            self.cat_list_url,
            {"name": "New cat", "code": "new-cat", "form_factor": "PIPE"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)

    def test_owner_can_write(self):
        client = self.login_as(self.owner)
        resp = client.post(self.list_url, self._payload(), format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        code = resp.data["code"]
        self.assertRegex(code, r"^PROD-\d{4}$")

    def test_owner_can_update_product(self):
        client = self.login_as(self.owner)
        created = client.post(self.list_url, self._payload(), format="json").json()
        resp = client.patch(
            reverse("product-detail", args=[created["id"]]),
            {"name": "Renamed Ingot"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["name"], "Renamed Ingot")


class ProductAPITests(CatalogFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client_owner = self.login_as(self.owner)
        self.product = Product.objects.create(
            code=next_product_code(),
            name="MS Flat 50mm",
            category=self.flat_category,
            material="Mild Steel",
            grade="Fe500",
            created_by=self.owner,
        )
        ProductVariant.objects.create(
            product=self.product,
            thickness=10,
            width=50,
            length=1000,
            unit=self.kg,
            created_by=self.owner,
        )

    def test_search_filter(self):
        resp = self.client_owner.get(
            reverse("product-list"), {"search": "Flat"}
        )
        self.assertEqual(resp.status_code, 200)
        results = resp.json()["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["code"], self.product.code)

    def test_filter_by_category(self):
        other_cat, _ = ProductCategory.objects.get_or_create(
            code="metal-pipes",
            defaults={"name": "Metal Pipes", "form_factor": "PIPE"},
        )
        other = Product.objects.create(
            code=next_product_code(),
            name="Pipe item",
            category=other_cat,
            material="MS",
        )
        resp = self.client_owner.get(
            reverse("product-list"), {"category": self.flat_category.id}
        )
        codes = [r["code"] for r in resp.json()["results"]]
        self.assertIn(self.product.code, codes)
        self.assertNotIn(other.code, codes)

    def test_nested_variants_returned(self):
        resp = self.client_owner.get(
            reverse("product-detail", args=[self.product.id])
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data["variants"]), 1)
        self.assertEqual(data["variants"][0]["unit_code"], "KG")
        self.assertEqual(data["category_form_factor"], "FLAT")

    def test_duplicate_variant_in_create_rejected(self):
        payload = {
            "name": "Dup test",
            "category": self.flat_category.id,
            "material": "MS",
            "variants": [
                {"thickness": 5, "width": 40, "length": 6000, "unit": "KG"},
                {"thickness": 5, "width": 40, "length": 6000, "unit": "KG"},
            ],
        }
        resp = self.client_owner.post(
            reverse("product-list"), payload, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_category_search(self):
        resp = self.client_owner.get(
            reverse("product-category-list"), {"search": "Ingots"}
        )
        results = resp.json()["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["code"], "metal-ingots")

    def test_units_endpoint(self):
        resp = self.client_owner.get(reverse("unit-list"))
        self.assertEqual(resp.status_code, 200)
        codes = {u["code"] for u in resp.json()}
        self.assertTrue({"KG", "PIECE", "FEET"}.issubset(codes))
