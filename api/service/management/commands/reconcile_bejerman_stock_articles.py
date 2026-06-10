from django.core.management.base import BaseCommand
from django.db import connection

from service.bejerman_sync import (
    SYNC_TYPE_STOCK_ENTRY_STR,
    SYNC_TYPE_STOCK_EXIT_RTS,
    reconcile_article_mappings_from_stock,
)


class Command(BaseCommand):
    help = "Concilia artículos Bejerman por número de serie y limpia jobs legacy si se solicita"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=250)
        parser.add_argument("--job-id", type=int, default=None)
        parser.add_argument("--delete-rss", action="store_true")
        parser.add_argument("--delete-ingreso-entry", action="store_true")

    def handle(self, *args, **opts):
        deleted_rss = 0
        deleted_ingreso_entry = 0
        if opts.get("delete_rss"):
            with connection.cursor() as cur:
                cur.execute(
                    "DELETE FROM bejerman_sync_jobs WHERE sync_type = %s",
                    [SYNC_TYPE_STOCK_EXIT_RTS],
                )
                deleted_rss = cur.rowcount

        if opts.get("delete_ingreso_entry"):
            with connection.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM bejerman_sync_jobs
                     WHERE sync_type = %s
                       AND status IN ('pending','running','failed','blocked')
                    """,
                    [SYNC_TYPE_STOCK_ENTRY_STR],
                )
                deleted_ingreso_entry = cur.rowcount

        limit = int(opts["limit"] or 0)
        if limit > 0:
            stats = reconcile_article_mappings_from_stock(
                limit=limit,
                job_id=opts.get("job_id"),
            )
        else:
            stats = {
                "checked": 0,
                "mapped_from_stock": 0,
                "mapped_from_local": 0,
                "not_found": 0,
                "skipped": 0,
                "errors": 0,
            }
        self.stdout.write(
            "BEJERMAN_ARTICLES "
            f"checked={stats['checked']} "
            f"mapped_from_stock={stats['mapped_from_stock']} "
            f"mapped_from_local={stats['mapped_from_local']} "
            f"not_found={stats['not_found']} "
            f"skipped={stats['skipped']} "
            f"errors={stats['errors']} "
            f"deleted_rss={deleted_rss} "
            f"deleted_ingreso_entry={deleted_ingreso_entry}"
        )
