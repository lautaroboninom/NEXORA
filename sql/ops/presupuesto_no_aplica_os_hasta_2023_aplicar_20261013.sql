-- Limpia de la cola "Pendientes de Presupuesto" las OS con fecha de ingreso 2017-2023.
-- Pedido del usuario 13/10/2026, en dos pasos:
--   1) las de equipos que Bejerman muestra vendidos: se registra la venta del MG en NEXORA,
--      igual que DeviceMgVentaView + aplicar_venta_mg_a_ingreso() (devices_views.py);
--   2) todas las demas, incluidas las 2 sin numero de serie (OS 19517 y 24390): presupuesto
--      "no aplica".
--
-- Criterio de la cola: el mismo de _SQL_PENDIENTES_PRESUPUESTO (colas_views.py) y
-- PendientesPresupuestoView (ingresos_views.py). El anio sale de fecha_ingreso en hora de
-- Buenos Aires, acotado a 2000-2023: la OS 29664 tiene fecha_ingreso 0026-08-28 (es de
-- agosto de 2026, mal tipeada) y con un "<= 2023" a secas entraba.
--
-- Medido el 13/10/2026 sobre el dump de prod del 12/10 22:30: 3686 OS, todas en estado
-- 'alquilado' (equipos MG), 1592 equipos. Informe por OS en
-- docs/reportes/os_hasta_2023_presupuesto_no_aplica_20261013.csv.
--
-- VENTAS (10 OS, la ultima OS de cada equipo; rastro en Bejerman SBDASEP):
--   WIOR MARCELA VERONICA (customer 1674), 03/09/2026: RDA R 00004-00005192 (devolucion del
--     alquiler) + RT R 00004-00005194 (salida por venta). La FC A 00002-00010121 se anulo el
--     mismo dia con la NC A 00002-00000593 y se reemitio como FC A 00002-00010122.
--     OS 21407 (MG 5297), 23075 (MG 2072), 23228 (MG 4563), 23435 (MG 6808),
--     23436 (MG 4326), 23787 (MG 5306), 23795 (MG 5245).
--   NOVAMED S.A. (customer 12), 08/06/2026, aporte sin factura (RTN no facturable, mismo
--     bloque que el usuario confirmo el 03/09/2026):
--     OS 23792 (MG 4978) RTN R 00004-00004509; OS 23871 (MG 4333) RTN R 00004-00004510.
--   OXICASTHOMECARE S.R.L. (customer 1657), 03/07/2025: RDN 00004-00003128 + RTN R
--     00004-00003130 con operacion MC (mercaderia, no alquiler).
--     OS 22388 (MG 0917).
--   La OS 25275 (MG 7205) NO se toma como venta: el RTN 00004-00001568 del 03/05/2024 es a
--   MG BIO S.A., un pase entre empresas. Queda con "no aplica" y en la lista de pendientes.
--
-- PRESUPUESTO NO APLICA (el resto, esperado 3668: las 10 vendidas y otras 8 OS viejas de esos
-- mismos equipos salen solas de la cola al quedar el MG vendido). Se escribe `quotes`, no
-- `ingresos.presupuesto_estado`, porque el trigger sync_quote_with_ingreso copia la version
-- vigente a `ingresos` (ver marcar_quote_no_aplica() en quotes_views.py). Solo se pisa una
-- version pendiente (o vacia), o se crea la fila si no hay ninguna.
--
-- Para deshacer: audit.change_log registra cada cambio de devices e ingresos con la hora de
-- esta corrida; los eventos nuevos son device_mg_events con usuario_id NULL y observaciones
-- que empiezan con "Regularizacion 13/10/2026".
--
-- Uso:  psql ... -v ON_ERROR_STOP=1 -f presupuesto_no_aplica_os_hasta_2023_aplicar_20261013.sql

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
  (21407, 'MG 5297', 1674, '2026-09-03 00:00-03', 'FC A 00002-00010122', 'RT R 00004-00005194',
   'Regularizacion 13/10/2026 desde Bejerman: venta a WIOR con RT R 00004-00005194. La FC A 00002-00010121 se anulo con la NC A 00002-00000593 y se reemitio como FC A 00002-00010122.'),
  (23075, 'MG 2072', 1674, '2026-09-03 00:00-03', 'FC A 00002-00010122', 'RT R 00004-00005194',
   'Regularizacion 13/10/2026 desde Bejerman: venta a WIOR con RT R 00004-00005194. La FC A 00002-00010121 se anulo con la NC A 00002-00000593 y se reemitio como FC A 00002-00010122.'),
  (23228, 'MG 4563', 1674, '2026-09-03 00:00-03', 'FC A 00002-00010122', 'RT R 00004-00005194',
   'Regularizacion 13/10/2026 desde Bejerman: venta a WIOR con RT R 00004-00005194. La FC A 00002-00010121 se anulo con la NC A 00002-00000593 y se reemitio como FC A 00002-00010122.'),
  (23435, 'MG 6808', 1674, '2026-09-03 00:00-03', 'FC A 00002-00010122', 'RT R 00004-00005194',
   'Regularizacion 13/10/2026 desde Bejerman: venta a WIOR con RT R 00004-00005194. La FC A 00002-00010121 se anulo con la NC A 00002-00000593 y se reemitio como FC A 00002-00010122.'),
  (23436, 'MG 4326', 1674, '2026-09-03 00:00-03', 'FC A 00002-00010122', 'RT R 00004-00005194',
   'Regularizacion 13/10/2026 desde Bejerman: venta a WIOR con RT R 00004-00005194. La FC A 00002-00010121 se anulo con la NC A 00002-00000593 y se reemitio como FC A 00002-00010122.'),
  (23787, 'MG 5306', 1674, '2026-09-03 00:00-03', 'FC A 00002-00010122', 'RT R 00004-00005194',
   'Regularizacion 13/10/2026 desde Bejerman: venta a WIOR con RT R 00004-00005194. La FC A 00002-00010121 se anulo con la NC A 00002-00000593 y se reemitio como FC A 00002-00010122.'),
  (23795, 'MG 5245', 1674, '2026-09-03 00:00-03', 'FC A 00002-00010122', 'RT R 00004-00005194',
   'Regularizacion 13/10/2026 desde Bejerman: venta a WIOR con RT R 00004-00005194. La FC A 00002-00010121 se anulo con la NC A 00002-00000593 y se reemitio como FC A 00002-00010122.'),
  (23792, 'MG 4978', 12, '2026-06-08 00:00-03', '', 'RTN R 00004-00004509',
   'Regularizacion 13/10/2026 desde Bejerman: aporte a NOVAMED con RTN R 00004-00004509 (no facturable), despues del RDA R 00004-00004482 del 04/06/2026.'),
  (23871, 'MG 4333', 12, '2026-06-08 00:00-03', '', 'RTN R 00004-00004510',
   'Regularizacion 13/10/2026 desde Bejerman: aporte a NOVAMED con RTN R 00004-00004510 (no facturable), despues del RDA R 00004-00004483 del 04/06/2026.'),
  (22388, 'MG 0917', 1657, '2025-07-03 00:00-03', '', 'RTN R 00004-00003130',
   'Regularizacion 13/10/2026 desde Bejerman: salida a OXICASTHOMECARE con RTN R 00004-00003130 (operacion MC, no alquiler), despues del RDN 00004-00003128 del mismo dia.');

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
  IF v_ok <> 10 THEN
    RAISE EXCEPTION 'Ventas: se esperaban 10 OS en condiciones y hay %: no se aplica nada', v_ok;
  END IF;
