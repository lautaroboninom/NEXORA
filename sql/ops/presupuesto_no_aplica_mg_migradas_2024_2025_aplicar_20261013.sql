-- Segundo lote de la limpieza de "Pendientes de Presupuesto" (pedido del usuario 13/10/2026:
-- "sigamos resolviendo ordenes de servicio de este tipo, equipos propios que no tienen el No
-- aplica tildado"). El primer lote, OS 2017-2023, es presupuesto_no_aplica_os_hasta_2023_aplicar_20261013.sql.
--
-- Alcance: OS de equipos propios (codigo MG/NM/NV/CE) que estan en la cola, con ingreso desde
-- 2024 y MIGRADAS del sistema viejo (fecha_creacion 18/09/2025, antes de que NEXORA tomara las
-- altas). Medido en prod el 13/10/2026: 297 OS (294 alquilado/otros, 2 alquilado/baja alquiler,
-- 1 diagnosticado/baja alquiler). Las OS creadas en NEXORA (desde diciembre de 2025) son la cola
-- viva de Comercial y no se tocan.
--
-- Mismo criterio de la cola que _SQL_PENDIENTES_PRESUPUESTO (colas_views.py) y
-- PendientesPresupuestoView (ingresos_views.py). Informe por OS en
-- docs/reportes/os_mg_migradas_2024_2025_presupuesto_no_aplica_20261013.csv.
--
-- VENTAS (11 OS, la ultima OS de cada equipo; rastro en Bejerman SBDASEP):
--   NOVAMED S.A. (customer 12), 08/06/2026, aporte sin factura (RTN no facturable, mismo bloque
--     del primer lote): RTN R 00004-00004509 (despues del RDA 00004-00004482) para las OS 26322,
--     26559, 26560, 26724, 26747 y 26749; RTN R 00004-00004510 (despues del RDA 00004-00004483)
--     para las OS 25879, 26476, 26696 y 26867.
--   ARGENTINA DE GASES S.A. (customer 17): OS 26045 (MG 6926), RT R 00001-00025729 del
--     05/11/2025 y FC A 00002-00008440 del 14/11/2025.
--   La OS 25888 (MG 7229) NO se toma como venta: el RTN 00004-00001309 del 17/01/2024 es a MG BIO
--   S.A., un pase entre empresas.
--
-- PRESUPUESTO NO APLICA para el resto (esperado 285; la OS 25748 sale sola de la cola por la venta
-- de su equipo, MG 6926). Una version pendiente con importe nunca emitida: OS 25716 (MG 2910,
-- $130.000, borrador de 02/2024; el equipo reingreso como OS 29830): pasa a no_aplica y el importe
-- queda como esta. Se escribe `quotes`, no `ingresos.presupuesto_estado` (ver
-- marcar_quote_no_aplica() en quotes_views.py y el trigger sync_quote_with_ingreso).
--
-- Para deshacer: audit.change_log con la hora de esta corrida; device_mg_events con usuario_id
-- NULL y observaciones que empiezan con "Regularizacion 13/10/2026".
--
-- Uso:  psql ... -v ON_ERROR_STOP=1 -f presupuesto_no_aplica_mg_migradas_2024_2025_aplicar_20261013.sql

BEGIN;

-- ============================================================================
-- 1) Ventas
-- ============================================================================
CREATE TEMP TABLE venta (
  ingreso_id integer PRIMARY KEY,
  numero_interno text NOT NULL,
  customer_id integer NOT NULL,
  fecha_venta timestamptz NOT NULL,
  factura text NOT NULL,
  remito text NOT NULL,
  observaciones text NOT NULL
) ON COMMIT DROP;

