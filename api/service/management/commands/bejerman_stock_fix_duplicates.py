from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from service.bejerman_companies import DEFAULT_INGRESS_COMPANY_KEY
from service.bejerman_stock_corrections import (
    apply_approved_stock_corrections,
    apply_duplicate_stock_corrections,
    audit_duplicate_stock,
    default_report_path,
    load_approved_corrections,
    timestamp_run_id,
    write_report,
)


class Command(BaseCommand):
    help = "Detecta y corrige stock duplicado por partida en Bejerman usando SAL con causa ERR."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Solo genera el reporte. Es el modo predeterminado.")
        parser.add_argument("--apply", action="store_true", help="Emite SAL ERR para los casos permitidos.")
        parser.add_argument("--approved-csv", default="", help="CSV aprobado para casos multi-depósito.")
        parser.add_argument("--output", default="", help="Ruta CSV de salida o directorio de reportes.")
        parser.add_argument("--limit", type=int, default=250, help="Cantidad máxima de equipos o filas aprobadas.")
        parser.add_argument("--serial", action="append", default=[], help="Número de serie puntual. Puede repetirse.")
        parser.add_argument("--run-id", default="", help="Identificador del lote para IdOrigen y reportes.")
        parser.add_argument("--company-key", default=DEFAULT_INGRESS_COMPANY_KEY, help="Empresa Bejerman: SEPID, MGBIO o TEST.")

    def handle(self, *args, **options):
        if options.get("dry_run") and options.get("apply"):
            raise CommandError("Usá --dry-run o --apply, no ambos.")

        run_id = options.get("run_id") or timestamp_run_id()
        company_key = options.get("company_key") or DEFAULT_INGRESS_COMPANY_KEY
        limit = int(options.get("limit") or 250)
        serials = [serial for serial in (options.get("serial") or []) if str(serial or "").strip()]

        if options.get("apply"):
            approved_csv = options.get("approved_csv") or ""
            if approved_csv:
                corrections = load_approved_corrections(approved_csv)
                stats = apply_approved_stock_corrections(
                    corrections,
                    company_key=company_key,
                    limit=limit,
                    run_id=run_id,
                )
            else:
                stats = apply_duplicate_stock_corrections(
                    company_key=company_key,
                    serials=serials or None,
                    limit=limit,
                    run_id=run_id,
                )
        else:
            stats = audit_duplicate_stock(
                company_key=company_key,
                serials=serials or None,
                limit=limit,
                run_id=run_id,
            )

        output = options.get("output") or str(default_report_path(run_id))
        out_path = write_report(Path(output), stats.get("items") or [])
        self.stdout.write(
            "BEJERMAN_STOCK_FIX "
            f"checked={stats['checked']} "
            f"issues={stats['issues']} "
            f"auto_candidates={stats['auto_candidates']} "
            f"needs_approval={stats['needs_approval']} "
            f"applied={stats['applied']} "
            f"blocked={stats['blocked']} "
            f"skipped={stats['skipped']} "
            f"errors={stats['errors']} "
            f"output={out_path}"
        )
