-- Limpieza de Pendientes de presupuesto, pedido del usuario 13/10/2026 ("avanzá con el resto"),
-- con el cruce contra Bejerman (SBDASEP / SBDAMGBI) de esa fecha.
--
-- 0) Trazabilidad: device 29144 (OS 29504, NATIVA, serie "MZJ2P0000P" mal tipeada, sin MG) es el
--    MG 7331: la partida "MZJ2P0000P" de Bejerman lleva la observación MG7331 y en MADRE 2026 el
--    MG 7331 es el JAY-10D MZJ2P00009. Sólo se carga el número interno; la serie queda igual que en
--    Bejerman para no cortar el vínculo con la partida.
--
-- 1) Ya volvieron a salir alquiladas (RTA posterior al ingreso) y la OS quedó en diagnosticado o
--    reparado: presupuesto no aplica + estado 'alquilado' con cliente, remito y fecha del RTA.
--
-- 2) Vendidas a NOVAMED (estado de venta, como aplicar_venta_mg_a_ingreso):
--    29107 MG 4700: ya vendido en NEXORA; salió con RT R 00001-00026226 (07/05/2026), FC A
--          00002-00009562 (02/06). La OS quedó en 'reparado' -> vendido_entregado.
--    29636 MG 4198: entró a reparar (RIS 00004-00005010) y se vendió con FC A 00002-00010346
--          (06/10, "Venta RIS Nº 5010"); sin remito, sigue en STR -> vendido_pendiente_entrega.
--    29640 MG 6793: ya vendido en NEXORA con factura "n/a"; entró con RIS 00004-00005000 y se
--          facturó con FC A 00002-00010172 (11/09, "según RIS Nº 5000"); sin remito, sigue en STR
--          -> vendido_pendiente_entrega y se completa la factura del equipo.
--
-- 3) Presupuesto no aplica a 4 de las 8 que se habían dejado para Comercial y que el equipo ya
--    dejó atrás: 29269 (MG 6864, RTA 00004-00004294 a ALCLA 12/05), 29298 (MG 6511, OS posterior
--    29415), 28992 (MG 3514, OS posterior 29040), 29118 (MG 7358, OS posterior 29121).
--
-- Uso:  psql ... -v ON_ERROR_STOP=1 -f cola_presupuesto_realquiladas_y_ventas_aplicar_20261013.sql

BEGIN;

-- ============================================================================
-- 0) MG 7331
-- ============================================================================
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM devices WHERE UPPER(REPLACE(numero_interno, ' ', '')) IN ('MG7331', 'MG07331')) THEN
    RAISE EXCEPTION 'El MG 7331 ya existe en otro equipo: no se aplica nada';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM devices WHERE id = 29144 AND numero_serie = 'MZJ2P0000P' AND COALESCE(numero_interno, '') = '') THEN
    RAISE EXCEPTION 'El device 29144 no está como se midió: no se aplica nada';
  END IF;
END $$;

\echo == 0) Device 29144 -> MG 7331 (esperado: UPDATE 1) ==
UPDATE devices SET numero_interno = 'MG 7331' WHERE id = 29144;

-- ============================================================================
-- 1) Realquiladas
-- ============================================================================
CREATE TEMP TABLE realq (
  ingreso_id integer PRIMARY KEY,
  numero_interno text NOT NULL,
  cliente text NOT NULL,
  remito text NOT NULL,
  fecha date NOT NULL
) ON COMMIT DROP;

INSERT INTO realq VALUES
  (29445, 'MG 6107', 'MERNO S A', 'RTA R 00004-00004455', '2026-06-01'),
  (29453, 'MG 1733', 'SIED SERVICIO INTEGRAL DOMICILIARIO SRL', 'RTA R 00004-00005387', '2026-10-01'),
  (29592, 'MG 4327', 'PRAXAIR ARGENTINA S.R.L.', 'RTA R 00007-00000792', '2026-10-06'),
  (29593, 'MG 5166', 'PRAXAIR ARGENTINA S.R.L.', 'RTA R 00001-00023972', '2026-08-11'),
  (29665, 'MG7217', 'JORGE AGUSTIN', 'RTA R 00004-00005443', '2026-10-08'),
  (29672, 'MG 4792', 'SIED SERVICIO INTEGRAL DOMICILIARIO SRL', 'RTA R 00004-00005387', '2026-10-01'),
  (29706, 'MG 4585', 'SIED SERVICIO INTEGRAL DOMICILIARIO SRL', 'RTA R 00004-00005387', '2026-10-01'),
  (29770, 'MG 4788', 'SIED SERVICIO INTEGRAL DOMICILIARIO SRL', 'RTA R 00004-00005387', '2026-10-01'),
  (29793, 'MG7301', 'BIOMEDICAL S.A.', 'RTA R 00004-00005416', '2026-10-06');

