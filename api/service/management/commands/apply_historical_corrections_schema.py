from django.core.management.base import BaseCommand
from django.db import connection, transaction


class Command(BaseCommand):
    help = (
        "Aplica schema de correcciones históricas forzadas y extiende trazabilidad de venta MG "
        "(comprador y número alternativo)"
    )

    def handle(self, *args, **opts):
        payload_type = "JSONB" if connection.vendor == "postgresql" else "TEXT"
        payload_default = "'{}'::jsonb" if connection.vendor == "postgresql" else "'{}'"

        with transaction.atomic():
            with connection.cursor() as cur:
                # Snapshot de venta MG en devices
                cur.execute(
                    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS mg_venta_customer_id INTEGER NULL REFERENCES customers(id) ON DELETE SET NULL"
                )
                cur.execute(
                    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS mg_venta_numero_alternativo TEXT"
                )

                # Trazabilidad de evento MG
                cur.execute(
                    "ALTER TABLE device_mg_events ADD COLUMN IF NOT EXISTS venta_customer_id INTEGER NULL REFERENCES customers(id) ON DELETE SET NULL"
                )
                cur.execute(
                    "ALTER TABLE device_mg_events ADD COLUMN IF NOT EXISTS venta_numero_alternativo TEXT"
                )

                # Auditoria dedicada de correcciones historicas
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS ingreso_historical_corrections (
                      id              INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                      ingreso_id      INTEGER NOT NULL REFERENCES ingresos(id) ON DELETE CASCADE,
                      accion          TEXT NOT NULL CHECK (accion IN ('entrega','alta_alquiler','baja_alquiler','baja_ingreso','alta_ingreso')),
                      fecha_efectiva  TIMESTAMPTZ NOT NULL,
                      motivo          TEXT NOT NULL,
                      payload         {payload_type} NOT NULL DEFAULT {payload_default},
                      notificar       BOOLEAN NOT NULL DEFAULT TRUE,
                      usuario_id      INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cur.execute(
                    f"ALTER TABLE ingreso_historical_corrections ADD COLUMN IF NOT EXISTS payload {payload_type} NOT NULL DEFAULT {payload_default}"
                )
                cur.execute(
                    "ALTER TABLE ingreso_historical_corrections ADD COLUMN IF NOT EXISTS notificar BOOLEAN NOT NULL DEFAULT TRUE"
                )
                cur.execute(
                    "ALTER TABLE ingreso_historical_corrections ADD COLUMN IF NOT EXISTS usuario_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ingreso_hist_corr_ingreso_fecha ON ingreso_historical_corrections (ingreso_id, fecha_efectiva DESC, id DESC)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ingreso_hist_corr_created_at ON ingreso_historical_corrections (created_at DESC, id DESC)"
                )

        self.stdout.write(
            "APLICADO OK: correcciones históricas forzadas + trazabilidad extendida de venta MG"
        )
