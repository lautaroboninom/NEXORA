-- Aplica "no aplica" al presupuesto de las OS que HOY estan en estado 'baja' y no tienen un
-- presupuesto emitido. Pedido del usuario 15/09/2026: la cola "Pendientes de Presupuesto"
-- deja de excluir 'baja' (colas_views.py y PendientesPresupuestoView) para las bajas que
-- vengan, pero las que ya existen no tienen que aparecer: con esto la cola queda igual.
--
-- Correr ANTES del rebuild que sube el cambio de regla. Una OS que pase a baja despues de
-- esta corrida ya es de las que vienen y puede quedar pendiente, que es lo buscado.
--
-- Medido en prod el 15/09/2026 con presupuesto_no_aplica_bajas_medir_20260915.sql:
-- 2000 OS en baja; se tocan las 1933 con presupuesto pendiente:
--   1899 sin fila en quotes                     -> INSERT de la version 1 en no_aplica
--     31 version vigente pendiente y vacia      -> UPDATE a no_aplica
--      3 version vigente pendiente con importe o items, nunca emitida (OS 17255, 17752 y
--        17761, de 2017-2018)                    -> UPDATE a no_aplica; importe e items quedan
--   0 anuladas, 0 desincronizadas. Las 40 presupuestadas, 11 aprobadas y 16 no_aplica no
--   se tocan.
--
-- Se escribe `quotes`, no `ingresos.presupuesto_estado`: el trigger sync_quote_with_ingreso
-- copia la version vigente a `ingresos` y pisaria cualquier otro valor (ver
-- marcar_quote_no_aplica() en quotes_views.py). Misma regla de seguridad que esa funcion:
-- solo se pisa una version pendiente (o vacia), o se crea la fila si no hay ninguna.
-- Sin CTE compartido entre sentencias (ver la v1 fallida de
-- presupuesto_no_aplica_ingresos_2025_aplicar_20260914.sql).
--
-- Para deshacer: audit.change_log tiene cada ingresos.presupuesto_estado pendiente -> no_aplica
-- con la hora de esta corrida; el UPDATE lista abajo sus OS.
--
-- Uso:  psql ... -v ON_ERROR_STOP=1 -f presupuesto_no_aplica_bajas_aplicar_20260915.sql

BEGIN;

\echo == 1) Version vigente pendiente o vacia -> no_aplica (esperado: UPDATE 34) ==
UPDATE quotes q
   SET estado = 'no_aplica'
  FROM (
    SELECT DISTINCT ON (ingreso_id) ingreso_id, id AS quote_id, estado::text AS quote_estado
      FROM quotes
     ORDER BY ingreso_id, COALESCE(version_num, 1) DESC, id DESC
  ) cur
  JOIN ingresos t ON t.id = cur.ingreso_id
 WHERE q.id = cur.quote_id
   AND COALESCE(cur.quote_estado, '') IN ('', 'pendiente')
   AND t.estado = 'baja'
   AND COALESCE(t.presupuesto_estado, 'pendiente') = 'pendiente'
RETURNING q.ingreso_id AS os, q.id AS quote_id;

\echo == 2) Sin ninguna fila en quotes -> nace la version 1 en no_aplica (esperado: INSERT 0 1899) ==
INSERT INTO quotes(ingreso_id, version_num, estado)
SELECT t.id, 1, 'no_aplica'
  FROM ingresos t
 WHERE t.estado = 'baja'
   AND COALESCE(t.presupuesto_estado, 'pendiente') = 'pendiente'
   AND NOT EXISTS (SELECT 1 FROM quotes q2 WHERE q2.ingreso_id = t.id);

-- 3) Control: no puede quedar ninguna baja con presupuesto pendiente, porque esas son las que
-- aparecerian en la cola con la regla nueva. Si queda alguna (una version anulada, o una OS
-- que paso a baja con un caso no medido), se aborta todo y no se aplica nada.
DO $$
DECLARE
  v_quedan integer;
BEGIN
  SELECT COUNT(*) INTO v_quedan
    FROM ingresos t
   WHERE t.estado = 'baja'
     AND COALESCE(t.presupuesto_estado, 'pendiente') = 'pendiente';
  IF v_quedan > 0 THEN
    RAISE EXCEPTION 'Quedan % OS en baja con presupuesto pendiente: no se aplica nada', v_quedan;
  END IF;
END $$;

\echo == Bajas por estado de presupuesto despues de aplicar (esperado: 1949 no_aplica, 40 presupuestado, 11 aprobado) ==
SELECT COALESCE(t.presupuesto_estado::text, '(null)') AS presupuesto_estado, COUNT(*) AS os
  FROM ingresos t
 WHERE t.estado = 'baja'
 GROUP BY 1
 ORDER BY 2 DESC;

COMMIT;
