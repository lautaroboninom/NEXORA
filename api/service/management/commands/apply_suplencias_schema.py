"""Esquema de las suplencias temporales.

Reemplazar a alguien mientras no está se venía haciendo a mano: cambiarle el rol a
otra persona, agregarle un permiso, prender doce notificaciones una por una, y
acordarse de deshacer las tres cosas al volver. Lo último es lo que falla — nadie
revierte lo que nadie anotó, y el reemplazo se queda con permisos que ya no le
corresponden.

Esta tabla existe para eso: guardar **cómo estaba la persona antes**, para poder
devolverla exactamente ahí. El `snapshot` no es informativo, es el mecanismo.
"""

from django.core.management.base import BaseCommand
from django.db import connection, transaction

TABLE_NAME = "suplencias"


class Command(BaseCommand):
    help = "Aplica el esquema de suplencias temporales"

    def handle(self, *args, **opts):
        if connection.vendor != "postgresql":
            raise RuntimeError("apply_suplencias_schema requiere PostgreSQL")

        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                      id                INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                      titular_user_id   INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      suplente_user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                      motivo            TEXT NOT NULL DEFAULT '',
                      estado            TEXT NOT NULL DEFAULT 'activa',
                      -- Cómo estaba el suplente antes de la suplencia: su rol, qué
                      -- permisos se le agregaron (sólo los que NO tenía) y en qué
                      -- estado estaba cada preferencia de notificación que se tocó.
                      -- Cerrar la suplencia es reescribir esto tal cual estaba.
                      snapshot          JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                      abierta_por       INTEGER NULL REFERENCES users(id) ON DELETE SET NULL,
                      abierta_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      cerrada_at        TIMESTAMPTZ NULL,
                      CONSTRAINT chk_suplencias_estado CHECK (estado IN ('activa', 'cerrada')),
                      CONSTRAINT chk_suplencias_distintos
                        CHECK (titular_user_id IS NULL OR titular_user_id <> suplente_user_id)
                    )
                    """
                )
                # Dos suplencias activas sobre la misma persona dejarían dos snapshots
                # y el segundo cierre restauraría un estado que ya no era el original.
                cur.execute(
                    f"""
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_{TABLE_NAME}_suplente_activa
                      ON {TABLE_NAME} (suplente_user_id)
                     WHERE estado = 'activa'
                    """
                )

        self.stdout.write(self.style.SUCCESS("APLICADO OK: esquema de suplencias temporales"))
