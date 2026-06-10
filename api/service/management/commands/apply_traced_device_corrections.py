from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from service.pending_device_trace import apply_traced_device_corrections, write_apply_outputs


def _timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


class Command(BaseCommand):
    help = "Aplica filas aprobadas del reporte de trazabilidad de equipos."

    def add_arguments(self, parser):
        parser.add_argument("--input", required=True, help="Archivo .csv/.xlsx con la hoja de propuestas aprobadas.")
        parser.add_argument(
            "--sheet",
            default="propuestas",
            help="Nombre de hoja dentro del XLSX (default: propuestas).",
        )
        parser.add_argument(
            "--actor-email",
            default="",
            help="Email del usuario que quedará registrado en la auditoría. Opcional.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Valida filas aprobadas sin persistir cambios.",
        )
        parser.add_argument(
            "--out-dir",
            default="",
            help="Directorio de salida. Default: output/apply_traced_device_corrections/<timestamp>",
        )

    def handle(self, *args, **options):
        input_path = str(options.get("input") or "").strip()
        sheet_name = str(options.get("sheet") or "propuestas").strip()
        actor_email = str(options.get("actor_email") or "").strip()
        dry_run = bool(options.get("dry_run"))
        out_dir = str(options.get("out_dir") or "").strip()
        if not out_dir:
            out_dir = str(Path("output") / "apply_traced_device_corrections" / _timestamp_slug())
        if not input_path:
            raise CommandError("Debe informar --input.")

        try:
            outputs = apply_traced_device_corrections(
                input_path=input_path,
                actor_email=actor_email,
                sheet_name=sheet_name,
                dry_run=dry_run,
            )
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        out_path = write_apply_outputs(out_dir, outputs)
        summary = outputs["summary"]
        self.stdout.write(f"OK: resultados guardados en {out_path}")
        self.stdout.write(f"Filas totales: {summary.get('rows_total', 0)}")
        self.stdout.write(f"Aplicadas: {summary.get('applied', 0)}")
        self.stdout.write(f"Errores: {summary.get('errors', 0)}")
        self.stdout.write(f"Saltadas: {summary.get('skipped', 0)}")
