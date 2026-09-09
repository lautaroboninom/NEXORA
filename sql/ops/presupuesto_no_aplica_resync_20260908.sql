-- Resincronización de presupuesto_estado vs quotes  (08/09/2026)
--
-- Contexto: `ingresos.presupuesto_estado` no es la fuente de verdad. El trigger
-- `sync_quote_with_ingreso` la reescribe con el estado de la versión vigente de
-- `quotes` cada vez que se toca esa tabla. Los cierres sin presupuesto
-- ("controlado sin defecto" y "no se repara") marcaban sólo el ingreso, así que la
-- quote quedaba en 'pendiente' y el 'no_aplica' duraba hasta el primer toque a
-- `quotes` -incluida la creación al vuelo que dispara abrir la pestaña Presupuesto-.
-- 62 cambios 'no_aplica' -> 'pendiente' en el change_log desde 10/2025, ninguno
-- hecho por una persona.
--
-- El código ya no genera casos nuevos (marcar_quote_no_aplica + _quote_seed_estado).
-- Esto arregla los que quedaron. Correr DESPUÉS de deployar el fix, si no la propia
-- normalización del rebuild los vuelve a dar vuelta.
--
-- Uso:  psql ... -v ON_ERROR_STOP=1 -1 -f presupuesto_no_aplica_resync_20260908.sql

BEGIN;

-- 1) OS cerradas sin falla / sin reparar cuyo presupuesto se reabrió solo.
--    Sólo las que tienen la versión vigente vacía: si hay ítems o plata, no se toca.
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
   AND i.estado::text IN ('controlado_sin_defecto', 'no_se_repara')
   AND cur.quote_estado = 'pendiente'
   AND COALESCE(q.subtotal, 0) = 0
   AND NOT EXISTS (SELECT 1 FROM quote_items qi WHERE qi.quote_id = q.id);

-- 2) OS que el operador dejó en 'no_aplica' y la quote vigente contradice con una
--    versión pendiente vacía. Es la misma desincronización, todavía sin disparar:
--    da vuelta sola en el próximo rebuild (apply_quote_versions_schema normaliza).
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
   AND i.presupuesto_estado::text = 'no_aplica'
   AND cur.quote_estado = 'pendiente'
   AND COALESCE(q.subtotal, 0) = 0
   AND NOT EXISTS (SELECT 1 FROM quote_items qi WHERE qi.quote_id = q.id);

-- 3) Al revés: el ingreso dice 'no_aplica' tapando un presupuesto que el cliente
--    rechazó de verdad (con ítems y plata). Manda la quote: ese 'rechazado' es lo
--    que hace que métricas cobre el neto pactado y no el costo de los repuestos.
--    Se escribe el ingreso; el trigger no hace falta porque `quotes` no se toca.
WITH cur AS (
  SELECT DISTINCT ON (ingreso_id) ingreso_id, id AS quote_id, estado::text AS quote_estado
    FROM quotes
   ORDER BY ingreso_id, COALESCE(version_num, 1) DESC, id DESC
)
UPDATE ingresos i
   SET presupuesto_estado = cur.quote_estado::quote_estado
  FROM cur
 WHERE cur.ingreso_id = i.id
   AND i.presupuesto_estado::text = 'no_aplica'
   AND cur.quote_estado IN ('presupuestado', 'aprobado', 'rechazado');

-- Control: después de esto no tiene que quedar ninguna fila.
\echo == Deberia dar 0 filas ==
WITH cur AS (
  SELECT DISTINCT ON (ingreso_id) ingreso_id, estado::text AS quote_estado
    FROM quotes
   ORDER BY ingreso_id, COALESCE(version_num, 1) DESC, id DESC
)
SELECT i.id AS os, i.estado::text AS estado,
       i.presupuesto_estado::text AS presu_ingreso, cur.quote_estado
  FROM ingresos i JOIN cur ON cur.ingreso_id = i.id
 WHERE i.presupuesto_estado::text IS DISTINCT FROM cur.quote_estado
 ORDER BY i.id;

COMMIT;

-- Queda afuera a propósito: OS 29246 (controlado_sin_defecto con la quote vigente
-- en 'aprobado', 0 ítems y $0). Es de 29/04/2026 y no se sabe si el 'aprobado' fue
-- una decisión o el mismo arrastre; pasarla a 'no_aplica' es una llamada de negocio,
-- no de datos.
