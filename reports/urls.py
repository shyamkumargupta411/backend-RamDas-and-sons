from django.urls import path

from .views import (
    DashboardView,
    LowStockThresholdViewSet,
    ReportExcelView,
    ReportView,
    BulkUploadPreviewView,
    BulkUploadConfirmView,
)

urlpatterns = [
    path("dashboard/", DashboardView.as_view(), name="report-dashboard"),
    path("reports/", ReportView.as_view(), name="report-list"),
    path("reports/excel/", ReportExcelView.as_view(), name="report-excel"),
    path(
        "thresholds/",
        LowStockThresholdViewSet.as_view(),
        name="threshold-list",
    ),
    path(
        "thresholds/<int:pk>/",
        LowStockThresholdViewSet.as_view(),
        name="threshold-detail",
    ),
    path(
        "reports/bulk-upload/preview/",
        BulkUploadPreviewView.as_view(),
        name="bulk-upload-preview",
    ),
    path(
        "reports/bulk-upload/confirm/",
        BulkUploadConfirmView.as_view(),
        name="bulk-upload-confirm",
    ),
]