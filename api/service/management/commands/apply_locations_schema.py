"""Esquema y consolidación de ubicaciones de dominio (locations).

Garantiza la presencia de las ubicaciones canónicas del dominio y consolida
aliases históricos (por ejemplo 'Depósito SEPID' o variantes de escritura/mojibake
hacia 'Sepid', con depósito VAL en Bejerman), reasignando todas las referencias
de ingresos y devices a la ubicación canónica antes de remover duplicados.
"""

from django.core.management.base import BaseCommand
from django.db import connection, transaction

from service.views.helpers_impl import ensure_default_locations


class Command(BaseCommand):
    help = "Aplica el esquema y consolidación de ubicaciones canónicas (locations)"

    def handle(self, *args, **opts):
        if connection.vendor != "postgresql":
            raise RuntimeError("apply_locations_schema requiere PostgreSQL")

        with transaction.atomic():
            with connection.cursor() as cur:
                # 1. Asegurar tabla base de locations
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS locations (
                      id      INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                      nombre  TEXT NOT NULL UNIQUE
                    )
                    """
                )
                # 2. Consolidación explícita a nivel SQL para entornos existentes
                cur.execute(
                    """
                    DO $$
                    DECLARE
                      v_id_sepid INTEGER;
                      r RECORD;
                    BEGIN
                      IF NOT EXISTS (SELECT 1 FROM locations WHERE nombre = 'Sepid') THEN
                        SELECT id INTO v_id_sepid
                          FROM locations
                         WHERE LOWER(TRIM(nombre)) IN (
                           'deposito sepid', 'depósito sepid', 'deposito sepid s.a.', 'depósito sepid s.a.',
                           'deposito sepid sa', 'depósito sepid sa', 'sepid s.a.', 'sepid sa',
                           'deposito sepid (val)', 'depósito sepid (val)', 'deposito sepid val', 'depósito sepid val',
                           'sepid (val)', 'sepid val',
                           'deposito central', 'depósito central', 'deposito central sepid', 'depósito central sepid',
                           'valdenegro', 'deposito valdenegro', 'depósito valdenegro', 'valdenegro 4578'
                         )
                         ORDER BY id
                         LIMIT 1;

                        IF v_id_sepid IS NOT NULL THEN
                          UPDATE locations SET nombre = 'Sepid' WHERE id = v_id_sepid;
                        ELSE
                          INSERT INTO locations(nombre) VALUES ('Sepid') ON CONFLICT (nombre) DO NOTHING;
                        END IF;
                      END IF;

                      SELECT id INTO v_id_sepid FROM locations WHERE nombre = 'Sepid' LIMIT 1;

                      IF v_id_sepid IS NOT NULL THEN
                        FOR r IN
                          SELECT id, nombre
                            FROM locations
                           WHERE id <> v_id_sepid
                             AND LOWER(TRIM(nombre)) IN (
                               'deposito sepid', 'depósito sepid', 'deposito sepid s.a.', 'depósito sepid s.a.',
                               'deposito sepid sa', 'depósito sepid sa', 'sepid s.a.', 'sepid sa',
                               'deposito sepid (val)', 'depósito sepid (val)', 'deposito sepid val', 'depósito sepid val',
                               'sepid (val)', 'sepid val',
                               'deposito central', 'depósito central', 'deposito central sepid', 'depósito central sepid',
                               'valdenegro', 'deposito valdenegro', 'depósito valdenegro', 'valdenegro 4578'
                             )
                        LOOP
                          UPDATE ingresos SET ubicacion_id = v_id_sepid WHERE ubicacion_id = r.id;
                          UPDATE devices SET ubicacion_id = v_id_sepid WHERE ubicacion_id = r.id;
                          DELETE FROM locations WHERE id = r.id;
                        END LOOP;
                      END IF;
                    END $$;
                    """
                )

            # 3. Consolidación de todos los nombres por defecto y sus remapeos
            ensure_default_locations()

        self.stdout.write(self.style.SUCCESS("APLICADO OK: esquema y consolidación de ubicaciones"))
