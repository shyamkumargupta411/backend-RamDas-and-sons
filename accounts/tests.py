from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from .models import EmployeeShopAssignment, Shop

User = get_user_model()


class HealthEndpointTests(TestCase):
    def test_health_is_public_and_ok(self):
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["database"], "ok")
        self.assertEqual(body["service"], "metalinv-api")
        self.assertIn("time", body)

    def test_health_live_is_public_and_ok(self):
        response = self.client.get(reverse("health-live"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")


class UserModelTests(TestCase):
    def test_default_role_is_seller(self):
        user = User.objects.create_user(
            username="seller1", password="s3cret-test-pass"
        )
        self.assertEqual(user.role, User.Role.SELLER)
        self.assertFalse(user.can_manage_products)

    def test_owner_role_round_trips(self):
        user = User.objects.create_user(
            username="owner1",
            password="s3cret-test-pass",
            role=User.Role.OWNER,
        )
        self.assertEqual(user.role, "OWNER")


class AuthFlowTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="owner_login",
            password="Str0ngPass!xyz",
            role=User.Role.OWNER,
        )

    def test_login_returns_tokens_and_user(self):
        resp = self.client.post(
            reverse("api-login"),
            {"username": "owner_login", "password": "Str0ngPass!xyz"},
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertIn("access", data)
        self.assertIn("refresh", data)
        self.assertEqual(data["user"]["role"], "OWNER")
        self.assertEqual(data["user"]["username"], "owner_login")

    def test_login_rejects_bad_password(self):
        resp = self.client.post(
            reverse("api-login"),
            {"username": "owner_login", "password": "wrong"},
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_refresh_rotates_access(self):
        login = self.client.post(
            reverse("api-login"),
            {"username": "owner_login", "password": "Str0ngPass!xyz"},
        ).json()
        resp = self.client.post(
            reverse("api-login-refresh"), {"refresh": login["refresh"]}
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn("access", resp.json())

    def test_logout_blacklists_refresh(self):
        login = self.client.post(
            reverse("api-login"),
            {"username": "owner_login", "password": "Str0ngPass!xyz"},
        ).json()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {login['access']}")
        resp = self.client.post(reverse("api-logout"), {"refresh": login["refresh"]})
        self.assertIn(
            resp.status_code,
            (status.HTTP_205_RESET_CONTENT, status.HTTP_200_OK),
        )
        # Refreshed token must now fail.
        refresh_resp = self.client.post(
            reverse("api-login-refresh"), {"refresh": login["refresh"]}
        )
        self.assertEqual(refresh_resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_me_requires_auth(self):
        resp = self.client.get(reverse("api-me"))
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_me_returns_profile(self):
        token = str(RefreshToken.for_user(self.user).access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        resp = self.client.get(reverse("api-me"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()["username"], "owner_login")
        self.assertEqual(resp.json()["role"], "OWNER")


class PermissionMatrixBase(APITestCase):
    """Shared fixtures: one user per role + two shops + seller assignment."""

    def setUp(self):
        self.owner = User.objects.create_user(
            username="owner1",
            password="Str0ngPass!xyz",
            role=User.Role.OWNER,
        )
        self.manager = User.objects.create_user(
            username="manager1",
            password="Str0ngPass!xyz",
            role=User.Role.MANAGER,
        )
        self.manager_products = User.objects.create_user(
            username="manager_products",
            password="Str0ngPass!xyz",
            role=User.Role.MANAGER,
            can_manage_products=True,
        )
        self.seller = User.objects.create_user(
            username="seller1",
            password="Str0ngPass!xyz",
            role=User.Role.SELLER,
        )
        self.seller_other = User.objects.create_user(
            username="seller2",
            password="Str0ngPass!xyz",
            role=User.Role.SELLER,
        )

        self.shop_a = Shop.objects.create(
            name="Shop Alpha", code="alpha", created_by=self.owner
        )
        self.shop_b = Shop.objects.create(
            name="Shop Beta", code="beta", created_by=self.owner
        )
        today = timezone.localdate()
        EmployeeShopAssignment.objects.create(
            employee=self.seller,
            shop=self.shop_a,
            assigned_from=today - timedelta(days=30),
            created_by=self.owner,
        )
        EmployeeShopAssignment.objects.create(
            employee=self.seller_other,
            shop=self.shop_b,
            assigned_from=today - timedelta(days=30),
            created_by=self.owner,
        )

    def login_as(self, user):
        self.client = APIClient()
        token = str(RefreshToken.for_user(user).access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return self.client

    def anon(self):
        self.client = APIClient()
        self.client.credentials()
        return self.client


class UserEndpointPermissionTests(PermissionMatrixBase):
    """GET/POST/PATCH /api/users/ - OWNER only."""

    def test_list_users_matrix(self):
        # anonymous -> 401
        resp = self.anon().get(reverse("user-list"))
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

        # owner -> 200
        resp = self.login_as(self.owner).get(reverse("user-list"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        # manager (even with can_manage_products) -> 403
        resp = self.login_as(self.manager).get(reverse("user-list"))
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        resp = self.login_as(self.manager_products).get(reverse("user-list"))
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        # seller -> 403
        resp = self.login_as(self.seller).get(reverse("user-list"))
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_create_user_matrix(self):
        payload = {
            "username": "newuser",
            "password": "Str0ngPass!xyz",
            "role": "SELLER",
            "email": "new@example.com",
        }

        resp = self.anon().post(reverse("user-list"), payload)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

        resp = self.login_as(self.manager).post(reverse("user-list"), payload)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        resp = self.login_as(self.seller).post(reverse("user-list"), payload)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        resp = self.login_as(self.owner).post(reverse("user-list"), payload)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(User.objects.filter(username="newuser").exists())

    def test_update_user_matrix(self):
        payload = {"first_name": "Updated"}

        resp = self.anon().patch(
            reverse("user-detail", args=[self.seller.id]), payload
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

        resp = self.login_as(self.manager).patch(
            reverse("user-detail", args=[self.seller.id]), payload
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        resp = self.login_as(self.seller).patch(
            reverse("user-detail", args=[self.seller.id]), payload
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        resp = self.login_as(self.owner).patch(
            reverse("user-detail", args=[self.seller.id]), payload
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.seller.refresh_from_db()
        self.assertEqual(self.seller.first_name, "Updated")


class ShopEndpointPermissionTests(PermissionMatrixBase):
    """GET /api/shops/ scoped; POST/PATCH owner only."""

    def test_list_shops_matrix(self):
        # anonymous -> 401
        resp = self.anon().get(reverse("shop-list"))
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

        # owner -> 200, sees both shops + seeded warehouse
        resp = self.login_as(self.owner).get(reverse("shop-list"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        codes = {s["code"] for s in resp.json()["results"]}
        self.assertEqual(codes, {"alpha", "beta", "central-warehouse"})

        # manager -> 200, sees both shops + seeded warehouse
        resp = self.login_as(self.manager).get(reverse("shop-list"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        codes = {s["code"] for s in resp.json()["results"]}
        self.assertEqual(codes, {"alpha", "beta", "central-warehouse"})

        # seller -> 200, sees ONLY assigned shop
        resp = self.login_as(self.seller).get(reverse("shop-list"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        codes = {s["code"] for s in resp.json()["results"]}
        self.assertEqual(codes, {"alpha"})

        # other seller -> only beta
        resp = self.login_as(self.seller_other).get(reverse("shop-list"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        codes = {s["code"] for s in resp.json()["results"]}
        self.assertEqual(codes, {"beta"})

    def test_create_shop_matrix(self):
        payload = {"name": "Shop Gamma", "code": "gamma"}

        resp = self.anon().post(reverse("shop-list"), payload)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

        resp = self.login_as(self.manager).post(reverse("shop-list"), payload)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        resp = self.login_as(self.seller).post(reverse("shop-list"), payload)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        resp = self.login_as(self.owner).post(reverse("shop-list"), payload)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Shop.objects.filter(code="gamma").exists())

    def test_update_shop_matrix(self):
        payload = {"name": "Shop Alpha Renamed"}

        resp = self.anon().patch(
            reverse("shop-detail", args=[self.shop_a.id]), payload
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

        resp = self.login_as(self.manager).patch(
            reverse("shop-detail", args=[self.shop_a.id]), payload
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        resp = self.login_as(self.seller).patch(
            reverse("shop-detail", args=[self.shop_a.id]), payload
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        resp = self.login_as(self.owner).patch(
            reverse("shop-detail", args=[self.shop_a.id]), payload
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_seller_cannot_retrieve_unassigned_shop(self):
        resp = self.login_as(self.seller).get(
            reverse("shop-detail", args=[self.shop_b.id])
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_seller_can_retrieve_assigned_shop(self):
        resp = self.login_as(self.seller).get(
            reverse("shop-detail", args=[self.shop_a.id])
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()["code"], "alpha")

    def test_soft_delete_shop_owner_only(self):
        resp = self.login_as(self.manager).delete(
            reverse("shop-detail", args=[self.shop_a.id])
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        resp = self.login_as(self.owner).delete(
            reverse("shop-detail", args=[self.shop_a.id])
        )
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.shop_a.refresh_from_db()
        self.assertTrue(self.shop_a.is_deleted)
        self.assertFalse(self.shop_a.is_active)


class AssignmentEndpointPermissionTests(PermissionMatrixBase):
    def test_assignment_matrix(self):
        payload = {
            "employee": self.seller_other.id,
            "shop": self.shop_a.id,
            "assigned_from": timezone.localdate().isoformat(),
        }

        resp = self.anon().get(reverse("shop-assignment-list"))
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

        resp = self.login_as(self.seller).get(reverse("shop-assignment-list"))
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        resp = self.login_as(self.manager).post(
            reverse("shop-assignment-list"), payload
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        resp = self.login_as(self.seller).post(
            reverse("shop-assignment-list"), payload
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

        resp = self.login_as(self.manager).get(reverse("shop-assignment-list"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        resp = self.login_as(self.owner).post(
            reverse("shop-assignment-list"), payload
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)


class RoleHomeRedirectTests(APITestCase):
    """Sanity: /api/auth/me works for every role."""

    def test_me_for_each_role(self):
        for role, name in [
            (User.Role.OWNER, "u_owner"),
            (User.Role.MANAGER, "u_manager"),
            (User.Role.SELLER, "u_seller"),
        ]:
            user = User.objects.create_user(
                username=name, password="Str0ngPass!xyz", role=role
            )
            client = APIClient()
            token = str(RefreshToken.for_user(user).access_token)
            client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
            resp = client.get(reverse("api-me"))
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            self.assertEqual(resp.json()["role"], role)
