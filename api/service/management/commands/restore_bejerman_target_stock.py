from django.core.management.base import BaseCommand

from service.bejerman_sync import restore_target_stock_from_jobs


class Command(BaseCommand):
    help = "Restaura stock positivo en STC/STL usando ENT con partida por número de serie"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=250)
        parser.add_argument("--job-id", type=int, default=None)

    def handle(self, *args, **opts):
        stats = restore_target_stock_from_jobs(
            limit=opts["limit"],
            job_id=opts.get("job_id"),
        )
        self.stdout.write(
            "BEJERMAN_TARGET_STOCK "
            f"checked={stats['checked']} "
            f"restored={stats['restored']} "
            f"already_present={stats['already_present']} "
            f"not_found={stats['not_found']} "
            f"skipped={stats['skipped']} "
            f"blocked={stats['blocked']} "
            f"errors={stats['errors']}"
        )
