-- 13/10/2026: la impresión en lote de las órdenes de salida de TERAPIAS MEDICAS DOMICILIARIAS
-- (OS 29741 a 29756, liberadas a las 13:48 por Lucas Humerez) falló, y al quedar en
-- "liberado" ya no aparecían en "Listos para liberar" para imprimirlas todas juntas.
-- Se vuelven a "reparado" (el estado previo de las 16) y se borra el evento de liberación,
-- para que la próxima impresión lo registre de nuevo.
--
-- Lo que NO se toca, porque al volver a liberar se reutiliza:
--   * bejerman_sync_jobs stock_str_to_stc (1508-1523): ya están en succeeded y el reencolado
--     respeta ese estado, así que el traslado STR->STC no se repite en Bejerman.
--   * delivery_orders service_release: sin RSS emitido; ensure_service_release_order_for_ingreso
--     refresca la orden existente en vez de crear otra.
\set ON_ERROR_STOP on
BEGIN;

DO $$
DECLARE n integer;
BEGIN
  SELECT count(*) INTO n
    FROM ingresos
   WHERE id BETWEEN 29741 AND 29756
     AND estado = 'liberado'
     AND COALESCE(remito_salida, '') = ''
     AND fecha_entrega IS NULL;
  IF n <> 16 THEN
    RAISE EXCEPTION 'Se esperaban 16 OS liberadas sin remito, hay %', n;
  END IF;
END $$;

UPDATE ingresos
   SET estado = 'reparado'
 WHERE id BETWEEN 29741 AND 29756
   AND estado = 'liberado';

DELETE FROM ingreso_events
 WHERE ticket_id BETWEEN 29741 AND 29756
   AND a_estado = 'liberado'
   AND comentario = 'Orden de salida impresa'
   AND ts >= '2026-10-13 13:48:00-03'
   AND ts <  '2026-10-13 13:49:00-03';

SELECT id AS os, estado, resolucion FROM ingresos WHERE id BETWEEN 29741 AND 29756 ORDER BY id;
SELECT count(*) AS eventos_liberado_restantes
  FROM ingreso_events WHERE ticket_id BETWEEN 29741 AND 29756 AND a_estado = 'liberado';

COMMIT;
