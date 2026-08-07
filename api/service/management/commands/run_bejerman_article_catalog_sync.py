import logging
import time

from django.core.management.base import BaseCommand

from service.bejerman_article_catalog_sync import sync_article_catalog


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Mantiene MGBIO alineado a SEPID en descripciones de articulos (casos cosmeticos). "
        "Los casos sustantivos (producto distinto bajo el mismo codigo) nunca se aplican "
        "solos aqui: quedan logueados como needs_review en bejerman_article_sync_log para "
        "resolverlos a mano con sync_bejerman_articulos_sepid_mgbio --include-sustantive."
    )

    def add_arguments(self, parser):
        parser.add_argument("--interval-hours", type=float, default=6)
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **opts):
        interval = max(0.5, float(opts["interval_hours"] or 6)) * 3600
        once = bool(opts["once"])
        self.stdout.write(f"Sync de catalogo de articulos SEPID -> MGBIO cada {interval / 3600:.1f}h.")
        while True:
            try:
                summary = sync_article_catalog(apply=True, include_sustantive=False)
                if summary["totalDiscrepancies"]:
                    self.stdout.write(
                        f"discrepancias={summary['totalDiscrepancies']} "
                        f"cosmeticas_aplicadas={summary['cosmetic']['applied']} "
                        f"sustantivas_pendientes_revision={summary['sustantive']['needsReview']}"
                    )
            except Exception:
                logger.exception("No se pudo sincronizar el catalogo de articulos SEPID -> MGBIO")
            if once:
                return
            time.sleep(interval)
