from django.core.management.base import BaseCommand

from service.bejerman_remito_sales_sync import DEFAULT_DATE_FROM, sync_bejerman_remito_items


class Command(BaseCommand):
    help = (
        "Sincroniza renglones de remito de salida con partida hacia la cache de ventas. "
        "Es el respaldo de la garantia de fabrica para el equipo que nunca se facturo "
        "con la partida cargada."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--company-key",
            default="SEPID",
            help="Empresa Bejerman. Acepta varias separadas por coma (ej: SEPID,MGBIO).",
        )
        parser.add_argument("--date-from", default=DEFAULT_DATE_FROM)
        parser.add_argument("--date-to", default="")
        parser.add_argument("--types", default="", help="Tipos de comprobante (default: el de settings, RT).")
        parser.add_argument(
            "--chunk-days",
            type=int,
            default=366,
            help="Tamano de la ventana de fechas por consulta.",
        )
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        types = [item.strip().upper() for item in (opts.get("types") or "").split(",") if item.strip()] or None
        companies = [
            item.strip().upper()
            for item in (opts.get("company_key") or "SEPID").split(",")
            if item.strip()
        ] or ["SEPID"]
        failed = False
        for company_key in companies:
            summary = sync_bejerman_remito_items(
                company_key=company_key,
                date_from=opts.get("date_from") or None,
                date_to=opts.get("date_to") or None,
                types=types,
                chunk_days=opts.get("chunk_days"),
                dry_run=bool(opts.get("dry_run")),
            )
            self.stdout.write(
                f"{'OK' if summary['ok'] else 'CON ERRORES'} remitos Bejerman [{company_key}]: "
                f"ventanas={summary['windows']} filas={summary['rows']} "
                f"items={summary['items']} upserted={summary['upserted']} "
                f"errors={len(summary['errors'])} dry_run={summary['dryRun']}"
            )
            for error in summary["errors"][:10]:
                failed = True
                self.stderr.write(f"[{company_key}] {error}")
        if failed:
            self.stderr.write("Revisar los errores: la cache puede haber quedado incompleta.")
