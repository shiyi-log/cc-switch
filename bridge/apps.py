from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _sqlite_pragmas(sender, connection, **kwargs):
    if connection.vendor == "sqlite":
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA busy_timeout=30000;")


class BridgeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "bridge"
    verbose_name = "CC Switch 代理桥"

    def ready(self):
        connection_created.connect(_sqlite_pragmas, dispatch_uid="bridge_sqlite_pragmas")
