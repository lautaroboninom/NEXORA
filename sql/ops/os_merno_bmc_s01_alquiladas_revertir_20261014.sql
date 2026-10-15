-- Revierte os_merno_bmc_s01_alquiladas_aplicar_20261014.sql.
--
-- Devuelve las OS 29712, 29713, 29714, 29716 y 29718 a 'ingresado' en el Taller, sin datos de
-- alquiler ni presupuesto (borra la quote 'no_aplica' que creó la corrida; antes no tenían
-- ninguna), borra los 5 eventos agregados, le quita a la 29715 el RTA completado y deja los
-- conflictos 7 a 10 otra vez pendientes. El trigger sync_device_snapshot devuelve cada equipo a lo
-- que diga su última OS. El borrado de las quotes reescribe presupuesto_estado por el trigger de
-- quotes; por eso al final se fuerza 'pendiente', que era el valor anterior.
--
-- Uso:  psql ... -v ON_ERROR_STOP=1 -f os_merno_bmc_s01_alquiladas_revertir_20261014.sql

SET client_encoding = 'UTF8';

BEGIN;

SELECT set_config('app.user_id', '2', true);

DO $$
DECLARE v integer;
BEGIN
  SELECT COUNT(*) INTO v
    FROM ingresos
   WHERE id IN (29712, 29713, 29714, 29716, 29718)
     AND estado = 'alquilado'
     AND alquiler_remito IN ('RTA R 00004-00005385', 'RTA R 00004-00005364', 'RTA R 00004-00005387');
  IF v <> 5 THEN RAISE EXCEPTION 'Se esperaban 5 OS alquiladas por la corrida y hay %: no se revierte nada', v; END IF;
  SELECT COUNT(*) INTO v FROM quotes WHERE ingreso_id IN (29712, 29713, 29714, 29716, 29718);
  IF v <> 5 THEN RAISE EXCEPTION 'Las quotes de esas OS cambiaron (% filas): no se revierte nada', v; END IF;
END $$;

\echo == Eventos de la corrida (esperado: DELETE 5) ==
DELETE FROM ingreso_events
 WHERE ticket_id IN (29712, 29713, 29714, 29716, 29718)
   AND a_estado = 'alquilado'
   AND (comentario LIKE 'Alquiler cargado a mano el 14/10/2026:%' OR comentario LIKE 'Cierre manual 14/10/2026:%');

\echo == Quotes no_aplica creadas por la corrida (esperado: DELETE 5) ==
DELETE FROM quotes
 WHERE ingreso_id IN (29712, 29713, 29714, 29716, 29718)
   AND version_num = 1
   AND estado = 'no_aplica';

\echo == OS alquilado -> ingresado (esperado: UPDATE 5) ==
UPDATE ingresos
   SET estado = 'ingresado',
       alquilado = NULL,
       alquiler_a = NULL,
       alquiler_remito = NULL,
       alquiler_fecha = NULL,
       remito_salida = NULL,
       ubicacion_id = 1,
       presupuesto_estado = 'pendiente'
 WHERE id IN (29712, 29713, 29714, 29716, 29718);

\echo == OS 29715 sin el RTA completado (esperado: UPDATE 1) ==
UPDATE ingresos
   SET alquiler_remito = NULL,
       alquiler_fecha = NULL,
       remito_salida = NULL
 WHERE id = 29715
   AND alquiler_remito = 'RTA R 00004-00005253';

\echo == Conflictos otra vez pendientes (esperado: UPDATE 4) ==
UPDATE ingreso_open_conflicts
   SET status = 'pending',
       resolution_note = NULL,
       evidence = '{}'::jsonb,
       resolved_by = NULL,
       resolved_at = NULL,
       updated_at = CURRENT_TIMESTAMP
 WHERE id IN (7, 8, 9, 10)
   AND evidence->>'source' = 'regularizacion_manual_20261014';

SELECT t.id, t.estado::text, t.alquilado, t.alquiler_remito, t.ubicacion_id, t.presupuesto_estado::text
  FROM ingresos t WHERE t.id BETWEEN 29711 AND 29718 ORDER BY t.id;

COMMIT;