INSERT INTO venta VALUES
  (26322, 'MG 7309', 12, '2026-06-08 00:00-03', '', 'RTN R 00004-00004509',
   'Regularizacion 13/10/2026 desde Bejerman: aporte a NOVAMED con RTN R 00004-00004509 (no facturable), despues del RDA R 00004-00004482 del 04/06/2026.'),
  (26559, 'MG 2094', 12, '2026-06-08 00:00-03', '', 'RTN R 00004-00004509',
   'Regularizacion 13/10/2026 desde Bejerman: aporte a NOVAMED con RTN R 00004-00004509 (no facturable), despues del RDA R 00004-00004482 del 04/06/2026.'),
  (26560, 'MG 0355', 12, '2026-06-08 00:00-03', '', 'RTN R 00004-00004509',
   'Regularizacion 13/10/2026 desde Bejerman: aporte a NOVAMED con RTN R 00004-00004509 (no facturable), despues del RDA R 00004-00004482 del 04/06/2026.'),
  (26724, 'MG 7299', 12, '2026-06-08 00:00-03', '', 'RTN R 00004-00004509',
   'Regularizacion 13/10/2026 desde Bejerman: aporte a NOVAMED con RTN R 00004-00004509 (no facturable), despues del RDA R 00004-00004482 del 04/06/2026.'),
  (26747, 'MG 6567', 12, '2026-06-08 00:00-03', '', 'RTN R 00004-00004509',
   'Regularizacion 13/10/2026 desde Bejerman: aporte a NOVAMED con RTN R 00004-00004509 (no facturable), despues del RDA R 00004-00004482 del 04/06/2026.'),
  (26749, 'MG1295', 12, '2026-06-08 00:00-03', '', 'RTN R 00004-00004509',
   'Regularizacion 13/10/2026 desde Bejerman: aporte a NOVAMED con RTN R 00004-00004509 (no facturable), despues del RDA R 00004-00004482 del 04/06/2026.'),
  (25879, 'MG 7241', 12, '2026-06-08 00:00-03', '', 'RTN R 00004-00004510',
   'Regularizacion 13/10/2026 desde Bejerman: aporte a NOVAMED con RTN R 00004-00004510 (no facturable), despues del RDA R 00004-00004483 del 04/06/2026.'),
  (26476, 'MG 1516', 12, '2026-06-08 00:00-03', '', 'RTN R 00004-00004510',
   'Regularizacion 13/10/2026 desde Bejerman: aporte a NOVAMED con RTN R 00004-00004510 (no facturable), despues del RDA R 00004-00004483 del 04/06/2026.'),
  (26696, 'NV 0223', 12, '2026-06-08 00:00-03', '', 'RTN R 00004-00004510',
   'Regularizacion 13/10/2026 desde Bejerman: aporte a NOVAMED con RTN R 00004-00004510 (no facturable), despues del RDA R 00004-00004483 del 04/06/2026.'),
  (26867, 'MG 0580', 12, '2026-06-08 00:00-03', '', 'RTN R 00004-00004510',
   'Regularizacion 13/10/2026 desde Bejerman: aporte a NOVAMED con RTN R 00004-00004510 (no facturable), despues del RDA R 00004-00004483 del 04/06/2026.'),
  (26045, 'MG 6926', 17, '2025-11-05 00:00-03', 'FC A 00002-00008440', 'RT R 00001-00025729',
   'Regularizacion 13/10/2026 desde Bejerman: venta a ARGENTINA DE GASES con RT R 00001-00025729 del 05/11/2025 y FC A 00002-00008440 del 14/11/2025.');

-- Guarda: cada OS tiene que ser la ultima de su equipo, seguir en 'alquilado' con el
-- presupuesto pendiente, y el equipo ser un MG activo con ese numero interno.
DO $$
DECLARE
  v_ok integer;
BEGIN
  SELECT COUNT(*) INTO v_ok
    FROM venta v
    JOIN ingresos t ON t.id = v.ingreso_id
    JOIN devices d ON d.id = t.device_id
   WHERE t.estado = 'alquilado'
     AND COALESCE(t.presupuesto_estado, 'pendiente') = 'pendiente'
     AND COALESCE(d.mg_estado, 'activo') = 'activo'
     AND UPPER(REPLACE(d.numero_interno, ' ', '')) = UPPER(REPLACE(v.numero_interno, ' ', ''))
     AND NOT EXISTS (SELECT 1 FROM ingresos t2 WHERE t2.device_id = t.device_id AND t2.id > t.id)
     AND EXISTS (SELECT 1 FROM customers c WHERE c.id = v.customer_id);
  IF v_ok <> 11 THEN
    RAISE EXCEPTION 'Ventas: se esperaban 11 OS en condiciones y hay %: no se aplica nada', v_ok;
  END IF;
