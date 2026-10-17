from asgiref.sync import async_to_sync
from django.core.management.base import BaseCommand

from bridge.probe import refresh_stale_probes


class Command(BaseCommand):
    help = "Refresh third-party provider balances used by the 5% reserve guard."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Ignore the probe TTL")

    def handle(self, *args, **options):
        count = async_to_sync(refresh_stale_probes)(force=options["force"])
        self.stdout.write(self.style.SUCCESS(f"refreshed {count} probe(s)"))
