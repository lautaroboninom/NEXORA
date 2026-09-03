from django.core.management.base import BaseCommand
from django.db import connection, transaction


class Command(BaseCommand):
    help = (
        "Aplica el schema del cambio de equipo (permuta / parte de pago): vincula el "
        "equipo entregado con `ingresos.device_cambio_id` y registra el destino del "
        "equipo que pasa a propiedad de SEPID (parque propio, venta o desguace)."
    )

    def handle(self, *args, **opts):
        with transaction.atomic():
            with connection.cursor() as cur:
                # `device_id` sigue siendo el equipo que INGRESÓ; este es el que SALIÓ.
                cur.execute(
                    "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS device_cambio_id "
                    "INTEGER NULL REFERENCES devices(id) ON DELETE SET NULL"
                )
                cur.execute(
                    "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS cambio_destino_viejo TEXT NULL"
                )
                cur.execute(
                    "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS cambio_fecha TIMESTAMPTZ NULL"
                )
                cur.execute(
                    "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS cambio_registrado_por "
                    "INTEGER NULL REFERENCES users(id) ON DELETE SET NULL"
                )
                # Parte de pago sin permuta: tomamos el equipo del cliente y no entregamos
                # ninguno a cambio. Es el mismo movimiento patrimonial, con `device_cambio_id`
                # en NULL. Hace falta la marca explícita porque un cambio histórico (anterior
                # a la FK) también tiene el vínculo vacío y no es lo mismo.
                cur.execute(
                    "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS cambio_sin_entrega "
                    "BOOLEAN NOT NULL DEFAULT FALSE"
                )
                # El cliente que tenía el equipo antes de que pasara a ser nuestro. Los
                # papeles de la OS resuelven el cliente por `devices.customer_id`, y tomar el
                # equipo en parte de pago lo pasa a MG BIO: sin este snapshot la orden de
                # salida que firma el cliente salía a nombre de MG BIO.
                cur.execute(
                    "ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS cliente_original_id "
                    "INTEGER NULL REFERENCES customers(id) ON DELETE SET NULL"
                )
                # La columna vieja de texto libre se conserva como snapshot: la leen el PDF,
                # la verificación de entrega y los cambios históricos sin FK.
                cur.execute("ALTER TABLE ingresos ADD COLUMN IF NOT EXISTS serial_cambio TEXT")

                if connection.vendor == "postgresql":
                    # Se recrea siempre en vez de un IF NOT EXISTS: agregar un destino
                    # cambia la lista permitida, y la constraint vieja seguiría rechazando
                    # el valor nuevo en una base donde el comando ya había corrido.
                    cur.execute(
                        "ALTER TABLE ingresos DROP CONSTRAINT IF EXISTS "
                        "chk_ingresos_cambio_destino_viejo"
                    )
                    cur.execute(
                        """
                        ALTER TABLE ingresos
                          ADD CONSTRAINT chk_ingresos_cambio_destino_viejo
                          CHECK (cambio_destino_viejo IS NULL
                                 OR cambio_destino_viejo IN
                                    ('pendiente','mg','venta','desguace'));
                        """
                    )
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS idx_ingresos_device_cambio "
                        "ON ingresos(device_cambio_id) WHERE device_cambio_id IS NOT NULL"
                    )

                # Los cambios ya registrados quedan con destino 'pendiente': nadie decidió
                # todavía qué pasa con el equipo que quedó acá.
                cur.execute(
                    """
                    UPDATE ingresos
                       SET cambio_destino_viejo = 'pendiente'
                     WHERE cambio_destino_viejo IS NULL
                       AND LOWER(COALESCE(resolucion,'')) = 'cambio'
                    """
                )

        self.stdout.write("APLICADO OK: cambio de equipo schema")
