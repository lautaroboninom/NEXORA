-- Ventas a NOVAMED que figuran en Bejerman y no estaban (o estaban mal) indicadas en NEXORA.
-- Pedido del usuario 13/10/2026: "de los remitos de venta que encontraste a NOVAMED fijate si están
-- todos bien indicados en NEXORA".
--
-- Fuente: todos los RT/RTN/FC con operación MC al cliente NOV, con partida, en SBDASEP y SBDAMGBI,
-- cruzados con los equipos propios de NEXORA por serie (tolerando el prefijo N) y por el MG de la
-- observación de la partida. Se descartó como venta lo que después NOVAMED devolvió o volvimos a
-- alquilar a otro cliente (MG 7444: RDN 00001-00026421 y RTA 00001-00026483 a BIOMEDICAL). El RT de
-- SEPID a MG BIO del mismo día es la triangulación entre empresas de la misma venta, no una vuelta.
-- Resultado: 105 ventas definitivas; 29 ya cargadas, 76 sin cargar (4 quedan afuera porque la misma
-- partida está en dos equipos de NEXORA: MG 7103 / MG 007103 y NM 0228 / MG 7104).
--
-- 1) 72 equipos sin la venta: se registra como DeviceMgVentaView (comprador NOVAMED, remito y factura
--    de Bejerman, evento de venta). Si la última OS del equipo es ANTERIOR a la venta y está en un
--    estado que la venta cierra (alquilado, reparado, controlado sin defecto, no se repara, liberado),
--    pasa a vendido_entregado con el remito: el equipo ya salió. Si es POSTERIOR (una reparación que
--    mandó NOVAMED u otro cliente) o está en baja/ingresado, la OS no se toca.
-- 2) 8 equipos con la venta cargada pero el remito o la factura con basura ("-", ".", "n/a", "4510",
--    "a fin de mes", "28/10/24"): se completan con los comprobantes de Bejerman; el valor viejo queda en
--    las observaciones. La OS 29134 (MG 7132, vendido con RTN 00004-00004033 el 06/04) seguía en
--    controlado_sin_defecto: pasa a vendido_entregado.
--
-- Para deshacer: audit.change_log con la hora de esta corrida; device_mg_events con observaciones
-- "Regularizacion 13/10/2026 desde Bejerman: venta a NOVAMED".
--
-- Uso:  psql ... -v ON_ERROR_STOP=1 -f ventas_novamed_bejerman_aplicar_20261013.sql

BEGIN;

CREATE TEMP TABLE venta (
  device_id integer PRIMARY KEY,
  numero_interno text NOT NULL,
  fecha date NOT NULL,
  remito text NOT NULL,
  factura text NOT NULL,
  ultima_os integer,
  cerrar_os boolean NOT NULL,
  partida text NOT NULL
) ON COMMIT DROP;

