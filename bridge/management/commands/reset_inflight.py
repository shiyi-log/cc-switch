from django.core.management.base import BaseCommand

from bridge.models import Account


class Command(BaseCommand):
    help = "Reset leaked in-flight counters (safe when the bridge is idle)."

    def handle(self, *args, **options):
        updated = Account.objects.update(inflight=0)
        self.stdout.write(self.style.SUCCESS(f"reset inflight for {updated} account(s)"))
