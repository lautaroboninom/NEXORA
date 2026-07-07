from __future__ import annotations

from dataclasses import dataclass

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone


@dataclass(frozen=True)
class RemitoMapping:
    ingreso_id: int
    tipo: str
    pto_venta: str
    numero: str

    @property
    def remito_number(self) -> str:
        return f"{self.tipo} R {self.pto_venta}-{self.numero}"


# Correcciones verificadas contra SegCabV de Bejerman el 07/07/2026:
# las bajas de alquiler se documentan con RDA, no con RIS.
DEFAULT_MAPPINGS = (
    RemitoMapping(29385, "RDA", "00004", "00004556"),
    RemitoMapping(29386, "RDA", "00004", "00004556"),
    RemitoMapping(29387, "RDA", "00004", "00004556"),
    RemitoMapping(29388, "RDA", "00004", "00004540"),
    RemitoMapping(29409, "RDA", "00001", "00026231"),
)


def _parse_mapping(raw: str) -> RemitoMapping:
    try:
        ingreso_text, doc = raw.split("=", 1)
        tipo, pto_venta, numero = doc.split(":")
        return RemitoMapping(int(ingreso_text.strip()), tipo.strip().upper(), pto_venta.strip(), numero.strip())
    except ValueError as exc:
        raise CommandError(f"Mapping inválido {raw!r}; formato esperado 29385=RDA:00004:00004556") from exc


class Command(BaseCommand):
    help = (
        "Corrige el comprobante Bejerman asociado a ingresos (bejerman_ingreso_remitos + ingresos.remito_ingreso). "
        "Dry-run por defecto; usar --apply para escribir."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--mapping",
            action="append",
            default=None,
            help="ingreso=TIPO:PTOVENTA:NUMERO (ej. 29385=RDA:00004:00004556). Repetible; sin esto usa los defaults.",
        )
        parser.add_argument("--apply", action="store_true", help="Aplica los cambios (sin esto solo muestra).")

    def handle(self, *args, **options):
        mappings = [_parse_mapping(raw) for raw in options["mapping"]] if options["mapping"] else list(DEFAULT_MAPPINGS)
        apply_changes = options["apply"]
        ingreso_ids = [m.ingreso_id for m in mappings]

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT r.ingreso_id, r.status, r.pdf_status, r.comprobante_tipo, r.comprobante_letra,
                       r.comprobante_pto_venta, r.comprobante_numero, r.remito_number, i.remito_ingreso
                  FROM bejerman_ingreso_remitos r
                  JOIN ingresos i ON i.id = r.ingreso_id
                 WHERE r.ingreso_id = ANY(%s)
                 ORDER BY r.ingreso_id
                """,
                [ingreso_ids],
            )
            columns = [col[0] for col in cur.description]
            before = {row[0]: dict(zip(columns, row)) for row in cur.fetchall()}

        missing = [m.ingreso_id for m in mappings if m.ingreso_id not in before]
        if missing:
            raise CommandError(f"Sin registro en bejerman_ingreso_remitos para ingresos: {missing}")

        self.stdout.write(self.style.MIGRATE_HEADING("Remapeo de comprobantes de ingreso"))
        for mapping in mappings:
            row = before[mapping.ingreso_id]
            self.stdout.write(
                f"OS {mapping.ingreso_id} [{row['status']}/{row['pdf_status']}]: "
                f"{row['remito_number'] or '(sin remito)'} -> {mapping.remito_number} "
                f"| ingresos.remito_ingreso: {row['remito_ingreso'] or '(vacío)'} -> {mapping.remito_number}"
            )

        if not apply_changes:
            self.stdout.write(self.style.WARNING("DRY-RUN: no se aplicaron cambios (usar --apply)."))
            return

        stamp = timezone.now().strftime("%Y%m%d_%H%M%S")
        backup_remitos = f"bejerman_ingreso_remitos_backup_{stamp}"
        backup_ingresos = f"ingresos_remito_backup_{stamp}"

        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(
                    f"CREATE TABLE {backup_remitos} AS SELECT * FROM bejerman_ingreso_remitos WHERE ingreso_id = ANY(%s)",
                    [ingreso_ids],
                )
                cur.execute(
                    f"CREATE TABLE {backup_ingresos} AS SELECT id, remito_ingreso FROM ingresos WHERE id = ANY(%s)",
                    [ingreso_ids],
                )
                for mapping in mappings:
                    cur.execute(
                        """
                        UPDATE bejerman_ingreso_remitos
                           SET comprobante_tipo = %s,
                               comprobante_letra = 'R',
                               comprobante_pto_venta = %s,
                               comprobante_numero = %s,
                               remito_number = %s,
                               pdf_status = 'pending',
                               updated_at = NOW()
                         WHERE ingreso_id = %s
                        """,
                        [mapping.tipo, mapping.pto_venta, mapping.numero, mapping.remito_number, mapping.ingreso_id],
                    )
                    cur.execute(
                        "UPDATE ingresos SET remito_ingreso = %s WHERE id = %s",
                        [mapping.remito_number, mapping.ingreso_id],
                    )

        self.stdout.write(self.style.SUCCESS(f"Backups: {backup_remitos}, {backup_ingresos}"))

        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT r.ingreso_id, r.comprobante_tipo, r.comprobante_pto_venta, r.comprobante_numero,
                       r.remito_number, r.status, r.pdf_status, i.remito_ingreso
                  FROM bejerman_ingreso_remitos r
                  JOIN ingresos i ON i.id = r.ingreso_id
                 WHERE r.ingreso_id = ANY(%s)
                 ORDER BY r.ingreso_id
                """,
                [ingreso_ids],
            )
            self.stdout.write(self.style.MIGRATE_HEADING("Verificación post-cambio"))
            for row in cur.fetchall():
                self.stdout.write("  " + " | ".join(str(value) for value in row))
