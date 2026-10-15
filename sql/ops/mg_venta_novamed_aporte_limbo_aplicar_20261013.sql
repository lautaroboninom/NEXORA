-- Venta (aporte a NOVAMED) de dos MG que estaban en el limbo de la limpieza de Pendientes de
-- presupuesto del 13/10/2026. Los encontró la búsqueda por partida con tolerancia: en Bejerman la
-- partida no es exactamente la serie de NEXORA.
--   OS 26460 MG 4869: NEXORA "P-06-377-19A", Bejerman "P-06-377-19-A".
--   OS 26561 MG 7307: NEXORA "MG 7307", Bejerman "09-23T-1004-0017" (observación de la partida = MG 7307).
-- Los dos: RDA R 00004-00004482 del 04/06/2026 y RTN R 00004-00004509 (no facturable) del 08/06/2026,
-- el mismo bloque de aportes a NOVAMED de los lotes anteriores. Mismo efecto que DeviceMgVentaView +
-- aplicar_venta_mg_a_ingreso() (devices_views.py). El presupuesto de estas OS ya está en no_aplica.
--
-- Uso:  psql ... -v ON_ERROR_STOP=1 -f mg_venta_novamed_aporte_limbo_aplicar_20261013.sql

BEGIN;

CREATE TEMP TABLE venta (
  ingreso_id integer PRIMARY KEY,
  numero_interno text NOT NULL,
  customer_id integer NOT NULL,
  fecha_venta timestamptz NOT NULL,
  remito text NOT NULL,
  observaciones text NOT NULL
) ON COMMIT DROP;

INSERT INTO venta VALUES
  (26460, 'MG 4869', 12, '2026-06-08 00:00-03', 'RTN R 00004-00004509',
   'Regularizacion 13/10/2026 desde Bejerman: aporte a NOVAMED con RTN R 00004-00004509 (no facturable), despues del RDA R 00004-00004482 del 04/06/2026. Partida en Bejerman: P-06-377-19-A.'),
  (26561, 'MG 7307', 12, '2026-06-08 00:00-03', 'RTN R 00004-00004509',
   'Regularizacion 13/10/2026 desde Bejerman: aporte a NOVAMED con RTN R 00004-00004509 (no facturable), despues del RDA R 00004-00004482 del 04/06/2026. Partida en Bejerman: 09-23T-1004-0017.');

DO $$
DECLARE
  v_ok integer;
BEGIN
  SELECT COUNT(*) INTO v_ok
    FROM venta v
    JOIN ingresos t ON t.id = v.ingreso_id
    JOIN devices d ON d.id = t.device_id
   WHERE t.estado = 'alquilado'
     AND COALESCE(d.mg_estado, 'activo') = 'activo'
     AND UPPER(REPLACE(d.numero_interno, ' ', '')) = UPPER(REPLACE(v.numero_interno, ' ', ''))
     AND NOT EXISTS (SELECT 1 FROM ingresos t2 WHERE t2.device_id = t.device_id AND t2.id > t.id);
  IF v_ok <> 2 THEN
    RAISE EXCEPTION 'Se esperaban 2 OS en condiciones y hay %: no se aplica nada', v_ok;
  END IF;
END $$;

\echo == Equipo vendido (esperado: UPDATE 2) ==
UPDATE devices d
   SET mg_estado = 'inactivo_venta',
       mg_inactivo_desde = v.fecha_venta,
       mg_venta_fecha = v.fecha_venta,
       mg_venta_factura_numero = NULL,
       mg_venta_remito_numero = v.remito,
       mg_venta_observaciones = v.observaciones,
       mg_venta_usuario_id = NULL,
       mg_venta_customer_id = v.customer_id,
       alquilado = FALSE,
       alquiler_a = NULL,
       ubicacion_id = (SELECT id FROM locations WHERE nombre = '-' LIMIT 1)
  FROM venta v
  JOIN ingresos t ON t.id = v.ingreso_id
 WHERE d.id = t.device_id;

\echo == Evento de venta del MG (esperado: INSERT 0 2) ==
INSERT INTO device_mg_events(
  device_id, accion, numero_interno_snapshot, fecha_evento, factura_numero, remito_numero,
  observaciones, usuario_id, ingreso_id, venta_customer_id, source
)
SELECT t.device_id, 'venta', v.numero_interno, v.fecha_venta, NULL, v.remito,
       v.observaciones, NULL, v.ingreso_id, v.customer_id, 'equipos'
  FROM venta v
  JOIN ingresos t ON t.id = v.ingreso_id;

\echo == OS: alquilado -> vendido_entregado (esperado: UPDATE 2) ==
UPDATE ingresos t
   SET estado = 'vendido_entregado',
       fecha_entrega = COALESCE(t.fecha_entrega, v.fecha_venta),
       remito_salida = COALESCE(NULLIF(t.remito_salida, ''), v.remito),
       alquilado = FALSE,
       alquiler_a = NULL,
       alquiler_remito = NULL,
       alquiler_fecha = NULL,
       ubicacion_id = (SELECT id FROM locations WHERE nombre = '-' LIMIT 1)
  FROM venta v
 WHERE t.id = v.ingreso_id;

\echo == Historial de la OS (esperado: INSERT 0 2) ==
INSERT INTO ingreso_events (ticket_id, de_estado, a_estado, usuario_id, ts, comentario)
SELECT v.ingreso_id, 'alquilado', 'vendido_entregado', NULL, v.fecha_venta,
       'Venta registrada y entrega confirmada'
  FROM venta v;

SELECT t.id AS os, d.numero_interno, t.estado::text, d.mg_estado, c.razon_social AS comprador,
       d.mg_venta_fecha::date, t.remito_salida, t.presupuesto_estado::text AS presupuesto
  FROM ingresos t
  JOIN devices d ON d.id = t.device_id
  LEFT JOIN customers c ON c.id = d.mg_venta_customer_id
 WHERE t.id IN (26460, 26561);

COMMIT;