END $$;

\echo == 1a) Equipo vendido (esperado: UPDATE 10) ==
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

\echo == 1b) Evento de venta del MG (esperado: INSERT 0 10) ==
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

\echo == 1c) OS: alquilado -> vendido_entregado, como aplicar_venta_mg_a_ingreso() (esperado: UPDATE 10) ==
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

\echo == 1d) Historial de la OS (esperado: INSERT 0 10) ==
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
   AND EXTRACT(YEAR FROM (t.fecha_ingreso AT TIME ZONE 'America/Argentina/Buenos_Aires')) BETWEEN 2000 AND 2023;

-- Guarda: lo medido fueron 3668 despues de las ventas. Si entre el dump y la corrida cambio
-- mucho, no se aplica nada (tampoco las ventas: es una sola transaccion).
DO $$
DECLARE
  v_n integer;
BEGIN
  SELECT COUNT(*) INTO v_n FROM objetivo;
  RAISE NOTICE 'OS para no aplica: %', v_n;
  IF v_n NOT BETWEEN 3590 AND 3690 THEN
    RAISE EXCEPTION 'Se esperaban unas 3668 OS para no aplica y hay %: no se aplica nada', v_n;
  END IF;
END $$;

\echo == 2a) Version vigente pendiente o vacia -> no_aplica (esperado: UPDATE 33) ==
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

\echo == 2b) Sin ninguna fila en quotes -> nace la version 1 en no_aplica (esperado: INSERT 0 3635) ==
INSERT INTO quotes(ingreso_id, version_num, estado)
SELECT o.id, 1, 'no_aplica'
  FROM objetivo o
 WHERE NOT EXISTS (SELECT 1 FROM quotes q2 WHERE q2.ingreso_id = o.id);

-- 3) Control: ninguna OS 2000-2023 puede seguir en la cola. Si queda alguna (una version
-- anulada o un caso no medido), se aborta todo y no se aplica nada.
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
     AND EXTRACT(YEAR FROM (t.fecha_ingreso AT TIME ZONE 'America/Argentina/Buenos_Aires')) BETWEEN 2000 AND 2023;
  IF v_quedan > 0 THEN
    RAISE EXCEPTION 'Quedan % OS 2000-2023 en la cola: no se aplica nada', v_quedan;
  END IF;
END $$;

\echo == Cola de Pendientes de Presupuesto por anio de ingreso despues de aplicar ==
\echo == (esperado: nada entre 2000 y 2023) ==
SELECT EXTRACT(YEAR FROM (t.fecha_ingreso AT TIME ZONE 'America/Argentina/Buenos_Aires'))::int AS anio,
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
 GROUP BY 1
 ORDER BY 1;

\echo == Las 10 ventas como quedaron ==
SELECT t.id AS os, d.numero_interno, t.estado::text, d.mg_estado, c.razon_social AS comprador,
       d.mg_venta_fecha::date, t.remito_salida, t.factura_numero, loc.nombre AS ubicacion
  FROM ingresos t
  JOIN devices d ON d.id = t.device_id
  LEFT JOIN customers c ON c.id = d.mg_venta_customer_id
  LEFT JOIN locations loc ON loc.id = t.ubicacion_id
 WHERE t.id IN (21407, 23075, 23228, 23435, 23436, 23787, 23795, 23792, 23871, 22388)
 ORDER BY t.id;

COMMIT;
