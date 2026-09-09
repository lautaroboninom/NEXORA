-- Resincronización de presupuesto_estado, segunda pasada  (08/09/2026)
--
-- El primer script (presupuesto_no_aplica_resync_20260908.sql) filtraba por
-- `ingresos.estado IN ('controlado_sin_defecto','no_se_repara')`, y eso deja afuera a
-- toda OS que ya avanzó: entregada, liberada o alquilada. Se vio en vivo con la OS
-- 29674, que se entregó entre la auditoría y la corrida y por eso no la agarró.
--
-- El criterio correcto es la **resolución**, que no se pisa al avanzar de estado: si
-- la OS se cerró como "no se encontró falla" o "no reparado" y la versión vigente del
-- presupuesto está pendiente y vacía, ese presupuesto nunca existió — es el
-- `no_aplica` que le comió el trigger `sync_quote_with_ingreso`.
--
-- Guardas: sólo versiones **pendientes**, sin ítems y con subtotal 0. Un presupuesto
-- emitido, aprobado o rechazado nunca se toca (verificado antes de correr: ninguna de
-- las 24 OS tenía más de una versión ni ítems ni importe).
--
-- Uso:  psql ... -v ON_ERROR_STOP=1 -f presupuesto_no_aplica_resync_por_resolucion_20260908.sql

BEGIN;

WITH cur AS (
  SELECT DISTINCT ON (ingreso_id) ingreso_id, id AS quote_id, estado::text AS quote_estado
    FROM quotes
   ORDER BY ingreso_id, COALESCE(version_num, 1) DESC, id DESC
)
UPDATE quotes q
   SET estado = 'no_aplica'
  FROM cur
  JOIN ingresos i ON i.id = cur.ingreso_id
 WHERE q.id = cur.quote_id
   AND i.resolucion::text IN ('no_se_encontro_falla', 'no_reparado')
   AND i.presupuesto_estado::text <> 'no_aplica'
   AND cur.quote_estado = 'pendiente'
   AND COALESCE(q.subtotal, 0) = 0
   AND NOT EXISTS (SELECT 1 FROM quote_items qi WHERE qi.quote_id = q.id);

\echo == Deberia dar 0 filas ==
WITH cur AS (
  SELECT DISTINCT ON (ingreso_id) ingreso_id, id AS quote_id, estado::text AS quote_estado
    FROM quotes
   ORDER BY ingreso_id, COALESCE(version_num, 1) DESC, id DESC
)
SELECT i.id AS os, i.estado::text AS estado, i.resolucion::text AS resolucion,
       i.presupuesto_estado::text AS presu, cur.quote_estado
  FROM ingresos i JOIN cur ON cur.ingreso_id = i.id
 WHERE i.resolucion::text IN ('no_se_encontro_falla', 'no_reparado')
   AND i.presupuesto_estado::text <> 'no_aplica'
   AND cur.quote_estado = 'pendiente'
   AND NOT EXISTS (SELECT 1 FROM quote_items qi WHERE qi.quote_id = cur.quote_id)
 ORDER BY i.id;

COMMIT;
