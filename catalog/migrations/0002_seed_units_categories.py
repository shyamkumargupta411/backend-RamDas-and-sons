"""Seed default units and product categories."""

from django.db import migrations

UNITS = [
    ("KG", "Kilogram", "WEIGHT"),
    ("PIECE", "Piece", "COUNT"),
    ("FEET", "Feet", "LENGTH"),
    ("METER", "Meter", "LENGTH"),
    ("G", "Gram", "WEIGHT"),
]

CATEGORIES = [
    # (name, code, form_factor, sort_order)
    ("Metal Ingots", "metal-ingots", "INGOT", 10),
    ("Patiya/Flat", "patiya-flat", "FLAT", 20),
    ("Metal Pipes", "metal-pipes", "PIPE", 30),
    ("Other Metal (Rods, Angles, Channels)", "other-metal", "OTHER", 40),
]


def seed_units(apps, schema_editor):
    Unit = apps.get_model("catalog", "Unit")
    for code, name, unit_type in UNITS:
        Unit.objects.get_or_create(
            code=code,
            defaults={"name": name, "unit_type": unit_type, "is_active": True},
        )


def seed_categories(apps, schema_editor):
    ProductCategory = apps.get_model("catalog", "ProductCategory")
    for name, code, form_factor, sort_order in CATEGORIES:
        ProductCategory.objects.get_or_create(
            code=code,
            defaults={
                "name": name,
                "form_factor": form_factor,
                "sort_order": sort_order,
                "is_active": True,
            },
        )


def unseed(apps, schema_editor):
    # Leave data in place on rollback; master data may already be referenced.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_units, unseed),
        migrations.RunPython(seed_categories, unseed),
    ]