INSERT INTO venta VALUES
  (19814, 'MG 0092', DATE '2025-03-13', 'RT R 00001-00023560', 'FC A 00002-00017972', 26575, FALSE, '09-0001-10-A'),
  (18471, 'MG 0318', DATE '2026-06-08', 'RTN R 00004-00004509', '', 28014, TRUE, '07-0559-10-A'),
  (16819, 'MG 1006', DATE '2025-09-18', 'RTN R 00001-00023776', '', 28124, TRUE, '06-0888-09-A'),
  (16828, 'MG 1231', DATE '2026-06-08', 'RTN R 00004-00004509', '', 28168, TRUE, 'PI-115382'),
  (15555, 'MG 1292', DATE '2025-12-23', 'RTN R 00001-00023863', '', 28774, TRUE, '07-0558-10-A'),
  (26694, 'MG 1748', DATE '2025-03-10', 'RT R 00001-00023551', 'FC A 00002-00017972', 26694, FALSE, '5143458'),
  (16586, 'MG 1761', DATE '2025-09-18', 'RTN R 00001-00023778', '', 26246, FALSE, '04-0506-11A'),
  (18827, 'MG 1820', DATE '2025-03-10', 'RT R 00001-00023551', 'FC A 00002-00017972', 27146, FALSE, '5143433'),
  (20374, 'MG 2071', DATE '2025-09-18', 'RTN R 00001-00023778', '', 23738, FALSE, '04-0497-11-A'),
  (19693, 'MG 2242', DATE '2025-03-10', 'RT R 00001-00023551', 'FC A 00002-00017972', 27173, FALSE, '5158302'),
  (21914, 'MG 2817', DATE '2026-06-08', 'RTN R 00004-00004510', '', 21914, TRUE, '21686646'),
  (23711, 'MG 2887', DATE '2025-09-18', 'RTN R 00001-00023778', '', 28421, FALSE, '560000838'),
  (15231, 'MG 2889', DATE '2026-06-08', 'RTN R 00004-00004509', '', 27454, TRUE, '560006909'),
  (26944, 'MG 3078', DATE '2026-08-03', '', 'FC A 00002-00009911', 29366, TRUE, 'P133516769DED'),
  (28781, 'MG 3121', DATE '2026-02-13', 'RTN R 00001-00025983', '', 28817, TRUE, '011514040251'),
  (16111, 'MG 3131', DATE '2026-06-08', 'RTN R 00004-00004509', '', 29215, TRUE, '11514040281'),
  (25511, 'MG 3137', DATE '2025-06-09', 'RT R 00001-00023269', 'FC A 00002-00018082', 28119, TRUE, '11514040214'),
  (21503, 'MG 3239', DATE '2025-09-18', 'RTN R 00001-00023778', '', 28193, FALSE, 'G01840486'),
  (15188, 'MG 3315', DATE '2026-02-19', 'RTN R 00001-00025998', '', 28994, TRUE, 'G003184210'),
  (15809, 'MG 3399', DATE '2025-04-22', 'RT R 00001-00023597', 'FC A 00002-00018009', 27024, TRUE, 'D-0103'),
  (15532, 'MG 3474', DATE '2025-11-27', 'RTN R 00001-00023854', '', 25437, FALSE, 'G07838665'),
  (20166, 'MG 4061', DATE '2025-03-10', 'RT R 00001-00023551', 'FC A 00002-00017972', 27298, FALSE, 'MZJ5S121927'),
  (28780, 'MG 4442', DATE '2026-02-13', 'RTN R 00001-00025983', '', 28816, TRUE, '560003624'),
  (24454, 'MG 4501', DATE '2025-09-05', 'RTN R 00007-00000221', '', 24454, FALSE, 'MZJ5S121955'),
  (27183, 'MG 4546', DATE '2026-06-08', 'RTN R 00004-00004511', '', 29120, TRUE, 'MZJ5S121739'),
  (27701, 'MG 4595', DATE '2026-06-08', 'RTN R 00004-00004510', '', 27701, TRUE, 'MZJ5S121687'),
  (24416, 'MG 4734', DATE '2026-06-08', 'RTN R 00004-00004511', '', 27321, TRUE, 'MZJ5S121817'),
  (24219, 'MG 4735', DATE '2025-09-05', 'RTN R 00007-00000221', '', 27322, FALSE, 'MZJ5S121793'),
  (21607, 'MG 4739', DATE '2025-09-05', 'RTN R 00001-00023669', '', 27323, FALSE, 'MZJ5S121848'),
  (27324, 'MG 4763', DATE '2025-09-05', 'RTN R 00007-00000221', '', 29160, FALSE, 'MZJ5S121624'),
  (28743, 'MG 4793', DATE '2025-12-05', 'RTN R 00001-00023865', '', 28740, TRUE, 'MZJ5S121899'),
  (28740, 'MG 4862', DATE '2025-12-23', 'RTN R 00001-00023863', '', 28736, TRUE, 'P-06-400-19A'),
  (21721, 'MG 4894', DATE '2026-06-08', 'RTN R 00004-00004511', '', 27718, TRUE, 'MZJ5S121901'),
  (20823, 'MG 4899', DATE '2026-06-08', 'RTN R 00004-00004510', '', 25438, TRUE, 'G02849323'),
  (27488, 'MG 4908', DATE '2025-09-05', 'RTN R 00007-00000221', '', 27488, FALSE, 'MZJ5S121689'),
  (22135, 'MG 4929', DATE '2026-06-08', 'RTN R 00004-00004511', '', 27711, TRUE, 'MZJ5S121799'),
  (23733, 'MG 4931', DATE '2026-06-08', 'RTN R 00004-00004511', '', 27616, TRUE, 'MZJ5S121762'),
  (25840, 'MG 4984', DATE '2025-09-05', 'RTN R 00007-00000221', '', 27327, FALSE, 'MZJ5S121698'),
  (23935, 'MG 4993', DATE '2026-06-08', 'RTN R 00004-00004510', '', 27707, TRUE, 'MZJ5S121651'),
  (22508, 'MG 5011', DATE '2026-06-08', 'RTN R 00004-00004510', '', 27619, TRUE, 'MZJ5S121665'),
  (27579, 'MG 5827', DATE '2025-09-05', 'RTN R 00001-00023667', '', 27579, FALSE, 'H2120105352'),
  (25004, 'MG 6330', DATE '2025-05-15', 'RT R 00001-00023626', 'FC A 00002-00018040', 25004, FALSE, 'MG6330'),
  (27598, 'MG 6406', DATE '2025-09-05', 'RTN R 00007-00000221', '', 27598, FALSE, 'MZJ5S121176'),
  (27332, 'MG 6411', DATE '2026-06-08', 'RTN R 00004-00004510', '', 27353, TRUE, 'MZJ5S121193'),
  (27705, 'MG 6420', DATE '2026-06-08', 'RTN R 00004-00004510', '', 28717, TRUE, 'MZJ5S121235'),
  (27600, 'MG 6447', DATE '2025-09-05', 'RTN R 00007-00000221', '', 27600, FALSE, 'MZJ5S121420'),
  (22862, 'MG 6569', DATE '2025-10-13', 'RT R 00001-00023804', 'FC A 00002-00018206', 22862, FALSE, 'B0111'),
  (24673, 'MG 6572', DATE '2026-06-08', 'RTN R 00004-00004510', '', 27128, TRUE, 'MZJ5S121148'),
  (19464, 'MG 6631', DATE '2026-02-13', 'RTN R 00001-00025983', '', 28815, TRUE, '150650041'),
  (26013, 'MG 6700', DATE '2025-03-05', 'RT R 00001-00023394', 'FC A 00002-00017972', 27996, FALSE, 'B 2876'),
  (24341, 'MG 6759', DATE '2026-04-16', 'RT R 00004-00004114', 'FC A 00002-00009305', 29146, FALSE, 'MZJ5S416988'),
  (24224, 'MG 6778', DATE '2026-06-08', 'RTN R 00004-00004511', '', 27712, TRUE, 'MZJ5S416941'),
  (24277, 'MG 7096', DATE '2025-10-29', 'RTN R 00001-00023820', '', 28048, TRUE, 'MZJ10S72906'),
  (26295, 'MG 7153', DATE '2026-09-24', 'RT R 00001-00026397', 'FC A 00002-00010346', 29403, TRUE, 'MBB2203480'),
  (26303, 'MG 7163', DATE '2026-06-08', 'RTN R 00004-00004509', '', 27085, TRUE, 'MBB2203490'),
  (26305, 'MG 7167', DATE '2026-06-08', 'RTN R 00004-00004509', '', 27070, TRUE, 'MBB2203494'),
  (25282, 'MG 7182', DATE '2026-06-08', 'RTN R 00004-00004510', '', 27167, TRUE, 'C8-1320'),
  (26165, 'MG 7284', DATE '2025-11-26', 'RTN R 00001-00023836', '', 26165, FALSE, 'E7765'),
  (27015, 'MG 7285', DATE '2025-09-05', 'RTN R 00001-00023667', '', 27987, TRUE, 'T2A22701092'),
  (27482, 'MG 7288', DATE '2025-04-23', 'RT R 00001-00023599', 'FC A 00002-00018009', 27482, TRUE, 'H2122805350'),
  (27481, 'MG 7289', DATE '2025-04-23', 'RT R 00001-00023599', 'FC A 00002-00018009', 27481, TRUE, 'T2A22701091'),
  (29223, 'MG 7298', DATE '2026-09-04', 'RTN R 00001-00026460', '', 29630, TRUE, 'H2123405080'),
  (27450, 'MG 7303', DATE '2026-06-08', 'RTN R 00004-00004509', '', 27450, TRUE, '09-23T-1004-0007'),
  (27266, 'MG 7304', DATE '2025-03-13', 'RT R 00001-00023560', 'FC A 00002-00017972', 27266, FALSE, '09-23T-1004-0018'),
  (27267, 'MG 7305', DATE '2025-03-05', 'RT R 00001-00023394', 'FC A 00002-00017972', 27307, TRUE, '09-23T-1004-0015'),
  (28169, 'MG 7306', DATE '2025-09-18', 'RTN R 00001-00023776', '', 28169, FALSE, 'MA23080040049'),
  (26824, 'MG 7311', DATE '2026-06-08', 'RTN R 00004-00004509', '', 26874, TRUE, 'MA23080040053'),
  (26981, 'MG 7316', DATE '2026-06-08', 'RTN R 00004-00004509', '', 27301, TRUE, 'MA23080040054'),
  (29287, 'MG 7558', DATE '2026-09-24', 'RT R 00001-00026397', 'FC A 00002-00010346', 29787, FALSE, 'MBB2600287'),
  (27911, 'MG 7985', DATE '2025-09-17', 'RTN R 00001-00023775', '', 29313, FALSE, 'A1-0137'),
  (28308, 'MG 7989', DATE '2025-07-15', 'RT R 00001-00023725', '', 29314, FALSE, 'A1-0139'),
  (28774, 'NM 0258', DATE '2025-09-18', 'RTN R 00001-00023778', '', 28803, FALSE, 'F0019');

