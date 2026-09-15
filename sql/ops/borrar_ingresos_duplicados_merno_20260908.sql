-- Borra las 4 OS duplicadas de MERNO S A del 07/09/2026: 29715, 29716, 29717, 29718.
--
-- Qué pasó: a las 12:39:04 se emitió el RDA R 00004-00005221 con los cuatro concentradores
-- (OS 29711-29714). Ocho minutos después, 12:47:54, se volvieron a cargar los mismos cuatro
-- equipos y se registró a mano el RDA R 00001-00026468 (OS 29715-29718). Son los mismos
-- device_id (29271-29274): una sola devolución, cargada dos veces.
--
-- Se borran las cuatro OS de la segunda carga. Quedan las 29711-29714, que son las que tienen
-- el remito emitido electrónicamente y con PDF.
--
-- Lo que arrastra el DELETE por FK ON DELETE CASCADE:
--   * bejerman_ingreso_remitos  -> las 4 filas del RDA R 00001-00026468
--   * ingreso_open_conflicts    -> los 4 conflictos pendientes (29715->29711, etc.)
-- No hay accesorios, presupuestos, eventos, fotos, tests, handoffs, órdenes de entrega ni
-- jobs de stock colgando de estas OS (verificado en el dump del 08/09 01:30).
-- Los equipos NO se tocan: los comparten con las OS que quedan.
--
-- Lo que este script NO hace: el comprobante RDA R 00001-00026468 sigue existiendo en Bejerman.
-- Anularlo allá, si corresponde, es una decisión aparte.
--
-- Correr con:  psql -v ON_ERROR_STOP=1 -1 -f borrar_ingresos_duplicados_merno_20260908.sql
-- (el -1 envuelve todo en una transacción: si una guarda falla, no se borra nada)

\echo '=== ANTES ==='
SELECT t.id AS os, t.estado::text, t.device_id, d.numero_serie, r.remito_number, r.document_mode
  FROM ingresos t
  JOIN devices d ON d.id = t.device_id
  LEFT JOIN bejerman_ingreso_remitos r ON r.ingreso_id = t.id
 WHERE t.id BETWEEN 29711 AND 29718
 ORDER BY t.id;

-- Guardas: si la base se movió desde el análisis, esto aborta la transacción entera.
DO $guardas$
DECLARE
  a_borrar int[] := ARRAY[29715, 29716, 29717, 29718];
  se_quedan int[] := ARRAY[29711, 29712, 29713, 29714];
  n int;
