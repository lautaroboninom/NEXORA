-- MG 7359 (Respirador Magnamed Fleximag MAX 300, N/S 3676, ALCLA): ficha duplicada y trabajo
-- cargado en la OS equivocada. Pedido del usuario 14/10/2026: "hacé la unificación, corregí esta
-- cagada que hicieron".
--
-- Qué pasó (verificado en el dump de prod del 14/10 01:30 y en SBDASEP):
--   * El 02/03/2026 15:51 la cuenta Gracia (id 7) cargó la OS 29098 desde Nuevo ingreso con
--     numero_interno "mg 7359" (en minúscula) y sin N/S. Ya existía la ficha 28141 con "MG 7359" y
--     N/S 3676 (desde el 31/10/2025). La búsqueda por MG de esa versión comparaba con
--     UPPER(REGEXP_REPLACE(x, '^(MG|...)', '\1 ' || LPAD('\2',4,'0'))): la regex distingue
--     mayúsculas y corre antes del UPPER, así que "MG 7359" daba "MG 007359" y "mg 7359" daba
--     "MG 7359". No coincidieron y se creó la ficha 28924. El índice único uq_devices_numint_norm
--     usa la misma expresión, por eso tampoco frenó el INSERT.
--   * El 04/03/2026 el trabajo de esa visita se cargó en la OS 28141 (baja alquiler del 07/07/2025,
--     ya realquilada con el RTN 25429 del 10/07/2025): fecha de servicio 04/03/2026, faja G0978/7,
--     descripción "Calibración sensores con soft ARM / Test de funcionamiento con Pulmotrend" y el
--     PDF del test (ingreso_media 57, "Test-Fleximag-MAX-300-NS-3676-MG7359-4-3-26.pdf"). Antes
--     del 04/03 esos tres campos estaban vacíos en la 28141 (audit.change_log).
--   * El 22/04/2026 se le cargó a la OS 28141 el alquiler de la 29098 (remito 26091, 04/03/2026).
--     En Bejerman la partida 3676 salió a ALCLA con el RTN R 00001-00025429 del 10/07/2025
--     después de esa baja, y con el RTN R 00001-00026091 del 04/03/2026 después de la 29098.
--
-- Efecto:
--   1) El trabajo del 04/03/2026 (fecha de servicio, faja, descripción y PDF del test) pasa de la
--      OS 28141 a la 29098. La OS 28141 recupera su alquiler real: remito 25429 del 10/07/2025.
--   2) La OS 29098 pasa a la ficha 28141 (N/S 3676, MG 7359) y se borra la ficha 28924, que no
--      tiene nada más colgado (ni órdenes de entrega, ni jobs de Bejerman, ni preventivos).
--      El plan preventivo 5 de la ficha 28141 queda intacto.
--   El trigger sync_device_snapshot deja la ficha con el alquiler, la ubicación y la faja de su
--   última OS (la 29098). No toca Bejerman ni el presupuesto (el de la 29098 pasó a "no aplica"
--   el 14/10 a las 12:07, desde el usuario 2, antes de esta corrida).
--
-- Para deshacer: mg7359_unificar_ficha_duplicada_os29098_revertir_20261014.sql
--
-- Uso:  psql ... -v ON_ERROR_STOP=1 -f mg7359_unificar_ficha_duplicada_os29098_aplicar_20261014.sql

SET client_encoding = 'UTF8';

BEGIN;

SELECT set_config('app.user_id', '2', true);
SELECT set_config('app.user_role', 'jefe', true);

DO $$
DECLARE v integer;
BEGIN
  SELECT COUNT(*) INTO v
    FROM devices
   WHERE id = 28141 AND numero_serie = '3676' AND numero_interno = 'MG 7359'
     AND tipo_equipo = 'Respirador' AND model_id = 1493 AND customer_id = 9;
  IF v <> 1 THEN RAISE EXCEPTION 'La ficha 28141 no está como se midió: no se aplica nada'; END IF;

  SELECT COUNT(*) INTO v
    FROM devices
   WHERE id = 28924 AND COALESCE(numero_serie, '') = '' AND numero_interno = 'mg 7359'
     AND tipo_equipo = 'Respirador' AND model_id = 1493 AND customer_id = 9;
  IF v <> 1 THEN RAISE EXCEPTION 'La ficha 28924 no está como se midió: no se aplica nada'; END IF;

  SELECT COUNT(*) INTO v
    FROM devices
   WHERE id NOT IN (28141, 28924)
     AND (REPLACE(REPLACE(UPPER(COALESCE(numero_serie, '')), ' ', ''), '-', '') = '3676'
          OR REGEXP_REPLACE(UPPER(BTRIM(COALESCE(numero_interno, ''))),
                            '^(MG|NM|NV|CE)\s*0*(\d{1,6})$', '\1 \2') = 'MG 7359');
  IF v <> 0 THEN RAISE EXCEPTION 'Hay otra ficha con N/S 3676 o MG 7359: no se aplica nada'; END IF;

  SELECT COUNT(*) INTO v FROM ingresos WHERE device_id = 28924;
  IF v <> 1 THEN RAISE EXCEPTION 'La ficha 28924 tiene % OS (se esperaba sólo la 29098): no se aplica nada', v; END IF;

  SELECT COUNT(*) INTO v
    FROM ingresos
   WHERE id = 29098 AND device_id = 28924 AND estado = 'alquilado'
     AND fecha_servicio IS NULL
     AND COALESCE(faja_garantia, '') = ''
     AND COALESCE(descripcion_problema, '') = '';
  IF v <> 1 THEN RAISE EXCEPTION 'La OS 29098 cambió desde el relevamiento: no se aplica nada'; END IF;

  SELECT COUNT(*) INTO v
    FROM ingresos
   WHERE id = 28141 AND device_id = 28141 AND estado = 'alquilado'
     AND fecha_servicio::date = DATE '2026-03-04'
     AND faja_garantia = 'G0978/7'
     AND descripcion_problema LIKE 'CAlibraci%Pulmotrend'
     AND alquiler_remito = '26091'
     AND alquiler_fecha = DATE '2026-03-04'
     AND fecha_entrega::date = DATE '2025-07-10';
  IF v <> 1 THEN RAISE EXCEPTION 'La OS 28141 cambió desde el relevamiento: no se aplica nada'; END IF;

  SELECT COUNT(*) INTO v
    FROM ingreso_media
   WHERE id = 57 AND ingreso_id = 28141
     AND original_name = 'Test-Fleximag-MAX-300-NS-3676-MG7359-4-3-26.pdf';
  IF v <> 1 THEN RAISE EXCEPTION 'El PDF del test (ingreso_media 57) no está en la OS 28141: no se aplica nada'; END IF;

  SELECT (SELECT COUNT(*) FROM bejerman_sync_jobs WHERE device_id = 28924)
       + (SELECT COUNT(*) FROM device_mg_events WHERE device_id = 28924)
       + (SELECT COUNT(*) FROM delivery_orders WHERE device_id = 28924)
       + (SELECT COUNT(*) FROM delivery_order_items WHERE device_id = 28924)
       + (SELECT COUNT(*) FROM delivery_order_item_partidas WHERE device_id = 28924)
       + (SELECT COUNT(*) FROM preventivo_planes WHERE device_id = 28924)
       + (SELECT COUNT(*) FROM preventivo_revision_items WHERE device_id = 28924)
       + (SELECT COUNT(*) FROM ingresos WHERE device_cambio_id = 28924)
    INTO v;
  IF v <> 0 THEN RAISE EXCEPTION 'La ficha 28924 tiene % referencias además de la OS: no se aplica nada', v; END IF;
