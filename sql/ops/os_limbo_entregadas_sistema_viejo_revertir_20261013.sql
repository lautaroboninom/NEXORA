-- Revierte os_limbo_entregadas_sistema_viejo_aplicar_20261013.sql (corrida de las 13:15:54 del
-- 13/10/2026). Corrección del usuario: para un equipo propio, "entregado" en el sistema viejo
-- ES "alquilado" (para alquilarlo hay que entregarlo), así que esas 190 OS ya estaban bien.
--
-- Restaura en ingresos los valores anteriores registrados en audit.change_log (estado, alquilado,
-- alquiler_a, alquiler_fecha, alquiler_remito); ubicación y fecha de entrega no habían cambiado. El
-- trigger sync_device_snapshot devuelve devices.alquilado y devices.alquiler_a. Borra los 190
-- eventos "Regularizacion 13/10/2026: entregado segun el sistema viejo".
--
-- Uso:  psql ... -v ON_ERROR_STOP=1 -f os_limbo_entregadas_sistema_viejo_revertir_20261013.sql

BEGIN;

CREATE TEMP TABLE antes ON COMMIT DROP AS
SELECT record_id AS ingreso_id, column_name, old_value
  FROM audit.change_log
 WHERE table_name = 'ingresos'
   AND ts = '2026-10-13 13:15:54.359537-03'
   AND user_id IS NULL
   AND record_id IN (15210,15432,15536,15716,15982,16171,16175,16208,16459,16470,16486,16501,16567,16574,16779,16909,16910,17045,17050,17177,17423,17430,17530,17585,17631,17744,17782,17820,17857,17875,17928,18232,18266,18816,19245,19256,19257,19282,19303,19339,19385,19420,19497,19539,19540,19549,19577,19618,19620,19705,19712,19722,19747,19750,19816,19938,20002,20034,20037,20049,20071,20090,20144,20160,20170,20254,20294,20295,20371,20393,20420,20446,20447,20463,20506,20517,20584,20619,20629,20654,20675,20700,20726,20778,20785,20806,20808,20809,20831,20832,20833,20834,20840,20843,20845,20846,20848,20854,20855,20911,20947,20951,20982,20983,21003,21007,21008,21009,21036,21048,21050,21067,21088,21090,21093,21094,21098,21128,21133,21149,21150,21152,21162,21187,21192,21219,21221,21223,21224,21225,21230,21233,21262,21276,21304,21306,21307,21311,21312,21313,21314,21346,21347,21361,21478,21484,21485,21486,21487,21580,21911,21920,22035,22113,22144,22183,22240,22399,22418,22419,22706,22707,22708,22744,22832,22873,22978,23011,23058,23070,23071,23095,23165,23194,23247,23302,23372,23677,23876,24350,24352,24355,24458,24555,24812,25057,25118,25121,26296,26393);

DO $$
DECLARE v integer;
BEGIN
  SELECT COUNT(*) INTO v FROM antes WHERE column_name = 'estado' AND old_value = 'alquilado';
  IF v <> 190 THEN RAISE EXCEPTION 'Se esperaban 190 estados anteriores y hay %', v; END IF;
  SELECT COUNT(*) INTO v FROM ingresos WHERE id IN (SELECT ingreso_id FROM antes) AND estado <> 'entregado';
  IF v <> 0 THEN RAISE EXCEPTION '% OS cambiaron desde la corrida: no se revierte nada', v; END IF;
END $$;

\echo == Estado y alquiler anteriores (esperado: UPDATE 190) ==
UPDATE ingresos t
   SET estado = 'alquilado',
       alquilado = (SELECT a.old_value::boolean FROM antes a WHERE a.ingreso_id = t.id AND a.column_name = 'alquilado'),
       alquiler_a = CASE WHEN EXISTS (SELECT 1 FROM antes a WHERE a.ingreso_id = t.id AND a.column_name = 'alquiler_a')
                         THEN (SELECT a.old_value FROM antes a WHERE a.ingreso_id = t.id AND a.column_name = 'alquiler_a')
                         ELSE t.alquiler_a END,
       alquiler_fecha = CASE WHEN EXISTS (SELECT 1 FROM antes a WHERE a.ingreso_id = t.id AND a.column_name = 'alquiler_fecha')
                             THEN (SELECT a.old_value::date FROM antes a WHERE a.ingreso_id = t.id AND a.column_name = 'alquiler_fecha')
                             ELSE t.alquiler_fecha END,
       alquiler_remito = CASE WHEN EXISTS (SELECT 1 FROM antes a WHERE a.ingreso_id = t.id AND a.column_name = 'alquiler_remito')
                              THEN (SELECT a.old_value FROM antes a WHERE a.ingreso_id = t.id AND a.column_name = 'alquiler_remito')
                              ELSE t.alquiler_remito END
 WHERE t.id IN (SELECT ingreso_id FROM antes WHERE column_name = 'estado');

\echo == Eventos agregados por la corrida (esperado: DELETE 190) ==
DELETE FROM ingreso_events
 WHERE ticket_id IN (SELECT ingreso_id FROM antes WHERE column_name = 'estado')
   AND comentario = 'Regularizacion 13/10/2026: entregado segun el sistema viejo (sin rastro en Bejerman)';

\echo == Control contra la auditoría de devices (esperado: 0 diferencias) ==
SELECT COUNT(*) AS diferencias
  FROM audit.change_log c
  JOIN devices d ON d.id = c.record_id
 WHERE c.table_name = 'devices'
   AND c.ts = '2026-10-13 13:15:54.359537-03'
   AND c.user_id IS NULL
   AND ((c.column_name = 'alquilado' AND d.alquilado::text <> c.old_value)
     OR (c.column_name = 'alquiler_a' AND COALESCE(d.alquiler_a, '') <> COALESCE(c.old_value, '')));

SELECT t.estado::text, t.alquilado, COUNT(*) FROM ingresos t WHERE t.id IN (SELECT ingreso_id FROM antes WHERE column_name='estado') GROUP BY 1, 2;

COMMIT;
