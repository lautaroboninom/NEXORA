-- Medicion (solo lectura) de OS "alquilado" (equipos propios) con fecha de ingreso
-- 2025 que hoy figuran en la cola "Pendientes de Presupuesto" (mismo criterio que
-- PendientesPresupuestoView en ingresos_views.py, acotado por el usuario a
-- estado='alquilado' el 14/09/2026). Son casos viejos que reaparecieron como
-- pendientes despues del fix del trigger sync_quote_with_ingreso (ver memoria
-- nexora-presupuesto-no-aplica-lo-pisa-el-trigger / commit 90428c9) y hay que
-- marcarlos "no aplica" en lugar de tramitarlos.
--
-- No escribe nada. Correr ANTES de presupuesto_no_aplica_ingresos_2025_aplicar_20260914.sql
-- para confirmar el alcance (cuantas filas, que pinta tienen) antes de tocar produccion.
--
-- Uso:  psql ... -v ON_ERROR_STOP=1 -f presupuesto_no_aplica_ingresos_2025_medir_20260914.sql

WITH cur AS (
  SELECT DISTINCT ON (ingreso_id)
         ingreso_id,
         id AS quote_id,
         estado::text AS quote_estado,
         COALESCE(subtotal, 0) AS subtotal
    FROM quotes
   ORDER BY ingreso_id, COALESCE(version_num, 1) DESC, id DESC
),
objetivo AS (
  SELECT
    t.id AS ingreso_id,
    t.estado,
    t.resolucion::text AS resolucion,
    t.presupuesto_estado::text AS presupuesto_estado,
    (t.fecha_ingreso AT TIME ZONE 'America/Argentina/Buenos_Aires')::date AS fecha_ingreso,
    c.razon_social AS cliente,
    COALESCE(b.nombre, '') AS marca,
    COALESCE(m.nombre, '') AS modelo,
    d.numero_interno,
    d.numero_serie,
    d.mg_estado,
    t.motivo,
    cur.quote_estado,
    cur.subtotal,
    -- Lo que tocaria el script de aplicacion: version pendiente/vacia, o sin quote todavia.
    (cur.quote_id IS NULL OR COALESCE(cur.quote_estado, '') IN ('', 'pendiente')) AS sera_actualizado
  FROM ingresos t
  JOIN devices d ON d.id = t.device_id
  JOIN customers c ON c.id = d.customer_id
  LEFT JOIN marcas b ON b.id = d.marca_id
  LEFT JOIN models m ON m.id = d.model_id
  LEFT JOIN cur ON cur.ingreso_id = t.id
  WHERE t.estado = 'alquilado'
    AND COALESCE(t.presupuesto_estado, 'pendiente') = 'pendiente'
    AND EXTRACT(YEAR FROM (t.fecha_ingreso AT TIME ZONE 'America/Argentina/Buenos_Aires')) = 2025
    AND (
      (
        BTRIM(COALESCE(d.numero_interno,'')) ~* '^(MG|NM|NV|CE)\s*0*(\d{1,6})$'
        AND LOWER(BTRIM(COALESCE(d.mg_estado,'activo'))) <> 'inactivo_venta'
      )
      OR LOWER(BTRIM(COALESCE(t.motivo::text,''))) IN ('baja alquiler','reparación alquiler','reparacion alquiler')
    )
)
SELECT * FROM objetivo ORDER BY fecha_ingreso, ingreso_id;

\echo == Total (deberian ser todas equipo propio / MG, por el filtro de arriba) ==
WITH cur AS (
  SELECT DISTINCT ON (ingreso_id)
         ingreso_id, id AS quote_id, estado::text AS quote_estado
    FROM quotes
   ORDER BY ingreso_id, COALESCE(version_num, 1) DESC, id DESC
)
SELECT COUNT(*) AS total,
       COUNT(*) FILTER (WHERE cur.quote_id IS NULL OR COALESCE(cur.quote_estado,'') IN ('','pendiente')) AS se_actualizarian,
       COUNT(*) FILTER (
         WHERE NOT (
           BTRIM(COALESCE(d.numero_interno,'')) ~* '^(MG|NM|NV|CE)\s*0*(\d{1,6})$'
           AND LOWER(BTRIM(COALESCE(d.mg_estado,'activo'))) <> 'inactivo_venta'
         )
       ) AS entraron_solo_por_motivo_no_por_codigo_mg
  FROM ingresos t
  JOIN devices d ON d.id = t.device_id
  LEFT JOIN cur ON cur.ingreso_id = t.id
 WHERE t.estado = 'alquilado'
   AND COALESCE(t.presupuesto_estado, 'pendiente') = 'pendiente'
   AND EXTRACT(YEAR FROM (t.fecha_ingreso AT TIME ZONE 'America/Argentina/Buenos_Aires')) = 2025
   AND (
     (
       BTRIM(COALESCE(d.numero_interno,'')) ~* '^(MG|NM|NV|CE)\s*0*(\d{1,6})$'
       AND LOWER(BTRIM(COALESCE(d.mg_estado,'activo'))) <> 'inactivo_venta'
     )
     OR LOWER(BTRIM(COALESCE(t.motivo::text,''))) IN ('baja alquiler','reparación alquiler','reparacion alquiler')
   );
