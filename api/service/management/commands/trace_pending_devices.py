from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from service.pending_device_trace import (
    DEFAULT_ACCESS_DB,
    DEFAULT_CUTOFF_DATE,
    DEFAULT_XLSX_NOVAMED,
    trace_pending_devices,
    write_trace_outputs,
)


def _timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


class Command(BaseCommand):
    help = "Genera un reporte consolidado de trazabilidad para ingresos pendientes previos a 2026."

    def add_arguments(self, parser):
        parser.add_argument("--cutoff-date", default=DEFAULT_CUTOFF_DATE, help="Fecha de corte YYYY-MM-DD.")
        parser.add_argument(
            "--xlsx-novamed",
            default=DEFAULT_XLSX_NOVAMED,
            help="Ruta del Excel de NOVAMED.",
        )
        parser.add_argument(
            "--access-db",
            default=DEFAULT_ACCESS_DB,
            help="Ruta de la base Access histórica.",
        )
        parser.add_argument("--ids", nargs="*", type=int, help="Lista opcional de OS/ingresos a rastrear.")
        parser.add_argument(
            "--out-dir",
            default="",
            help="Directorio de salida. Default: output/trace_pending_devices/<timestamp>",
        )

    def handle(self, *args, **options):
        cutoff_date = str(options.get("cutoff_date") or DEFAULT_CUTOFF_DATE).strip()
        xlsx_novamed = str(options.get("xlsx_novamed") or DEFAULT_XLSX_NOVAMED).strip()
        access_db = str(options.get("access_db") or DEFAULT_ACCESS_DB).strip()
        ids = [int(item) for item in (options.get("ids") or []) if item is not None]
        out_dir = str(options.get("out_dir") or "").strip()
        if not out_dir:
            out_dir = str(Path("output") / "trace_pending_devices" / _timestamp_slug())

        try:
            outputs = trace_pending_devices(
                cutoff_date=cutoff_date,
                xlsx_novamed=xlsx_novamed,
                access_db=access_db,
                ids=ids or None,
            )
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        out_path = write_trace_outputs(out_dir, outputs)
        summary = outputs["metadata"]
        self.stdout.write(f"OK: reporte generado en {out_path}")
        self.stdout.write(f"Candidatos: {summary.get('candidate_count', 0)}")
        if summary.get("access_error"):
            self.stdout.write(f"Advertencia Access: {summary['access_error']}")
