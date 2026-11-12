from django.contrib.auth.password_validation import validate_password
from django.utils import timezone
from rest_framework import serializers

from .models import EmployeeShopAssignment, Shop, User


class ShopSerializer(serializers.ModelSerializer):
    class Meta:
        model = Shop
        fields = [
            "id",
            "name",
            "code",
            "shop_type",
            "address",
            "phone",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class EmployeeShopAssignmentSerializer(serializers.ModelSerializer):
    shop_detail = ShopSerializer(source="shop", read_only=True)
    employee_username = serializers.CharField(
        source="employee.username", read_only=True
    )

    class Meta:
        model = EmployeeShopAssignment
        fields = [
            "id",
            "employee",
            "employee_username",
            "shop",
            "shop_detail",
            "assigned_from",
            "assigned_to",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class UserSerializer(serializers.ModelSerializer):
    shops = ShopSerializer(source="active_shops", many=True, read_only=True)
    password = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        validators=[validate_password],
    )

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "role",
            "can_manage_products",
            "is_active",
            "shops",
            "password",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        # Password required only on create; optional on update (blank = unchanged).
        if self.instance is None:
            if not attrs.get("password"):
                raise serializers.ValidationError(
                    {"password": "This field is required."}
                )
        elif attrs.get("password") in (None, ""):
            attrs.pop("password", None)
        return attrs

    def create(self, validated_data):
        password = validated_data.pop("password")
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if password:
            instance.set_password(password)
        instance.save()
        return instance


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(style={"input_type": "password"})


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField(required=False, allow_blank=True)


class UserCreateSerializer(UserSerializer):
    """Alias kept explicit for OpenAPI readability."""

    class Meta(UserSerializer.Meta):
        model = User
        fields = UserSerializer.Meta.fields
        read_only_fields = UserSerializer.Meta.read_only_fields


def today():
    return timezone.localdate()
