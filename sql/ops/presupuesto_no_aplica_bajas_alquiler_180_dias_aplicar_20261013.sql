-- Regla del usuario (13/10/2026): "si pasaron 180 días de que no tiene presupuesto y es un equipo
-- propio que volvió de alquiler, ponelo como No aplica".
--
-- Criterio: OS en la cola de Pendientes de presupuesto, equipo propio (código MG/NM/NV/CE, no
-- vendido), motivo "baja alquiler" o "reparación alquiler", presupuesto pendiente (sin emitir) y
-- 180 días o más desde la fecha de ingreso (hora de Buenos Aires). Se exige una fecha de ingreso
-- posterior al año 2000: la OS 29664 tiene 0026-08-28 (es de agosto de 2026) y daba 730.531 días.
--
-- Medido en prod el 13/10/2026: 10 OS, todas "baja alquiler" en diagnosticado: 28858, 29088, 29094,
-- 29147, 29150, 29151, 29152, 29153, 29178, 29286 (7 son el lote que devolvió PRAXAIR el 25/03).
--
-- Se escribe `quotes` (el trigger sync_quote_with_ingreso copia la versión vigente a ingresos).
--
-- Uso:  psql ... -v ON_ERROR_STOP=1 -f presupuesto_no_aplica_bajas_alquiler_180_dias_aplicar_20261013.sql

BEGIN;

CREATE TEMP TABLE objetivo ON COMMIT DROP AS
SELECT t.id
  FROM ingresos t
  JOIN devices d ON d.id = t.device_id
 WHERE COALESCE(t.presupuesto_estado, 'pendiente') = 'pendiente'
   AND t.estado NOT IN ('ingresado','entregado','liberado','no_se_repara',
                        'vendido_pendiente_entrega','vendido_entregado')
   AND BTRIM(COALESCE(d.numero_interno,'')) ~* '^(MG|NM|NV|CE)\s*0*(\d{1,6})$'
   AND LOWER(BTRIM(COALESCE(d.mg_estado,'activo'))) <> 'inactivo_venta'
   AND LOWER(BTRIM(t.motivo::text)) IN ('baja alquiler','reparación alquiler','reparacion alquiler')
   AND t.fecha_ingreso >= '2000-01-01'
   AND CURRENT_DATE - (t.fecha_ingreso AT TIME ZONE 'America/Argentina/Buenos_Aires')::date >= 180;

DO $$
DECLARE v integer;
BEGIN
  SELECT COUNT(*) INTO v FROM objetivo;
  RAISE NOTICE 'OS objetivo: %', v;
  IF v <> 10 THEN RAISE EXCEPTION 'Se esperaban 10 OS y hay %: no se aplica nada', v; END IF;
END $$;

\echo == Versión vigente pendiente o vacía -> no_aplica (esperado: UPDATE 1) ==
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

\echo == Sin fila en quotes -> versión 1 en no_aplica (esperado: INSERT 0 9) ==
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

SELECT t.id, d.numero_interno, t.presupuesto_estado::text
  FROM ingresos t JOIN devices d ON d.id = t.device_id
 WHERE t.id IN (SELECT id FROM objetivo) ORDER BY t.id;

COMMIT;