DO $$
DECLARE v integer;
BEGIN
  SELECT COUNT(*) INTO v
    FROM venta x JOIN devices d ON d.id = x.device_id
   WHERE d.numero_interno = x.numero_interno
     AND COALESCE(d.mg_estado, 'activo') = 'activo'
     AND (x.ultima_os IS NULL OR x.ultima_os = (SELECT MAX(t.id) FROM ingresos t WHERE t.device_id = d.id));
  IF v <> 72 THEN RAISE EXCEPTION 'Se esperaban 72 equipos en condiciones y hay %: no se aplica nada', v; END IF;
  SELECT COUNT(*) INTO v
    FROM venta x JOIN ingresos t ON t.id = x.ultima_os
   WHERE x.cerrar_os AND t.estado IN ('alquilado','reparado','controlado_sin_defecto','no_se_repara','liberado');
  IF v <> 42 THEN RAISE EXCEPTION 'Se esperaban 42 OS para cerrar y hay %: no se aplica nada', v; END IF;
END $$;

\echo == 1a) Equipos: venta a NOVAMED (esperado: UPDATE 72) ==
UPDATE devices d
   SET mg_estado = 'inactivo_venta',
       mg_inactivo_desde = x.fecha::timestamptz,
       mg_venta_fecha = x.fecha::timestamptz,
       mg_venta_factura_numero = NULLIF(x.factura, ''),
       mg_venta_remito_numero = NULLIF(x.remito, ''),
       mg_venta_observaciones = 'Regularizacion 13/10/2026 desde Bejerman: venta a NOVAMED'
         || CASE WHEN x.remito <> '' THEN ' con ' || x.remito ELSE '' END
         || CASE WHEN x.factura <> '' THEN ' / ' || x.factura ELSE '' END
         || ' (partida ' || x.partida || ').',
       mg_venta_usuario_id = NULL,
       mg_venta_customer_id = 12,
       alquilado = FALSE,
       alquiler_a = NULL
  FROM venta x
 WHERE d.id = x.device_id;