END $$;

\echo == 1a) Equipo vendido (esperado: UPDATE 11) ==
UPDATE devices d
   SET mg_estado = 'inactivo_venta',
       mg_inactivo_desde = v.fecha_venta,
       mg_venta_fecha = v.fecha_venta,
       mg_venta_factura_numero = NULLIF(v.factura, ''),
       mg_venta_remito_numero = NULLIF(v.remito, ''),
       mg_venta_observaciones = v.observaciones,
       mg_venta_usuario_id = NULL,
       mg_venta_customer_id = v.customer_id,
       alquilado = FALSE,
       alquiler_a = NULL,
       ubicacion_id = (SELECT id FROM locations WHERE nombre = '-' LIMIT 1)
  FROM venta v
  JOIN ingresos t ON t.id = v.ingreso_id
 WHERE d.id = t.device_id;

\echo == 1b) Evento de venta del MG (esperado: INSERT 0 11) ==
INSERT INTO device_mg_events(
  device_id, accion, numero_interno_snapshot, fecha_evento,
  factura_numero, remito_numero, observaciones, usuario_id, ingreso_id,
  venta_customer_id, source
)
SELECT t.device_id, 'venta', v.numero_interno, v.fecha_venta,
       NULLIF(v.factura, ''), NULLIF(v.remito, ''), v.observaciones, NULL, v.ingreso_id,
       v.customer_id, 'equipos'
  FROM venta v
  JOIN ingresos t ON t.id = v.ingreso_id;

\echo == 1c) OS: alquilado -> vendido_entregado, como aplicar_venta_mg_a_ingreso() (esperado: UPDATE 11) ==
UPDATE ingresos t
   SET estado = 'vendido_entregado',
       factura_numero = COALESCE(NULLIF(v.factura, ''), t.factura_numero),
       fecha_entrega = COALESCE(t.fecha_entrega, v.fecha_venta),
       remito_salida = COALESCE(NULLIF(t.remito_salida, ''), NULLIF(v.remito, '')),
       alquilado = FALSE,
       alquiler_a = NULL,
       alquiler_remito = NULL,
       alquiler_fecha = NULL,
       ubicacion_id = (SELECT id FROM locations WHERE nombre = '-' LIMIT 1)
  FROM venta v
 WHERE t.id = v.ingreso_id;

\echo == 1d) Historial de la OS (esperado: INSERT 0 11) ==
INSERT INTO ingreso_events (ticket_id, de_estado, a_estado, usuario_id, ts, comentario)
SELECT v.ingreso_id, 'alquilado', 'vendido_entregado', NULL, v.fecha_venta,
       'Venta registrada y entrega confirmada'
  FROM venta v;

-- ============================================================================
-- 2) Presupuesto "no aplica" para el resto
-- ============================================================================
CREATE TEMP TABLE objetivo ON COMMIT DROP AS
SELECT t.id
  FROM ingresos t
  JOIN devices d ON d.id = t.device_id
 WHERE COALESCE(t.presupuesto_estado, 'pendiente') = 'pendiente'
   AND t.estado NOT IN ('ingresado','entregado','liberado','no_se_repara',
                        'vendido_pendiente_entrega','vendido_entregado')
   AND (
     t.estado <> 'alquilado'
     OR (
       BTRIM(COALESCE(d.numero_interno,'')) ~* '^(MG|NM|NV|CE)\s*0*(\d{1,6})$'
       AND LOWER(BTRIM(COALESCE(d.mg_estado,'activo'))) <> 'inactivo_venta'
     )
     OR LOWER(BTRIM(COALESCE(t.motivo::text,''))) IN ('baja alquiler','reparación alquiler','reparacion alquiler')
   )
   AND BTRIM(COALESCE(d.numero_interno,'')) ~* '^(MG|NM|NV|CE)\s*0*(\d{1,6})$'
   AND t.fecha_creacion < '2025-09-19'
   AND t.fecha_ingreso >= '2024-01-01';

DO $$
DECLARE
  v_n integer;
BEGIN
  SELECT COUNT(*) INTO v_n FROM objetivo;
  RAISE NOTICE 'OS para no aplica: %', v_n;
  IF v_n NOT BETWEEN 275 AND 290 THEN
    RAISE EXCEPTION 'Se esperaban unas 285 OS para no aplica y hay %: no se aplica nada', v_n;
  END IF;
