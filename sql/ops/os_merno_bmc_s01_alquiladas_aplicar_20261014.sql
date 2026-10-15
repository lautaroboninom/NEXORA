-- Concentradores BMC S01 de alquiler (artículo 1115006) devueltos por MERNO S A el 07/09/2026.
-- Pedido del usuario 14/10/2026: "buscá el MG para este equipo y sincronizalo [...] hacé lo mismo
-- con todos estos así podemos indicar que estuvieron alquilados y no salta el problema para el
-- nuevo ingreso" (panel Bejerman: conflictos de ingreso de las OS 29716, 29717 y 29718).
--
-- Qué pasó (verificado en SBDASEP y en NEXORA):
--   * Salieron a MERNO con el RTA R 00004-00004842 (17/07/2026) y volvieron con el
--     RDA R 00001-00026468 (07/09/2026), que es el ingreso real: OS 29715 a 29718.
--   * El mismo ingreso se cargó dos veces ese día (12:39 y 12:47): las OS 29711 a 29714 son las
--     gemelas. Su remito de ingreso dice RDA R 00004-00005221, pero ese RDA es de COC (OS 29719 y
--     29720), no contiene estas partidas.
--   * Después cada equipo volvió a salir alquilado, con RTA emitidos fuera de NEXORA:
--       S12BEA05014  RTA R 00004-00005253  09/09/2026  EDU    ANDREOLI EDUARDO GABRIEL  (OS 29711 y 29715 ya alquiladas)
--       S12BEA05010  RTA R 00004-00005364  25/09/2026  MEDAUX SLOBINSKY PABLO EMILIO    (OS 29717 ya alquilada; falta la 29713)
--       S12BEA05013  RTA R 00004-00005385  01/10/2026  EDU    ANDREOLI EDUARDO GABRIEL  (OS 29712 y 29716 abiertas)
--       S12BEA05007  RTA R 00004-00005387  01/10/2026  SIED   SIED SERVICIO INTEGRAL DOMICILIARIO SRL (OS 29714 y 29718 abiertas)
--   * MG: ninguna de las cuatro series tiene número interno en NEXORA, ni en la observación de la
--     partida (StockPar.stp_Obs vacío en SAR/STL/STR), ni en MADRE 2026.xlsx. No se carga ningún MG.
--
-- Efecto: las 5 OS abiertas pasan a 'alquilado' con el RTA, la fecha y el cliente que figuran en
-- Bejerman, ubicación '-' y presupuesto 'no_aplica' (por `quotes`, para que el trigger
-- sync_quote_with_ingreso no lo pise), igual que quedaron a mano las OS 29711, 29715 y 29717. A la
-- 29715 se le completa el RTA que le faltaba. Los 4 conflictos de ingreso quedan resueltos con el
-- RTA como evidencia. El trigger sync_device_snapshot copia el alquiler a cada equipo. No dispara
-- movimientos en Bejerman.
--
-- Para deshacer: os_merno_bmc_s01_alquiladas_revertir_20261014.sql
--
-- Uso:  psql ... -v ON_ERROR_STOP=1 -f os_merno_bmc_s01_alquiladas_aplicar_20261014.sql

SET client_encoding = 'UTF8';

BEGIN;

SELECT set_config('app.user_id', '2', true);

CREATE TEMP TABLE alq (
  ingreso_id  integer PRIMARY KEY,
  serie       text NOT NULL,
  cliente_cod text NOT NULL,
  alquiler_a  text NOT NULL,
  rta         text NOT NULL,
  fecha       date NOT NULL,
  gemela_de   integer NULL
) ON COMMIT DROP;
INSERT INTO alq VALUES
  (29712, 'S12BEA05013', 'EDU',    'ANDREOLI EDUARDO GABRIEL',                'RTA R 00004-00005385', DATE '2026-10-01', 29716),
  (29716, 'S12BEA05013', 'EDU',    'ANDREOLI EDUARDO GABRIEL',                'RTA R 00004-00005385', DATE '2026-10-01', NULL),
  (29713, 'S12BEA05010', 'MEDAUX', 'SLOBINSKY PABLO EMILIO',                  'RTA R 00004-00005364', DATE '2026-09-25', 29717),
  (29714, 'S12BEA05007', 'SIED',   'SIED SERVICIO INTEGRAL DOMICILIARIO SRL', 'RTA R 00004-00005387', DATE '2026-10-01', 29718),
  (29718, 'S12BEA05007', 'SIED',   'SIED SERVICIO INTEGRAL DOMICILIARIO SRL', 'RTA R 00004-00005387', DATE '2026-10-01', NULL);

