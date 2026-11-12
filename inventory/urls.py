from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    ProductReturnViewSet,
    StockAdjustmentViewSet,
    StockLevelViewSet,
    StockReceiptViewSet,
    StockTransferViewSet,
)

router = DefaultRouter()
router.register("stock-levels", StockLevelViewSet, basename="stocklevel")
router.register("stock-receipts", StockReceiptViewSet, basename="stockreceipt")
router.register("transfers", StockTransferViewSet, basename="transfer")
router.register("returns", ProductReturnViewSet, basename="return")
router.register(
    "stock-adjustments", StockAdjustmentViewSet, basename="stockadjustment"
)

urlpatterns = [
    path("", include(router.urls)),
]
