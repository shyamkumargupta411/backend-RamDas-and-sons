"""Backend permission matrix (derived from the SRS role rules).

+----------------------+--------+----------+--------+-------+
| Capability           | OWNER  | MANAGER  | SELLER | Anon  |
+======================+========+==========+========+=======+
| Manage users         | yes    | no       | no     | 401   |
| Manage shops         | yes    | no       | no     | 401   |
| View all shops       | yes    | yes      | no     | 401   |
| View assigned shop   | yes    | yes      | yes    | 401   |
| Manage products      | yes    | flag*    | no     | 401   |
| Stock levels (read)   | yes    | yes      | own*   | 401   |
| Stock / transfers     | yes    | yes      | no     | 401   |
| Record sales          | yes    | yes      | own    | 401   |
| View audit history    | yes    | yes      | no     | 401   |
+----------------------+--------+----------+--------+-------+
* MANAGER needs ``can_manage_products=True``; SELLER stock read is
scoped to assigned shops (Stage 6).
"""

from rest_framework.permissions import SAFE_METHODS, BasePermission

from .models import User


class IsOwner(BasePermission):
    """Allows access only to OWNER users."""

    message = "Owner access required."

    def has_permission(self, request, view):
        return bool(
            request.user and request.user.is_authenticated and request.user.is_owner
        )


class IsManager(BasePermission):
    """Allows access only to MANAGER users."""

    message = "Manager access required."

    def has_permission(self, request, view):
        return bool(
            request.user and request.user.is_authenticated and request.user.is_manager
        )


class IsSeller(BasePermission):
    """Allows access only to SELLER users."""

    message = "Seller access required."

    def has_permission(self, request, view):
        return bool(
            request.user and request.user.is_authenticated and request.user.is_seller
        )


class IsOwnerOrManager(BasePermission):
    """Allows OWNER or MANAGER (the back-office roles)."""

    message = "Owner or manager access required."

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and user.role in (User.Role.OWNER, User.Role.MANAGER)
        )


class IsOwnerReadOnlyOrManager(BasePermission):
    """Read: owner/manager. Write: owner only."""

    message = "Owner write access required."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if request.method in SAFE_METHODS:
            return user.role in (User.Role.OWNER, User.Role.MANAGER)
        return user.is_owner


class CanManageProducts(BasePermission):
    """OWNER always; MANAGER only when ``can_manage_products`` is set."""

    message = "You do not have product management permission."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_owner:
            return True
        return user.is_manager and user.can_manage_products


class IsSellerOfShop(BasePermission):
    """Object-level: SELLER may only touch their assigned shop."""

    message = "You can only act on your assigned shop."

    def has_object_permission(self, request, view, obj):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.role != User.Role.SELLER:
            return user.role in (User.Role.OWNER, User.Role.MANAGER)
        shop = getattr(obj, "shop", obj)
        return user.active_shops().filter(pk=shop.pk).exists()


class ShopAccess(BasePermission):
    """Shops: any authenticated user may READ (sellers scoped by queryset);
    only OWNER may WRITE (create/update/soft-delete)."""

    message = "Owner access required to modify shops."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if request.method in SAFE_METHODS:
            return True
        return user.is_owner

    def has_object_permission(self, request, view, obj):
        user = request.user
        if request.method in SAFE_METHODS:
            if user.is_seller:
                return user.active_shops().filter(pk=obj.pk).exists()
            return True
        return user.is_owner