-- Conflicto -> OS nueva, OS gemela y RTA con el que salió el equipo.
CREATE TEMP TABLE conf (
  conflict_id integer PRIMARY KEY,
  ingreso_id  integer NOT NULL,
  gemela      integer NOT NULL,
  serie       text NOT NULL,
  cliente_cod text NOT NULL,
  alquiler_a  text NOT NULL,
  rta         text NOT NULL,
  fecha       date NOT NULL
) ON COMMIT DROP;
INSERT INTO conf VALUES
  (7,  29715, 29711, 'S12BEA05014', 'EDU',    'ANDREOLI EDUARDO GABRIEL',                'RTA R 00004-00005253', DATE '2026-09-09'),
  (8,  29716, 29712, 'S12BEA05013', 'EDU',    'ANDREOLI EDUARDO GABRIEL',                'RTA R 00004-00005385', DATE '2026-10-01'),
  (9,  29717, 29713, 'S12BEA05010', 'MEDAUX', 'SLOBINSKY PABLO EMILIO',                  'RTA R 00004-00005364', DATE '2026-09-25'),
  (10, 29718, 29714, 'S12BEA05007', 'SIED',   'SIED SERVICIO INTEGRAL DOMICILIARIO SRL', 'RTA R 00004-00005387', DATE '2026-10-01');

DO $$
DECLARE
  v integer;
BEGIN
  SELECT COUNT(*) INTO v
    FROM alq a
    JOIN ingresos t ON t.id = a.ingreso_id
    JOIN devices d ON d.id = t.device_id
   WHERE t.estado = 'ingresado'
     AND COALESCE(t.alquilado, FALSE) = FALSE
     AND COALESCE(t.alquiler_remito, '') = ''
     AND COALESCE(t.remito_salida, '') = ''
     AND UPPER(REPLACE(d.numero_serie, ' ', '')) = a.serie
     AND COALESCE(d.numero_interno, '') = ''
     AND NOT EXISTS (SELECT 1 FROM quotes q WHERE q.ingreso_id = t.id);
  IF v <> 5 THEN
    RAISE EXCEPTION 'Se esperaban 5 OS abiertas en condiciones y hay %: no se aplica nada', v;
  END IF;

  SELECT COUNT(*) INTO v
    FROM ingresos t
   WHERE t.id = 29715 AND t.estado = 'alquilado' AND COALESCE(t.alquiler_remito, '') = ''
     AND COALESCE(t.remito_salida, '') = '';
  IF v <> 1 THEN
    RAISE EXCEPTION 'La OS 29715 cambió desde el relevamiento: no se aplica nada';
  END IF;

  SELECT COUNT(*) INTO v
    FROM conf c
    JOIN ingreso_open_conflicts k ON k.id = c.conflict_id
   WHERE k.status = 'pending' AND k.ingreso_id = c.ingreso_id AND k.conflicting_ingreso_id = c.gemela;
  IF v <> 4 THEN
    RAISE EXCEPTION 'Se esperaban 4 conflictos pendientes y hay %: no se aplica nada', v;
  END IF;
END $$;

\echo == Historial de la OS (esperado: INSERT 0 5) ==
INSERT INTO ingreso_events (ticket_id, de_estado, a_estado, usuario_id, ts, comentario)
SELECT a.ingreso_id, 'ingresado', 'alquilado', 2,
       (a.fecha + TIME '12:00') AT TIME ZONE 'America/Argentina/Buenos_Aires',
       CASE
         WHEN a.gemela_de IS NULL THEN
           'Alquiler cargado a mano el 14/10/2026: el equipo salió con el ' || a.rta || ' del '
           || to_char(a.fecha, 'DD/MM/YYYY') || ' a ' || a.alquiler_a || ', emitido fuera de NEXORA.'
         ELSE
           'Cierre manual 14/10/2026: OS duplicada de la ' || a.gemela_de
           || ' (el mismo ingreso se cargó dos veces el 07/09/2026; el equipo volvió con el RDA R 00001-00026468). '
           || 'Salió alquilado con el ' || a.rta || ' del ' || to_char(a.fecha, 'DD/MM/YYYY') || ' a ' || a.alquiler_a || '.'
       END
  FROM alq a;

