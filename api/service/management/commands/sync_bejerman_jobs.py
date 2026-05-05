from django.core.management.base import BaseCommand

from service.bejerman_sync import process_bejerman_jobs


class Command(BaseCommand):
    help = "Procesa jobs pendientes de sincronizacion con Bejerman"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10)
        parser.add_argument("--job-id", type=int, default=None)

    def handle(self, *args, **opts):
        stats = process_bejerman_jobs(limit=opts["limit"], job_id=opts.get("job_id"))
        self.stdout.write(f"BEJERMAN_SYNC {stats}")
