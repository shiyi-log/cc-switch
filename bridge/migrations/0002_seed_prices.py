from decimal import Decimal

from django.db import migrations

# USD per million tokens; longest prefix wins in ``bridge.upstream.price_for``.
PRICES = [
    ("claude-opus-4", "15", "75", "1.5", "18.75"),
    ("claude-sonnet-4", "3", "15", "0.3", "3.75"),
    ("claude-haiku-4", "1", "5", "0.1", "1.25"),
    ("claude-3-7-sonnet", "3", "15", "0.3", "3.75"),
    ("claude-3-5-sonnet", "3", "15", "0.3", "3.75"),
    ("claude-3-5-haiku", "0.8", "4", "0.08", "1"),
]


def seed_prices(apps, schema_editor):
    ModelPrice = apps.get_model("bridge", "ModelPrice")
    for prefix, input_price, output_price, cache_read, cache_write in PRICES:
        ModelPrice.objects.update_or_create(
            model_prefix=prefix,
            defaults={
                "input_per_mtok": Decimal(input_price),
                "output_per_mtok": Decimal(output_price),
                "cache_read_per_mtok": Decimal(cache_read),
                "cache_write_per_mtok": Decimal(cache_write),
            },
        )


def remove_prices(apps, schema_editor):
    ModelPrice = apps.get_model("bridge", "ModelPrice")
    ModelPrice.objects.filter(model_prefix__in=[item[0] for item in PRICES]).delete()


class Migration(migrations.Migration):
    dependencies = [("bridge", "0001_initial")]

    operations = [migrations.RunPython(seed_prices, remove_prices)]
