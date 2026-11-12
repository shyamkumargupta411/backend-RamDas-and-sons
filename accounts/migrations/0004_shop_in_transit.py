from django.db import migrations, models


def seed_in_transit(apps, schema_editor):
    Shop = apps.get_model("accounts", "Shop")
    Shop.objects.get_or_create(
        code="in-transit",
        defaults={
            "name": "In-Transit",
            "shop_type": "IN_TRANSIT",
            "is_active": True,
        },
    )


def unseed_in_transit(apps, schema_editor):
    Shop = apps.get_model("accounts", "Shop")
    Shop.objects.filter(code="in-transit", name="In-Transit").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_shop_shop_type"),
    ]

    operations = [
        migrations.AlterField(
            model_name="shop",
            name="shop_type",
            field=models.CharField(
                choices=[
                    ("WAREHOUSE", "Warehouse"),
                    ("SHOP", "Shop"),
                    ("IN_TRANSIT", "In-Transit"),
                ],
                default="SHOP",
                max_length=10,
            ),
        ),
        migrations.RunPython(seed_in_transit, unseed_in_transit),
    ]
