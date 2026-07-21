from django.core.management.base import BaseCommand

from service.bejerman_pdf_requests import (
    process_bejerman_pdf_requests_once,
    run_bejerman_pdf_requests_loop,
)


class Command(BaseCommand):
    help = "Ejecuta el worker serial de solicitudes de PDF oficial de Bejerman"

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=2)
        parser.add_argument("--limit", type=int, default=1)
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **opts):
        interval = max(1, int(opts.get("interval") or 2))
        limit = max(1, int(opts.get("limit") or 1))
        if opts.get("once"):
            processed = process_bejerman_pdf_requests_once(limit=limit)
            self.stdout.write(self.style.SUCCESS(f"Solicitudes PDF procesadas: {processed}"))
            return
        self.stdout.write(
            f"Iniciando worker PDF de Bejerman interval={interval}s limit={limit}"
        )
        run_bejerman_pdf_requests_loop(interval_seconds=interval, limit=limit)
