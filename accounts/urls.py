from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from .views import (
    EmployeeShopAssignmentViewSet,
    LoginView,
    LogoutView,
    MeView,
    ShopViewSet,
    UserViewSet,
)

router = DefaultRouter()
router.register("users", UserViewSet, basename="user")
router.register("shops", ShopViewSet, basename="shop")
router.register(
    "shop-assignments", EmployeeShopAssignmentViewSet, basename="shop-assignment"
)

urlpatterns = [
    path("auth/login/", LoginView.as_view(), name="api-login"),
    path("auth/login/refresh/", TokenRefreshView.as_view(), name="api-login-refresh"),
    path("auth/logout/", LogoutView.as_view(), name="api-logout"),
    path("auth/me/", MeView.as_view(), name="api-me"),
    path("", include(router.urls)),
]