BEGIN
  -- 1. Las cuatro duplicadas siguen siendo las que creemos: sin tocar, con el remito manual.
  SELECT count(DISTINCT t.id) INTO n
    FROM ingresos t
    JOIN bejerman_ingreso_remitos r ON r.ingreso_id = t.id
   WHERE t.id = ANY(a_borrar)
     AND t.estado::text = 'ingresado'
     AND t.device_id IN (29271, 29272, 29273, 29274)
     AND r.remito_number = 'RDA R 00001-00026468';
  IF n <> 4 THEN
    RAISE EXCEPTION 'Abortado: coinciden % de 4 OS duplicadas. Alguna cambio de estado o de remito desde el analisis.', n;
  END IF;

  -- 2. Las buenas siguen vivas: no se borra el duplicado si se perdió el original.
  SELECT count(DISTINCT t.id) INTO n
    FROM ingresos t
    JOIN bejerman_ingreso_remitos r ON r.ingreso_id = t.id
   WHERE t.id = ANY(se_quedan)
     AND r.remito_number = 'RDA R 00004-00005221';
  IF n <> 4 THEN
    RAISE EXCEPTION 'Abortado: no estan las 4 OS buenas con el RDA R 00004-00005221 (hay %).', n;
  END IF;

  -- 3. Nadie trabajó sobre las duplicadas. Si aparece algo, se mira a mano antes de borrar.
  SELECT
      (SELECT count(*) FROM ingreso_accesorios          WHERE ingreso_id = ANY(a_borrar))
    + (SELECT count(*) FROM quotes                      WHERE ingreso_id = ANY(a_borrar))
    + (SELECT count(*) FROM ingreso_events              WHERE ticket_id  = ANY(a_borrar))
    + (SELECT count(*) FROM ingreso_media               WHERE ingreso_id = ANY(a_borrar))
    + (SELECT count(*) FROM ingreso_tests               WHERE ingreso_id = ANY(a_borrar))
    + (SELECT count(*) FROM ingreso_alquiler_accesorios WHERE ingreso_id = ANY(a_borrar))
    + (SELECT count(*) FROM ingreso_baja_requests       WHERE ingreso_id = ANY(a_borrar))
    + (SELECT count(*) FROM ingreso_assignment_requests WHERE ingreso_id = ANY(a_borrar))
    + (SELECT count(*) FROM handoffs                    WHERE ingreso_id = ANY(a_borrar))
    + (SELECT count(*) FROM equipos_derivados           WHERE ingreso_id = ANY(a_borrar))
    + (SELECT count(*) FROM delivery_order_items        WHERE ingreso_id = ANY(a_borrar))
    + (SELECT count(*) FROM delivery_orders             WHERE ingreso_id = ANY(a_borrar))
    + (SELECT count(*) FROM bejerman_sync_jobs          WHERE ingreso_id = ANY(a_borrar))
    INTO n;
  IF n <> 0 THEN
    RAISE EXCEPTION 'Abortado: las OS duplicadas tienen % registros asociados. Revisar a mano.', n;
  END IF;
END
$guardas$;

-- El motivo, para que el Histórico de eliminados pueda explicar el agujero de numeración.
INSERT INTO audit.change_log (user_role, table_name, record_id, column_name, old_value, new_value, ingreso_id)
SELECT 'claude',
       'ingresos',
       t.id,
       'correccion_operativa',
       NULL,
       format(
         'Ingreso %s eliminado por carga duplicada. El 07/09/2026 12:39 se emitio el RDA R 00004-00005221 '
         || 'con los cuatro concentradores de MERNO S A (OS 29711-29714); a las 12:47 se volvieron a cargar '
         || 'los mismos equipos y se registro a mano el RDA R 00001-00026468 (OS 29715-29718). Es la misma '
         || 'devolucion cargada dos veces: mismo cliente, mismo dia y los mismos device_id (29271-29274). '
         || 'Esta OS (serie %s) no tenia accesorios, presupuesto, eventos, fotos ni movimientos de stock, y '
         || 'su conflicto de ingreso apuntaba a la OS %s, que es la que queda. Los equipos no se borraron '
         || 'porque son los de la OS que sobrevive. El comprobante RDA R 00001-00026468 sigue existiendo en '
         || 'Bejerman.',
         t.id, d.numero_serie, t.id - 4
       ),
       t.id
  FROM ingresos t
  JOIN devices d ON d.id = t.device_id
 WHERE t.id IN (29715, 29716, 29717, 29718);

DELETE FROM ingresos WHERE id IN (29715, 29716, 29717, 29718);

\echo '=== DESPUES: tienen que quedar solo las 4 buenas ==='
SELECT t.id AS os, t.estado::text, t.device_id, d.numero_serie, r.remito_number
  FROM ingresos t
  JOIN devices d ON d.id = t.device_id
  LEFT JOIN bejerman_ingreso_remitos r ON r.ingreso_id = t.id
 WHERE t.device_id IN (29271, 29272, 29273, 29274)
 ORDER BY t.id;

\echo '=== conflictos pendientes de esas OS (tiene que dar 0) ==='
SELECT count(*) AS conflictos FROM ingreso_open_conflicts
 WHERE ingreso_id BETWEEN 29711 AND 29718 OR conflicting_ingreso_id BETWEEN 29711 AND 29718;
