from django.db import migrations


def create_in_transit_shop(apps, schema_editor):
    Shop = apps.get_model("accounts", "Shop")
    Shop.objects.get_or_create(
        code="in-transit",
        defaults={
            "name": "In Transit",
            "shop_type": "IN_TRANSIT",
            "is_active": True,
        },
    )


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0004_shop_in_transit"),
    ]

    operations = [
        migrations.RunPython(create_in_transit_shop),
    ]