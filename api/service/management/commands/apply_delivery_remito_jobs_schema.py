from django.core.management.base import BaseCommand
from django.db import connection, transaction


TABLE_NAME = "delivery_remito_jobs"

REQUIRED_COLUMNS = {
    "id",
    "actor_user_id",
    "order_ids",
    "request_payload",
    "status",
    "result_payload",
    "error_payload",
    "created_at",
    "started_at",
    "finished_at",
}


class Command(BaseCommand):
    help = "Aplica el esquema de los trabajos de emisión de remitos de órdenes de entrega"

    def handle(self, *args, **opts):
        if connection.vendor != "postgresql":
            raise RuntimeError("apply_delivery_remito_jobs_schema requiere PostgreSQL")

        with transaction.atomic():
            with connection.cursor() as cur:
                # La emisión dejó de vivir dentro del request HTTP. El navegador
                # congela las pestañas que quedan en segundo plano, así que atar el
                # resultado a que esa pestaña siga despierta hacía que la emisión
                # pareciera colgada durante una hora: el trabajo estaba hecho pero
                # nadie recogía la respuesta. Esta tabla es donde queda el resultado
                # esperando a que alguien lo venga a buscar, sea la pestaña original,
                # otra, o la misma después de recargar.
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS delivery_remito_jobs (
                      id               TEXT PRIMARY KEY,
                      actor_user_id    INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      order_ids        JSONB NOT NULL DEFAULT '[]'::jsonb,
                      request_payload  JSONB NOT NULL DEFAULT '{}'::jsonb,
                      status           TEXT NOT NULL DEFAULT 'pending',
                      result_payload   JSONB NULL,
                      error_payload    JSONB NULL,
                      -- clock_timestamp() y no CURRENT_TIMESTAMP: hay que poder medir
                      -- cuánto tardó de verdad cada tramo, y CURRENT_TIMESTAMP devuelve
                      -- la hora en que arrancó la transacción.
                      created_at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
                      started_at       TIMESTAMPTZ NULL,
                      finished_at      TIMESTAMPTZ NULL,
                      CONSTRAINT chk_delivery_remito_jobs_status
                        CHECK (status IN ('pending', 'running', 'done', 'error'))
                    )
                    """
                )
                # Para que el POST pueda reusar un trabajo ya en curso de las mismas
                # órdenes en vez de largar una segunda emisión.
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_delivery_remito_jobs_abiertos
                      ON delivery_remito_jobs (status, created_at DESC)
                     WHERE status IN ('pending', 'running')
                    """
                )
                cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_delivery_remito_jobs_actor
                      ON delivery_remito_jobs (actor_user_id, created_at DESC)
                    """
                )

                cur.execute(
                    """
                    SELECT column_name FROM information_schema.columns
                     WHERE table_name = %s
                    """,
                    [TABLE_NAME],
                )
                present = {row[0] for row in cur.fetchall()}

        faltantes = REQUIRED_COLUMNS - present
        if faltantes:
            raise RuntimeError(f"{TABLE_NAME} quedó incompleta, faltan: {sorted(faltantes)}")
        self.stdout.write(self.style.SUCCESS(f"{TABLE_NAME} OK"))
