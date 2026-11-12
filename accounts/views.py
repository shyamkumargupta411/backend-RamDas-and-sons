from django.contrib.auth import authenticate
from django.db.models import Q
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

from .models import EmployeeShopAssignment, Shop, User
from .permissions import (
    IsOwner,
    IsOwnerReadOnlyOrManager,
    ShopAccess,
)
from .serializers import (
    EmployeeShopAssignmentSerializer,
    LoginSerializer,
    LogoutSerializer,
    ShopSerializer,
    UserSerializer,
)


class LoginRateThrottle(AnonRateThrottle):
    scope = "login"


class LoginView(APIView):
    """POST /api/auth/login/ -> {access, refresh, user}."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [LoginRateThrottle]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = authenticate(
            request,
            username=serializer.validated_data["username"],
            password=serializer.validated_data["password"],
        )
        if user is None or not user.is_active:
            return Response(
                {"detail": "Invalid credentials."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
                "user": UserSerializer(user).data,
            },
            status=status.HTTP_200_OK,
        )


class LogoutView(APIView):
    """POST /api/auth/logout/ - blacklists the refresh token."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        refresh = serializer.validated_data.get("refresh")
        if refresh:
            try:
                RefreshToken(refresh).blacklist()
            except (InvalidToken, TokenError):
                # Already blacklisted / invalid: treat as success (idempotent).
                pass
        return Response(status=status.HTTP_205_RESET_CONTENT)


class MeView(APIView):
    """GET /api/auth/me/ - current user profile."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)


class UserViewSet(viewsets.ModelViewSet):
    """OWNER-only user management."""

    serializer_class = UserSerializer
    permission_classes = [IsOwner]
    queryset = User.objects.all().order_by("username")
    lookup_field = "id"

    def get_queryset(self):
        qs = super().get_queryset()
        search = self.request.query_params.get("search")
        if search:
            qs = qs.filter(
                Q(username__icontains=search)
                | Q(email__icontains=search)
                | Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
            )
        role = self.request.query_params.get("role")
        if role:
            qs = qs.filter(role=role)
        return qs

    def perform_create(self, serializer):
        serializer.save()

    def perform_update(self, serializer):
        serializer.save()


class ShopViewSet(viewsets.ModelViewSet):
    """Shops: read for owner+manager+seller (seller scoped), write owner only."""

    serializer_class = ShopSerializer
    permission_classes = [ShopAccess]
    queryset = Shop.objects.filter(is_deleted=False).order_by("name")
    lookup_field = "id"

    def get_queryset(self):
        qs = super().get_queryset().exclude(
            shop_type=Shop.ShopType.IN_TRANSIT
        )
        user = self.request.user
        if user.is_authenticated and user.is_seller:
            return qs.filter(pk__in=user.active_shops().values_list("pk", flat=True))
        if not self.request.query_params.get("include_inactive"):
            qs = qs.filter(is_active=True)
        search = self.request.query_params.get("search")
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(code__icontains=search))
        return qs

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user, updated_by=self.request.user)

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)

    def perform_destroy(self, instance):
        # Soft delete only (core rule 7).
        instance.is_deleted = True
        instance.is_active = False
        instance.updated_by = self.request.user
        instance.save(update_fields=["is_deleted", "is_active", "updated_by", "updated_at"])


class EmployeeShopAssignmentViewSet(viewsets.ModelViewSet):
    """Shop assignments: owner manages; owner/manager may read."""

    serializer_class = EmployeeShopAssignmentSerializer
    permission_classes = [IsOwnerReadOnlyOrManager]
    queryset = EmployeeShopAssignment.objects.select_related(
        "employee", "shop"
    ).order_by("-assigned_from", "-id")
    lookup_field = "id"

    def get_queryset(self):
        qs = super().get_queryset()
        employee = self.request.query_params.get("employee")
        if employee:
            qs = qs.filter(employee_id=employee)
        shop = self.request.query_params.get("shop")
        if shop:
            qs = qs.filter(shop_id=shop)
        active_only = self.request.query_params.get("active")
        if active_only in ("1", "true", "True"):
            today = timezone.localdate()
            qs = qs.filter(
                is_active=True,
                assigned_from__lte=today,
            ).filter(
                Q(assigned_to__isnull=True) | Q(assigned_to__gte=today)
            )
        return qs

    def perform_create(self, serializer):
        serializer.save(
            created_by=self.request.user, updated_by=self.request.user
        )

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)
