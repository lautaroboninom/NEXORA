from django.core.management.base import BaseCommand
from django.db import connection, transaction


class Command(BaseCommand):
    help = "Agrega cobro neto y vínculo de presupuesto rechazado en ingresos"

    def handle(self, *args, **opts):
        if connection.vendor != "postgresql":
            self.stdout.write("SKIP: comando disponible solo para PostgreSQL.")
            return

        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(
                    "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS presupuesto_rechazado_cobro_neto NUMERIC(12,2)"
                )
                cur.execute(
                    "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS presupuesto_rechazado_quote_id INTEGER"
                )
                cur.execute(
                    """
                    DO $$
                    BEGIN
                      IF NOT EXISTS (
                        SELECT 1
                          FROM pg_constraint
                         WHERE conname = 'fk_ingresos_presupuesto_rechazado_quote'
                      ) THEN
                        ALTER TABLE ingresos
                          ADD CONSTRAINT fk_ingresos_presupuesto_rechazado_quote
                          FOREIGN KEY (presupuesto_rechazado_quote_id)
                          REFERENCES quotes(id)
                          ON DELETE SET NULL;
                      END IF;
                    END
                    $$;
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_ingresos_presupuesto_rechazado_quote_id
                    ON ingresos(presupuesto_rechazado_quote_id)
                    """
                )

        self.stdout.write(
            "APLICADO OK: ingresos (presupuesto_rechazado_cobro_neto, presupuesto_rechazado_quote_id)."
        )