\echo == OS ingresado -> alquilado (esperado: UPDATE 5) ==
UPDATE ingresos t
   SET estado = 'alquilado',
       alquilado = TRUE,
       alquiler_a = a.alquiler_a,
       alquiler_remito = a.rta,
       alquiler_fecha = a.fecha,
       remito_salida = COALESCE(NULLIF(t.remito_salida, ''), a.rta),
       ubicacion_id = (SELECT id FROM locations WHERE nombre = '-' LIMIT 1)
  FROM alq a
 WHERE t.id = a.ingreso_id;

\echo == Presupuesto no_aplica por quotes (esperado: INSERT 0 5) ==
INSERT INTO quotes (ingreso_id, version_num, estado)
SELECT a.ingreso_id, 1, 'no_aplica' FROM alq a;

\echo == OS 29715: RTA que le faltaba (esperado: UPDATE 1) ==
UPDATE ingresos
   SET alquiler_remito = 'RTA R 00004-00005253',
       alquiler_fecha = COALESCE(alquiler_fecha, DATE '2026-09-09'),
       remito_salida = COALESCE(NULLIF(remito_salida, ''), 'RTA R 00004-00005253')
 WHERE id = 29715;

\echo == Conflictos de ingreso resueltos (esperado: UPDATE 4) ==
UPDATE ingreso_open_conflicts k
   SET status = 'resolved',
       resolution_note = 'La OS ' || c.gemela || ' era un duplicado de la ' || c.ingreso_id
                         || ' (el mismo ingreso se cargó dos veces el 07/09/2026). El equipo salió alquilado con el '
                         || c.rta || ' del ' || to_char(c.fecha, 'DD/MM/YYYY') || ' a ' || c.alquiler_a
                         || '; las dos OS quedan cerradas como alquiladas.',
       evidence = jsonb_build_object(
         'source', 'regularizacion_manual_20261014',
         'documents', jsonb_build_array(jsonb_build_object(
           'type', 'RTA', 'letter', 'R', 'pointOfSale', '00004',
           'number', split_part(c.rta, '-', 2),
           'remitoNumber', c.rta,
           'issueDate', to_char(c.fecha, 'YYYY-MM-DD'),
           'partida', c.serie,
           'customerCode', c.cliente_cod,
           'customerName', c.alquiler_a
         )),
         'returnDocument', 'RDA R 00001-00026468'
       ),
       resolved_by = 2,
       resolved_at = CURRENT_TIMESTAMP,
       updated_at = CURRENT_TIMESTAMP
  FROM conf c
 WHERE k.id = c.conflict_id
   AND k.status = 'pending';

\echo == Control: OS de los cuatro equipos ==
SELECT t.id, d.numero_serie, t.estado::text, t.alquilado, t.alquiler_a, t.alquiler_remito, t.alquiler_fecha,
       t.remito_salida, t.ubicacion_id, t.presupuesto_estado::text
  FROM ingresos t JOIN devices d ON d.id = t.device_id
 WHERE t.id BETWEEN 29711 AND 29718
 ORDER BY d.numero_serie, t.id;

\echo == Control: equipos (esperado: los 4 alquilados) ==
SELECT d.id, d.numero_serie, d.alquilado, d.alquiler_a, d.ubicacion_id
  FROM devices d WHERE d.id IN (29271, 29272, 29273, 29274) ORDER BY d.id;

\echo == Control: conflictos pendientes de estas OS (esperado: 0) ==
SELECT COUNT(*) AS pendientes FROM ingreso_open_conflicts WHERE id IN (7, 8, 9, 10) AND status = 'pending';

COMMIT;