END $$;

\echo == 1a) Trabajo del 04/03/2026 a la OS 29098 (esperado: UPDATE 1) ==
UPDATE ingresos t
   SET fecha_servicio = s.fecha_servicio,
       faja_garantia = s.faja_garantia,
       descripcion_problema = s.descripcion_problema
  FROM ingresos s
 WHERE t.id = 29098 AND s.id = 28141;

\echo == 1b) OS 28141 sin ese trabajo y con su alquiler real (esperado: UPDATE 1) ==
UPDATE ingresos
   SET fecha_servicio = NULL,
       faja_garantia = NULL,
       descripcion_problema = NULL,
       alquiler_remito = '25429',
       alquiler_fecha = DATE '2025-07-10'
 WHERE id = 28141;

\echo == 1c) PDF del test a la OS 29098 (esperado: UPDATE 1) ==
UPDATE ingreso_media SET ingreso_id = 29098 WHERE id = 57 AND ingreso_id = 28141;

\echo == 2a) OS 29098 a la ficha 28141 (esperado: UPDATE 1) ==
UPDATE ingresos SET device_id = 28141 WHERE id = 29098 AND device_id = 28924;

\echo == 2b) Ficha duplicada 28924 (esperado: DELETE 1) ==
DELETE FROM devices WHERE id = 28924;

DO $$
DECLARE v integer;
BEGIN
  SELECT COUNT(*) INTO v
    FROM ingresos
   WHERE id = 29098 AND device_id = 28141 AND fecha_servicio::date = DATE '2026-03-04'
     AND faja_garantia = 'G0978/7' AND descripcion_problema LIKE 'CAlibraci%Pulmotrend';
  IF v <> 1 THEN RAISE EXCEPTION 'Verificación: la OS 29098 no quedó como se esperaba'; END IF;

  SELECT COUNT(*) INTO v
    FROM ingresos
   WHERE id = 28141 AND device_id = 28141 AND fecha_servicio IS NULL AND faja_garantia IS NULL
     AND descripcion_problema IS NULL AND alquiler_remito = '25429' AND alquiler_fecha = DATE '2025-07-10';
  IF v <> 1 THEN RAISE EXCEPTION 'Verificación: la OS 28141 no quedó como se esperaba'; END IF;

  SELECT COUNT(*) INTO v
    FROM devices
   WHERE id = 28141 AND numero_serie = '3676' AND numero_interno = 'MG 7359'
     AND alquilado AND alquiler_a = 'ALCLA S.A.C.I.F.I.A.' AND n_de_control = 'G0978/7';
  IF v <> 1 THEN RAISE EXCEPTION 'Verificación: la ficha 28141 no quedó como se esperaba'; END IF;

  IF EXISTS (SELECT 1 FROM devices WHERE id = 28924) THEN
    RAISE EXCEPTION 'Verificación: la ficha 28924 sigue existiendo';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM ingreso_media WHERE id = 57 AND ingreso_id = 29098) THEN
    RAISE EXCEPTION 'Verificación: el PDF del test no quedó en la OS 29098';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM preventivo_planes WHERE id = 5 AND device_id = 28141) THEN
    RAISE EXCEPTION 'Verificación: el plan preventivo 5 no quedó en la ficha 28141';
  END IF;
END $$;

\echo == Resultado ==
SELECT t.id AS os, t.device_id, d.numero_serie AS ns, d.numero_interno AS mg, t.estado,
       t.fecha_ingreso::date, t.fecha_servicio::date, t.faja_garantia, t.alquiler_remito, t.alquiler_fecha
  FROM ingresos t JOIN devices d ON d.id = t.device_id
 WHERE t.id IN (28141, 29098)
 ORDER BY t.id;

COMMIT;
