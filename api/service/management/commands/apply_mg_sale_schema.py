from django.core.management.base import BaseCommand
from django.db import connection, transaction


class Command(BaseCommand):
    help = "Aplica schema de MG inactivo por venta + historial device_mg_events"

    def handle(self, *args, **opts):
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS mg_estado TEXT")
                cur.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS mg_inactivo_desde TIMESTAMPTZ")
                cur.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS mg_venta_fecha TIMESTAMPTZ")
                cur.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS mg_venta_factura_numero TEXT")
                cur.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS mg_venta_remito_numero TEXT")
                cur.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS mg_venta_observaciones TEXT")
                cur.execute(
                    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS mg_venta_usuario_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL"
                )

                cur.execute("UPDATE devices SET mg_estado='activo' WHERE mg_estado IS NULL OR TRIM(mg_estado) = ''")
                cur.execute("UPDATE devices SET mg_estado='activo' WHERE mg_estado NOT IN ('activo','inactivo_venta')")
                cur.execute("ALTER TABLE devices ALTER COLUMN mg_estado SET DEFAULT 'activo'")
                cur.execute("ALTER TABLE devices ALTER COLUMN mg_estado SET NOT NULL")

                if connection.vendor == "postgresql":
                    cur.execute(
                        """
                        DO $$
                        BEGIN
                          IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint
                             WHERE conname = 'chk_devices_mg_estado'
                               AND conrelid = 'devices'::regclass
                          ) THEN
                            ALTER TABLE devices
                              ADD CONSTRAINT chk_devices_mg_estado
                              CHECK (mg_estado IN ('activo','inactivo_venta'));
                          END IF;
                        END
                        $$;
                        """
                    )

                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS device_mg_events (
                      id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                      device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
                      accion TEXT NOT NULL CHECK (accion IN ('venta', 'reactivacion')),
                      numero_interno_snapshot TEXT,
                      fecha_evento TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      factura_numero TEXT,
                      remito_numero TEXT,
                      observaciones TEXT,
                      usuario_id INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      ingreso_id INTEGER NULL REFERENCES ingresos(id) ON DELETE SET NULL,
                      source TEXT NOT NULL DEFAULT 'equipos' CHECK (source IN ('equipos', 'service_sheet'))
                    );
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_device_mg_events_device_fecha_desc ON device_mg_events(device_id, fecha_evento DESC)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_device_mg_events_fecha_desc ON device_mg_events(fecha_evento DESC)"
                )

        self.stdout.write("APLICADO OK: MG venta schema")