END $$;

\echo == 2a) Version vigente pendiente o vacia -> no_aplica (esperado: UPDATE 8) ==
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

\echo == 2b) Sin ninguna fila en quotes -> nace la version 1 en no_aplica (esperado: INSERT 0 277) ==
INSERT INTO quotes(ingreso_id, version_num, estado)
SELECT o.id, 1, 'no_aplica'
  FROM objetivo o
 WHERE NOT EXISTS (SELECT 1 FROM quotes q2 WHERE q2.ingreso_id = o.id);

-- 3) Control: ninguna OS del alcance puede seguir en la cola.
DO $$
DECLARE
  v_quedan integer;
BEGIN
  SELECT COUNT(*) INTO v_quedan
    FROM ingresos t
    JOIN devices d ON d.id = t.device_id
   WHERE COALESCE(t.presupuesto_estado, 'pendiente') = 'pendiente'
     AND t.estado NOT IN ('ingresado','entregado','liberado','no_se_repara',
                          'vendido_pendiente_entrega','vendido_entregado')
     AND (
       t.estado <> 'alquilado'
       OR (
         BTRIM(COALESCE(d.numero_interno,'')) ~* '^(MG|NM|NV|CE)\s*0*(\d{1,6})$'
         AND LOWER(BTRIM(COALESCE(d.mg_estado,'activo'))) <> 'inactivo_venta'
       )
       OR LOWER(BTRIM(COALESCE(t.motivo::text,''))) IN ('baja alquiler','reparación alquiler','reparacion alquiler')
     )
     AND BTRIM(COALESCE(d.numero_interno,'')) ~* '^(MG|NM|NV|CE)\s*0*(\d{1,6})$'
     AND t.fecha_creacion < '2025-09-19'
     AND t.fecha_ingreso >= '2024-01-01';
  IF v_quedan > 0 THEN
    RAISE EXCEPTION 'Quedan % OS del alcance en la cola: no se aplica nada', v_quedan;
  END IF;
END $$;

\echo == Cola de Pendientes de Presupuesto despues de aplicar: anio de ingreso y si es propio ==
SELECT EXTRACT(YEAR FROM (t.fecha_ingreso AT TIME ZONE 'America/Argentina/Buenos_Aires'))::int AS anio,
       (BTRIM(COALESCE(d.numero_interno,'')) ~* '^(MG|NM|NV|CE)\s*0*(\d{1,6})$') AS propio,
       COUNT(*) AS os
  FROM ingresos t
  JOIN devices d ON d.id = t.device_id
 WHERE COALESCE(t.presupuesto_estado, 'pendiente') = 'pendiente'
   AND t.estado NOT IN ('ingresado','entregado','liberado','no_se_repara',
                        'vendido_pendiente_entrega','vendido_entregado')
   AND (
     t.estado <> 'alquilado'
     OR (
       BTRIM(COALESCE(d.numero_interno,'')) ~* '^(MG|NM|NV|CE)\s*0*(\d{1,6})$'
       AND LOWER(BTRIM(COALESCE(d.mg_estado,'activo'))) <> 'inactivo_venta'
     )
     OR LOWER(BTRIM(COALESCE(t.motivo::text,''))) IN ('baja alquiler','reparación alquiler','reparacion alquiler')
   )
 GROUP BY 1, 2
 ORDER BY 1, 2;

\echo == Las 11 ventas como quedaron ==
SELECT t.id AS os, d.numero_interno, t.estado::text, d.mg_estado, c.razon_social AS comprador,
       d.mg_venta_fecha::date, t.remito_salida, t.factura_numero, loc.nombre AS ubicacion
  FROM ingresos t
  JOIN devices d ON d.id = t.device_id
  LEFT JOIN customers c ON c.id = d.mg_venta_customer_id
  LEFT JOIN locations loc ON loc.id = t.ubicacion_id
 WHERE t.id IN (26322, 26559, 26560, 26724, 26747, 26749, 25879, 26476, 26696, 26867, 26045)
 ORDER BY t.id;

COMMIT;
