from django.core.management.base import BaseCommand
from django.db import connection, transaction


class Command(BaseCommand):
    help = "Aplica Fase 3: índices únicos normalizados y triggers de snapshot de devices."

    def handle(self, *args, **opts):
        with transaction.atomic():
            with connection.cursor() as cur:
                # Asegurar columna ubicacion_id en devices
                cur.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS ubicacion_id INTEGER NULL REFERENCES locations(id) ON DELETE SET NULL")

                # Índices únicos (idempotentes)
                # OJO: este índice quedó superado por apply_devices_ns_tipo_unique_schema, que lo
                # reemplaza por uno que incluye el tipo de equipo (el humidificador comparte la
                # serie del CPAP). Volver a crearlo acá encima del nuevo rompería esos ingresos,
                # así que sólo se crea si el reemplazo todavía no está puesto.
                cur.execute("SELECT to_regclass('public.uq_devices_ns_tipo_norm')")
                if not (cur.fetchone() or [None])[0]:
                    cur.execute(
                        """
                        CREATE UNIQUE INDEX IF NOT EXISTS uq_devices_ns_norm
                          ON devices ((UPPER(REPLACE(REPLACE(numero_serie, ' ', ''), '-', ''))))
                          WHERE NULLIF(TRIM(numero_serie), '') IS NOT NULL;
                        """
                    )
                # Intentar crear índice único sobre numero_interno normalizado. Si hay duplicados, degradar a índice normal
                try:
                    cur.execute(
                        """
                        CREATE UNIQUE INDEX IF NOT EXISTS uq_devices_numint_norm
                          ON devices ((UPPER(REGEXP_REPLACE(numero_interno,
                               '^(MG|NM|NV|CE)\s*(\d{1,4})$', '\\1 ' || LPAD('\\2',4,'0')))))
                          WHERE numero_interno ~* '^(MG|NM|NV|CE)\s*\d{1,4}$';
                        """
                    )
                except Exception:
                    # Generar reporte de duplicados y crear índice no-único
                    cur.execute(
                        """
                        SELECT UPPER(REGEXP_REPLACE(numero_interno,
                                   '^(MG|NM|NV|CE)\s*(\d{1,4})$', '\\1 ' || LPAD('\\2',4,'0'))) AS key,
                               ARRAY_AGG(id ORDER BY id)
                          FROM devices
                         WHERE numero_interno ~* '^(MG|NM|NV|CE)\s*\d{1,4}$'
                         GROUP BY 1
                        HAVING COUNT(*) > 1
                        ORDER BY 1
                        """
                    )
                    dups = cur.fetchall() or []
                    if dups:
                        # Guardar CSV en docs
                        try:
                            import os, csv
                            docs_dir = os.path.join(os.getcwd(), "docs")
                            os.makedirs(docs_dir, exist_ok=True)
                            path = os.path.join(docs_dir, "devices_numint_duplicates.csv")
                            with open(path, "w", newline="", encoding="utf-8") as f:
                                w = csv.writer(f)
                                w.writerow(["numero_interno_norm", "device_ids"])
                                for k, ids in dups:
                                    w.writerow([k, ",".join(map(str, ids))])
                        except Exception:
                            pass
                    # Crear índice no único como fallback
                    cur.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_devices_numint_norm
                          ON devices ((UPPER(REGEXP_REPLACE(numero_interno,
                               '^(MG|NM|NV|CE)\s*(\d{1,4})$', '\\1 ' || LPAD('\\2',4,'0')))))
                          WHERE numero_interno ~* '^(MG|NM|NV|CE)\s*\d{1,4}$';
                        """
                    )

                # Backfill ubicacion_id snapshot desde último ingreso
                cur.execute(
                    """
                    WITH last_i AS (
                      SELECT d.id AS device_id,
                             (
                               SELECT t.ubicacion_id
                                 FROM ingresos t
                                WHERE t.device_id = d.id
                                ORDER BY COALESCE(t.fecha_ingreso, t.fecha_creacion) DESC, t.id DESC
                                LIMIT 1
                             ) AS ubic
                        FROM devices d
                    )
                    UPDATE devices d
                       SET ubicacion_id = COALESCE(li.ubic, d.ubicacion_id)
                      FROM last_i li
                     WHERE d.id = li.device_id
                    """
                )

                # Función + triggers de snapshot
                cur.execute(
                    """
                    CREATE OR REPLACE FUNCTION sync_device_snapshot()
                    RETURNS TRIGGER AS $$
                    DECLARE
                      v_device_id INTEGER;
                      v_last_id INTEGER;
                      v_alquilado BOOLEAN;
                      v_alquiler_a TEXT;
                      v_propietario_nombre TEXT;
                      v_faja TEXT;
                      v_ubic_id INTEGER;
                      v_is_own BOOLEAN;
                      v_mgbio_id INTEGER;
                    BEGIN
                      v_device_id := COALESCE(NEW.device_id, OLD.device_id);

                      SELECT t.id, t.alquilado, t.alquiler_a, t.propietario_nombre, t.faja_garantia, t.ubicacion_id
                        INTO v_last_id, v_alquilado, v_alquiler_a, v_propietario_nombre, v_faja, v_ubic_id
                        FROM ingresos t
                       WHERE t.device_id = v_device_id
                       ORDER BY COALESCE(t.fecha_ingreso, t.fecha_creacion) DESC, t.id DESC
                       LIMIT 1;

                      SELECT (CASE WHEN d.numero_serie ~* '^(MG|NM|NV)\s*\d{1,4}$' THEN TRUE ELSE FALSE END)
                        INTO v_is_own
                        FROM devices d
                       WHERE d.id = v_device_id;

                      IF v_is_own THEN
                        SELECT id INTO v_mgbio_id FROM customers
                         WHERE LOWER(razon_social) LIKE '%mg%bio%'
                         ORDER BY id ASC LIMIT 1;
                      END IF;

                      UPDATE devices d
                         SET alquilado = COALESCE(v_alquilado, FALSE),
                             alquiler_a = v_alquiler_a,
                             ubicacion_id = COALESCE(v_ubic_id, d.ubicacion_id),
                             n_de_control = COALESCE(NULLIF(v_faja, ''), d.n_de_control),
                             propietario = CASE WHEN v_is_own THEN COALESCE(v_propietario_nombre, d.propietario) ELSE d.propietario END,
                             customer_id = CASE WHEN v_is_own AND v_mgbio_id IS NOT NULL THEN v_mgbio_id ELSE d.customer_id END
                       WHERE d.id = v_device_id;

                      RETURN NULL;
                    END;
                    $$ LANGUAGE plpgsql;

                    DROP TRIGGER IF EXISTS trg_sync_device_snapshot_ins ON ingresos;
                    DROP TRIGGER IF EXISTS trg_sync_device_snapshot_upd ON ingresos;
                    DROP TRIGGER IF EXISTS trg_sync_device_snapshot_del ON ingresos;

                    CREATE TRIGGER trg_sync_device_snapshot_ins
                    AFTER INSERT ON ingresos
                    FOR EACH ROW EXECUTE FUNCTION sync_device_snapshot();

                    CREATE TRIGGER trg_sync_device_snapshot_upd
                    AFTER UPDATE OF device_id, fecha_ingreso, fecha_creacion, ubicacion_id, alquiler_a, alquilado, faja_garantia, propietario_nombre ON ingresos
                    FOR EACH ROW EXECUTE FUNCTION sync_device_snapshot();

                    CREATE TRIGGER trg_sync_device_snapshot_del
                    AFTER DELETE ON ingresos
                    FOR EACH ROW EXECUTE FUNCTION sync_device_snapshot();
                    """
                )

        self.stdout.write("APLICADO OK: Fase 3 (índices y triggers)")
