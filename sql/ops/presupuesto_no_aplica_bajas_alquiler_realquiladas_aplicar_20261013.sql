-- Presupuesto "no aplica" para 13 OS de 2026 con motivo "baja alquiler" que ya volvieron a salir
-- alquiladas (estado 'alquilado', la mayoría con RTA). Pedido del usuario 13/10/2026: de las 21 OS
-- 'alquilado' con presupuesto pendiente en la cola, "no aplica" a las de baja alquiler; las 8 de
-- "reparación" / "urgente control" (28992, 29073, 29098, 29117, 29118, 29121, 29269, 29298) quedan
-- pendientes para Comercial.
--
-- Se escribe `quotes` (el trigger sync_quote_with_ingreso copia la versión vigente a ingresos):
-- se pisa una versión pendiente o vacía, o se crea la versión 1 en no_aplica si no hay ninguna.
--
-- Uso:  psql ... -v ON_ERROR_STOP=1 -f presupuesto_no_aplica_bajas_alquiler_realquiladas_aplicar_20261013.sql

BEGIN;

CREATE TEMP TABLE objetivo (id integer PRIMARY KEY) ON COMMIT DROP;
INSERT INTO objetivo VALUES
  (29348), (29476), (29537), (29607), (29609), (29651), (29652),
  (29711), (29715), (29717), (29777), (29816), (29832);

DO $$
DECLARE v integer;
BEGIN
  SELECT COUNT(*) INTO v
    FROM objetivo o JOIN ingresos t ON t.id = o.id
   WHERE t.estado = 'alquilado'
     AND LOWER(BTRIM(t.motivo::text)) = 'baja alquiler'
     AND COALESCE(t.presupuesto_estado, 'pendiente') = 'pendiente';
  IF v <> 13 THEN RAISE EXCEPTION 'Se esperaban 13 OS en condiciones y hay %: no se aplica nada', v; END IF;
END $$;

\echo == Versión vigente pendiente o vacía -> no_aplica ==
UPDATE quotes q
   SET estado = 'no_aplica'
  FROM (
    SELECT DISTINCT ON (ingreso_id) ingreso_id, id AS quote_id, estado::text AS quote_estado
      FROM quotes
     ORDER BY ingreso_id, COALESCE(version_num, 1) DESC, id DESC
  ) cur
 WHERE q.id = cur.quote_id
   AND COALESCE(cur.quote_estado, '') IN ('', 'pendiente')
   AND cur.ingreso_id IN (SELECT id FROM objetivo);

\echo == Sin fila en quotes -> versión 1 en no_aplica ==
INSERT INTO quotes(ingreso_id, version_num, estado)
SELECT o.id, 1, 'no_aplica'
  FROM objetivo o
 WHERE NOT EXISTS (SELECT 1 FROM quotes q2 WHERE q2.ingreso_id = o.id);

DO $$
DECLARE v integer;
BEGIN
  SELECT COUNT(*) INTO v FROM ingresos WHERE id IN (SELECT id FROM objetivo) AND presupuesto_estado <> 'no_aplica';
  IF v > 0 THEN RAISE EXCEPTION 'Quedan % OS sin no_aplica: no se aplica nada', v; END IF;
END $$;

SELECT t.presupuesto_estado::text, COUNT(*) FROM ingresos t WHERE t.id IN (SELECT id FROM objetivo) GROUP BY 1;

COMMIT;