\echo == 1b) Evento de venta (esperado: INSERT 0 72) ==
INSERT INTO device_mg_events(device_id, accion, numero_interno_snapshot, fecha_evento, factura_numero, remito_numero,
                             observaciones, usuario_id, ingreso_id, venta_customer_id, source)
SELECT x.device_id, 'venta', x.numero_interno, x.fecha::timestamptz, NULLIF(x.factura, ''), NULLIF(x.remito, ''),
       d.mg_venta_observaciones, NULL, CASE WHEN x.cerrar_os THEN x.ultima_os END, 12, 'equipos'
  FROM venta x JOIN devices d ON d.id = x.device_id;

\echo == 1c) Historial de las OS que se cierran (esperado: INSERT 0 42) ==
INSERT INTO ingreso_events (ticket_id, de_estado, a_estado, usuario_id, ts, comentario)
SELECT t.id, t.estado, 'vendido_entregado', NULL, x.fecha::timestamptz,
       'Venta registrada y entrega confirmada (NOVAMED, ' || COALESCE(NULLIF(x.remito, ''), x.factura) || ')'
  FROM venta x JOIN ingresos t ON t.id = x.ultima_os
 WHERE x.cerrar_os;

\echo == 1d) OS -> vendido_entregado (esperado: UPDATE 42) ==
UPDATE ingresos t
   SET estado = 'vendido_entregado',
       factura_numero = COALESCE(NULLIF(x.factura, ''), t.factura_numero),
       fecha_entrega = COALESCE(t.fecha_entrega, x.fecha::timestamptz),
       remito_salida = COALESCE(NULLIF(t.remito_salida, ''), NULLIF(x.remito, '')),
       alquilado = FALSE, alquiler_a = NULL, alquiler_remito = NULL, alquiler_fecha = NULL,
       ubicacion_id = (SELECT id FROM locations WHERE nombre = '-' LIMIT 1)
  FROM venta x
 WHERE t.id = x.ultima_os AND x.cerrar_os;