DO $$
DECLARE v integer;
BEGIN
  SELECT COUNT(*) INTO v
    FROM realq r JOIN ingresos t ON t.id = r.ingreso_id JOIN devices d ON d.id = t.device_id
   WHERE t.estado IN ('diagnosticado', 'reparado')
     AND COALESCE(t.presupuesto_estado, 'pendiente') = 'pendiente'
     AND d.numero_interno = r.numero_interno
     AND COALESCE(d.mg_estado, 'activo') = 'activo'
     AND NOT EXISTS (SELECT 1 FROM ingresos t2 WHERE t2.device_id = t.device_id AND t2.id > t.id);
  IF v <> 9 THEN RAISE EXCEPTION 'Realquiladas: se esperaban 9 OS en condiciones y hay %', v; END IF;
END $$;

\echo == 1a) Historial (esperado: INSERT 0 9) ==
INSERT INTO ingreso_events (ticket_id, de_estado, a_estado, usuario_id, ts, comentario)
SELECT r.ingreso_id, t.estado, 'alquilado', NULL, r.fecha::timestamptz,
       'Regularizacion 13/10/2026: alquilado a ' || r.cliente || ' con ' || r.remito || ' (Bejerman)'
  FROM realq r JOIN ingresos t ON t.id = r.ingreso_id;

\echo == 1b) OS -> alquilado (esperado: UPDATE 9) ==
UPDATE ingresos t
   SET estado = 'alquilado',
       alquilado = TRUE,
       alquiler_a = r.cliente,
       alquiler_remito = r.remito,
       alquiler_fecha = r.fecha,
       ubicacion_id = (SELECT id FROM locations WHERE nombre = '-' LIMIT 1)
  FROM realq r
 WHERE t.id = r.ingreso_id;

-- ============================================================================
-- 2) Ventas a NOVAMED
-- ============================================================================
DO $$
BEGIN
  IF (SELECT COUNT(*) FROM ingresos t JOIN devices d ON d.id = t.device_id
       WHERE (t.id = 29107 AND t.estado = 'reparado' AND d.numero_interno = 'MG 4700' AND d.mg_estado = 'inactivo_venta')
          OR (t.id = 29636 AND t.estado = 'diagnosticado' AND d.numero_interno = 'MG 4198' AND d.mg_estado = 'activo')
          OR (t.id = 29640 AND t.estado = 'diagnosticado' AND d.numero_interno = 'MG 6793' AND d.mg_estado = 'inactivo_venta')) <> 3 THEN
    RAISE EXCEPTION 'Ventas: las OS 29107/29636/29640 no están como se midieron';
  END IF;
END $$;

\echo == 2a) MG 4198: venta a NOVAMED (esperado: UPDATE 1 e INSERT 0 1) ==
UPDATE devices
   SET mg_estado = 'inactivo_venta',
       mg_inactivo_desde = '2026-10-06 00:00-03',
       mg_venta_fecha = '2026-10-06 00:00-03',
       mg_venta_factura_numero = 'FC A 00002-00010346',
       mg_venta_remito_numero = NULL,
       mg_venta_observaciones = 'Regularizacion 13/10/2026 desde Bejerman: vendido a NOVAMED con FC A 00002-00010346 del 06/10/2026 ("Venta RIS Nº 5010"); el equipo habia entrado a reparar con RIS R 00004-00005010. Sin remito de salida: entrega pendiente.',
       mg_venta_usuario_id = NULL,
       mg_venta_customer_id = 12,
       alquilado = FALSE,
       alquiler_a = NULL
 WHERE id = 22435;
INSERT INTO device_mg_events(device_id, accion, numero_interno_snapshot, fecha_evento, factura_numero, remito_numero,
                             observaciones, usuario_id, ingreso_id, venta_customer_id, source)
SELECT 22435, 'venta', 'MG 4198', '2026-10-06 00:00-03', 'FC A 00002-00010346', NULL,
       mg_venta_observaciones, NULL, 29636, 12, 'equipos'
  FROM devices WHERE id = 22435;

\echo == 2b) MG 6793: se completa la factura de la venta ya registrada (esperado: UPDATE 1) ==
UPDATE devices
   SET mg_venta_factura_numero = 'FC A 00002-00010172',
       mg_venta_observaciones = COALESCE(NULLIF(mg_venta_observaciones, ''), '') ||
         ' | Regularizacion 13/10/2026: facturado con FC A 00002-00010172 del 11/09/2026 ("segun RIS Nº 5000").'
 WHERE id = 25389 AND COALESCE(mg_venta_factura_numero, '') IN ('', 'n/a', '-', '.');

