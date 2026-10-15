-- Revierte mg7359_unificar_ficha_duplicada_os29098_aplicar_20261014.sql.
--
-- Vuelve a crear la ficha 28924 con los valores que tenía en el dump de prod del 14/10/2026 01:30
-- (N/S vacío, "mg 7359", Respirador, ALCLA, alquilado, ubicación 10), le devuelve la OS 29098 y
-- devuelve a la OS 28141 el trabajo del 04/03/2026 (fecha de servicio, faja G0978/7, descripción y
-- el PDF del test) y el alquiler 26091 del 04/03/2026 que tenía cargado. El trigger
-- sync_device_snapshot recalcula la ficha 28924 desde la OS 29098; la 28141 queda con los mismos
-- valores que tenía antes de la corrida.
--
-- Uso:  psql ... -v ON_ERROR_STOP=1 -f mg7359_unificar_ficha_duplicada_os29098_revertir_20261014.sql

SET client_encoding = 'UTF8';

BEGIN;

SELECT set_config('app.user_id', '2', true);
SELECT set_config('app.user_role', 'jefe', true);

DO $$
DECLARE v integer;
BEGIN
  IF EXISTS (SELECT 1 FROM devices WHERE id = 28924) THEN
    RAISE EXCEPTION 'La ficha 28924 ya existe: no se revierte nada';
  END IF;

  SELECT COUNT(*) INTO v
    FROM ingresos
   WHERE id = 29098 AND device_id = 28141 AND fecha_servicio::date = DATE '2026-03-04'
     AND faja_garantia = 'G0978/7' AND descripcion_problema LIKE 'CAlibraci%Pulmotrend';
  IF v <> 1 THEN RAISE EXCEPTION 'La OS 29098 cambió después de la corrida: no se revierte nada'; END IF;

  SELECT COUNT(*) INTO v
    FROM ingresos
   WHERE id = 28141 AND device_id = 28141 AND fecha_servicio IS NULL AND faja_garantia IS NULL
     AND descripcion_problema IS NULL AND alquiler_remito = '25429' AND alquiler_fecha = DATE '2025-07-10';
  IF v <> 1 THEN RAISE EXCEPTION 'La OS 28141 cambió después de la corrida: no se revierte nada'; END IF;

  IF NOT EXISTS (SELECT 1 FROM ingreso_media WHERE id = 57 AND ingreso_id = 29098) THEN
    RAISE EXCEPTION 'El PDF del test (ingreso_media 57) ya no está en la OS 29098: no se revierte nada';
  END IF;
END $$;

\echo == OS 28141 recupera el trabajo del 04/03/2026 y el alquiler 26091 (esperado: UPDATE 1) ==
UPDATE ingresos t
   SET fecha_servicio = s.fecha_servicio,
       faja_garantia = s.faja_garantia,
       descripcion_problema = s.descripcion_problema,
       alquiler_remito = '26091',
       alquiler_fecha = DATE '2026-03-04'
  FROM ingresos s
 WHERE t.id = 28141 AND s.id = 29098;

\echo == OS 29098 sin ese trabajo (esperado: UPDATE 1) ==
UPDATE ingresos
   SET fecha_servicio = NULL,
       faja_garantia = NULL,
       descripcion_problema = NULL
 WHERE id = 29098;

\echo == PDF del test a la OS 28141 (esperado: UPDATE 1) ==
UPDATE ingreso_media SET ingreso_id = 28141 WHERE id = 57 AND ingreso_id = 29098;

\echo == Ficha 28924 como estaba (esperado: INSERT 0 1) ==
-- devices.id es GENERATED ALWAYS: sin OVERRIDING SYSTEM VALUE no deja reponer el id original.
INSERT INTO devices (id, customer_id, marca_id, model_id, numero_serie, alquilado, alquiler_a,
                     numero_interno, tipo_equipo, ubicacion_id, mg_estado)
OVERRIDING SYSTEM VALUE
VALUES (28924, 9, 687, 1493, '', TRUE, 'ALCLA S.A.C.I.F.I.A.', 'mg 7359', 'Respirador', 10, 'activo');

\echo == OS 29098 vuelve a la ficha 28924 (esperado: UPDATE 1) ==
UPDATE ingresos SET device_id = 28924 WHERE id = 29098 AND device_id = 28141;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM ingresos WHERE id = 29098 AND device_id = 28924 AND faja_garantia IS NULL) THEN
    RAISE EXCEPTION 'Verificación: la OS 29098 no volvió a la ficha 28924';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM ingresos WHERE id = 28141 AND faja_garantia = 'G0978/7' AND alquiler_remito = '26091') THEN
    RAISE EXCEPTION 'Verificación: la OS 28141 no recuperó sus valores';
  END IF;
END $$;

COMMIT;