-- ============================================================================
-- 2) Ventas ya cargadas con datos incompletos
-- ============================================================================
CREATE TEMP TABLE completar (device_id integer PRIMARY KEY, remito text, factura text) ON COMMIT DROP;
INSERT INTO completar VALUES
  (21606, 'RT R 00001-00026226', 'FC A 00002-00009562'),
  (27367, 'RTN R 00004-00004510', NULL),
  (26159, 'RTN R 00004-00004510', NULL),
  (27700, 'RTN R 00004-00004511', NULL),
  (24338, 'RTN R 00004-00004511', NULL),
  (26327, NULL, 'FC A 00002-00009909'),
  (24822, 'RTN R 00004-00004033', NULL),
  (26522, NULL, 'FC A 00002-00010172');

\echo == 2a) Remito y factura reales (esperado: UPDATE 8) ==
UPDATE devices d
   SET mg_venta_remito_numero = COALESCE(c.remito, CASE WHEN BTRIM(COALESCE(d.mg_venta_remito_numero, '')) IN ('', '-', '.', 'n/a', 'a fin de mes') THEN NULL ELSE d.mg_venta_remito_numero END),
       mg_venta_factura_numero = COALESCE(c.factura, CASE WHEN BTRIM(COALESCE(d.mg_venta_factura_numero, '')) IN ('', '-', '.', 'n/a') OR d.mg_venta_factura_numero ~ '^[0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4}$' THEN NULL ELSE d.mg_venta_factura_numero END),
       mg_venta_observaciones = COALESCE(NULLIF(d.mg_venta_observaciones, '') || ' | ', '')
         || 'Regularizacion 13/10/2026 desde Bejerman: remito/factura antes "' || COALESCE(d.mg_venta_remito_numero, '') || '" / "' || COALESCE(d.mg_venta_factura_numero, '') || '".'
  FROM completar c
 WHERE d.id = c.device_id AND d.mg_estado = 'inactivo_venta';

\echo == 2b) OS 29134 (MG 7132) -> vendido_entregado (esperado: INSERT 0 1 y UPDATE 1) ==
INSERT INTO ingreso_events (ticket_id, de_estado, a_estado, usuario_id, ts, comentario)
SELECT 29134, estado, 'vendido_entregado', NULL, '2026-04-06 00:00-03', 'Venta registrada y entrega confirmada (NOVAMED, RTN R 00004-00004033)'
  FROM ingresos WHERE id = 29134 AND estado = 'controlado_sin_defecto';
UPDATE ingresos
   SET estado = 'vendido_entregado',
       fecha_entrega = COALESCE(fecha_entrega, '2026-04-06 00:00-03'),
       remito_salida = COALESCE(NULLIF(remito_salida, ''), 'RTN R 00004-00004033'),
       alquilado = FALSE, alquiler_a = NULL, alquiler_remito = NULL, alquiler_fecha = NULL,
       ubicacion_id = (SELECT id FROM locations WHERE nombre = '-' LIMIT 1)
 WHERE id = 29134 AND estado = 'controlado_sin_defecto';

\echo == Control: equipos de la lista que no quedaron vendidos a NOVAMED (esperado: 0) ==
SELECT COUNT(*) FROM devices d WHERE d.id IN (SELECT device_id FROM venta UNION SELECT device_id FROM completar)
   AND NOT (d.mg_estado = 'inactivo_venta' AND d.mg_venta_customer_id = 12);

\echo == Estado de las últimas OS ==
SELECT t.estado::text, x.cerrar_os, COUNT(*) FROM venta x JOIN ingresos t ON t.id = x.ultima_os GROUP BY 1, 2 ORDER BY 2, 1;

COMMIT;