\echo == 2c) OS 29107 -> vendido_entregado (esperado: UPDATE 1) ==
UPDATE ingresos
   SET estado = 'vendido_entregado',
       factura_numero = 'FC A 00002-00009562',
       fecha_entrega = COALESCE(fecha_entrega, '2026-05-07 00:00-03'),
       remito_salida = COALESCE(NULLIF(remito_salida, ''), 'RT R 00001-00026226'),
       alquilado = FALSE, alquiler_a = NULL, alquiler_remito = NULL, alquiler_fecha = NULL,
       ubicacion_id = (SELECT id FROM locations WHERE nombre = '-' LIMIT 1)
 WHERE id = 29107;

\echo == 2d) OS 29636 y 29640 -> vendido_pendiente_entrega en Taller (esperado: UPDATE 2) ==
UPDATE ingresos
   SET estado = 'vendido_pendiente_entrega',
       factura_numero = CASE id WHEN 29636 THEN 'FC A 00002-00010346' ELSE 'FC A 00002-00010172' END,
       alquilado = FALSE, alquiler_a = NULL, alquiler_remito = NULL, alquiler_fecha = NULL,
       ubicacion_id = (SELECT id FROM locations WHERE LOWER(nombre) = 'taller' ORDER BY id LIMIT 1)
 WHERE id IN (29636, 29640);

\echo == 2e) Historial (esperado: INSERT 0 3) ==
INSERT INTO ingreso_events (ticket_id, de_estado, a_estado, usuario_id, ts, comentario) VALUES
  (29107, 'reparado', 'vendido_entregado', NULL, '2026-05-07 00:00-03', 'Venta registrada y entrega confirmada (NOVAMED, RT R 00001-00026226, FC A 00002-00009562)'),
  (29636, 'diagnosticado', 'vendido_pendiente_entrega', NULL, '2026-10-06 00:00-03', 'Venta registrada; entrega pendiente (NOVAMED, FC A 00002-00010346)'),
  (29640, 'diagnosticado', 'vendido_pendiente_entrega', NULL, '2026-09-11 00:00-03', 'Venta registrada; entrega pendiente (NOVAMED, FC A 00002-00010172)');

-- ============================================================================
-- 1 y 3) Presupuesto no aplica
-- ============================================================================
CREATE TEMP TABLE no_aplica ON COMMIT DROP AS
SELECT ingreso_id AS id FROM realq
UNION SELECT unnest(ARRAY[29269, 29298, 28992, 29118]);

DO $$
DECLARE v integer;
BEGIN
  SELECT COUNT(*) INTO v FROM ingresos WHERE id IN (SELECT id FROM no_aplica) AND COALESCE(presupuesto_estado, 'pendiente') = 'pendiente';
  IF v <> 13 THEN RAISE EXCEPTION 'No aplica: se esperaban 13 OS pendientes y hay %', v; END IF;
END $$;

\echo == 3a) Versión vigente pendiente o vacía -> no_aplica ==
UPDATE quotes q
   SET estado = 'no_aplica'
  FROM (
    SELECT DISTINCT ON (ingreso_id) ingreso_id, id AS quote_id, estado::text AS quote_estado
      FROM quotes
     ORDER BY ingreso_id, COALESCE(version_num, 1) DESC, id DESC
  ) cur
 WHERE q.id = cur.quote_id
   AND COALESCE(cur.quote_estado, '') IN ('', 'pendiente')
   AND cur.ingreso_id IN (SELECT id FROM no_aplica);

\echo == 3b) Sin fila en quotes -> versión 1 en no_aplica ==
INSERT INTO quotes(ingreso_id, version_num, estado)
SELECT n.id, 1, 'no_aplica' FROM no_aplica n
 WHERE NOT EXISTS (SELECT 1 FROM quotes q2 WHERE q2.ingreso_id = n.id);

DO $$
DECLARE v integer;
BEGIN
  SELECT COUNT(*) INTO v FROM ingresos WHERE id IN (SELECT id FROM no_aplica) AND presupuesto_estado <> 'no_aplica';
  IF v > 0 THEN RAISE EXCEPTION 'Quedan % OS sin no_aplica: no se aplica nada', v; END IF;
END $$;

\echo == Cómo quedaron ==
SELECT t.id, d.numero_interno, t.estado::text, t.presupuesto_estado::text, COALESCE(t.alquiler_a, '') AS alquiler_a,
       COALESCE(t.alquiler_remito, '') AS remito_alq, COALESCE(l.nombre, '') AS ubicacion, d.alquilado AS equipo_alquilado
  FROM ingresos t JOIN devices d ON d.id = t.device_id LEFT JOIN locations l ON l.id = t.ubicacion_id
 WHERE t.id IN (SELECT id FROM no_aplica) OR t.id IN (29107, 29636, 29640, 29504)
 ORDER BY t.id;

COMMIT;
