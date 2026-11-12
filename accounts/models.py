from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone
from simple_history.models import HistoricalRecords


class User(AbstractUser):
    """Application user with a single fixed role.

    Role enforcement lives on the backend permission classes. The model is
    introduced now so ``AUTH_USER_MODEL`` never has to change after data
    exists.
    """

    class Role(models.TextChoices):
        OWNER = "OWNER", "Owner"
        MANAGER = "MANAGER", "Manager"
        SELLER = "SELLER", "Seller"

    role = models.CharField(
        max_length=10,
        choices=Role.choices,
        default=Role.SELLER,
        db_index=True,
    )
    can_manage_products = models.BooleanField(
        default=False,
        help_text="Allows a MANAGER to create products. Owners always can.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["username"]

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.role})"

    @property
    def is_owner(self):
        return self.role == self.Role.OWNER

    @property
    def is_manager(self):
        return self.role == self.Role.MANAGER

    @property
    def is_seller(self):
        return self.role == self.Role.SELLER

    history = HistoricalRecords()

    def active_shops(self):
        """Shops this user is currently assigned to (SELLER scope)."""
        today = timezone.localdate()
        return Shop.objects.filter(
            assignments__employee=self,
            assignments__is_active=True,
            assignments__assigned_from__lte=today,
        ).filter(
            models.Q(assignments__assigned_to__isnull=True)
            | models.Q(assignments__assigned_to__gte=today),
            is_deleted=False,
            is_active=True,
        )


class Shop(models.Model):
    """A physical location that holds stock (retail shop or warehouse)."""

    class ShopType(models.TextChoices):
        WAREHOUSE = "WAREHOUSE", "Warehouse"
        SHOP = "SHOP", "Shop"
        IN_TRANSIT = "IN_TRANSIT", "In-Transit"

    name = models.CharField(max_length=120)
    code = models.SlugField(max_length=40, unique=True)
    shop_type = models.CharField(
        max_length=10, choices=ShopType.choices, default=ShopType.SHOP
    )
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    is_active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)

    created_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="shops_created",
    )
    updated_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="shops_updated",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.code})"

    history = HistoricalRecords()


class EmployeeShopAssignment(models.Model):
    """Maps an employee to a shop with an assignment date range.

    Active when ``is_active`` and today falls within
    ``assigned_from`` .. ``assigned_to`` (``assigned_to`` null = open-ended).
    """

    employee = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="shop_assignments",
    )
    shop = models.ForeignKey(
        Shop,
        on_delete=models.CASCADE,
        related_name="assignments",
    )
    assigned_from = models.DateField()
    assigned_to = models.DateField(
        null=True,
        blank=True,
        help_text="Leave blank for an open-ended assignment.",
    )
    is_active = models.BooleanField(default=True)

    created_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assignments_created",
    )
    updated_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assignments_updated",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-assigned_from", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "shop", "assigned_from"],
                name="unique_employee_shop_assignment_date",
            ),
            models.CheckConstraint(
                check=models.Q(assigned_to__isnull=True)
                | models.Q(assigned_to__gte=models.F("assigned_from")),
                name="assignment_to_gte_from",
            ),
        ]

    def __str__(self):
        return f"{self.employee} -> {self.shop} from {self.assigned_from}"

    def is_current(self, on_date=None):
        on_date = on_date or timezone.localdate()
        if not self.is_active:
            return False
        if self.assigned_from > on_date:
            return False
        if self.assigned_to and self.assigned_to < on_date:
            return False
        return True
