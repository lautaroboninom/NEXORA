-- OS del limbo de la limpieza de Pendientes de presupuesto (13/10/2026) que en el sistema viejo
-- figuran ENTREGADAS. Pedido del usuario 13/10/2026: "si en el sistema viejo figuran entregados es
-- porque fueron entregados, tildalos y vamos descartando más rápido".
--
-- Alcance: 190 OS (2017-2024) de equipos MG en estado 'alquilado', presupuesto ya en no_aplica, que
-- son la última OS de su equipo, sin ningún rastro en Bejerman (ni por partida exacta, ni con
-- tolerancia de tipeo, ni por el MG en la observación de la partida) y con Servicio.Entregado = Sí en
-- Tablas2025 MG-SEPID 2.0.accdb (Servicio.Id = número de OS). Sólo 13 tienen FechaEntrega en el
-- sistema viejo; al resto la fecha de entrega les queda como estaba. Informe:
-- docs/reportes/limbo_estado_inferido_20261013.csv (grupo "9 Sin rastro en Bejerman").
--
-- Efecto: la OS pasa a 'entregado', ubicación '-', sin datos de alquiler, con un evento en el
-- historial. El trigger sync_device_snapshot copia eso al equipo (alquilado = false). No dispara
-- movimientos en Bejerman: no hay stock de estas partidas y los jobs sólo los encolan las vistas.
--
-- Para deshacer: audit.change_log con la hora de esta corrida.
--
-- Uso:  psql ... -v ON_ERROR_STOP=1 -f os_limbo_entregadas_sistema_viejo_aplicar_20261013.sql

BEGIN;

CREATE TEMP TABLE entregada (ingreso_id integer PRIMARY KEY, fecha_viejo date) ON COMMIT DROP;
INSERT INTO entregada VALUES
  (15210, NULL),
  (15432, NULL),
  (15536, NULL),
  (15716, NULL),
  (15982, NULL),
  (16171, NULL),
  (16175, NULL),
  (16208, NULL),
  (16459, NULL),
  (16470, NULL),
  (16486, NULL),
  (16501, NULL),
  (16567, NULL),
  (16574, NULL),
  (16779, NULL),
  (16909, NULL),
  (16910, NULL),
  (17045, NULL),
  (17050, NULL),
  (17177, NULL),
  (17423, NULL),
  (17430, DATE '2017-12-20'),
  (17530, NULL),
  (17585, NULL),
  (17631, NULL),
  (17744, NULL),
  (17782, NULL),
  (17820, NULL),
  (17857, NULL),
  (17875, NULL),
  (17928, NULL),
  (18232, NULL),
  (18266, NULL),
  (18816, NULL),
  (19245, NULL),
  (19256, NULL),
  (19257, NULL),
  (19282, NULL),
  (19303, NULL),
  (19339, NULL),
  (19385, NULL),
  (19420, NULL),
  (19497, NULL),
  (19539, NULL),
  (19540, NULL),
  (19549, DATE '2019-02-08'),
  (19577, NULL),
  (19618, NULL),
  (19620, NULL),
  (19705, NULL),
  (19712, NULL),
  (19722, DATE '2019-03-14'),
  (19747, NULL),
  (19750, DATE '2019-03-19'),
  (19816, NULL),
  (19938, NULL),
  (20002, DATE '2019-06-14'),
  (20034, NULL),
  (20037, NULL),
  (20049, NULL),
  (20071, NULL),
  (20090, NULL),
  (20144, DATE '2019-06-13'),
  (20160, NULL),
  (20170, NULL),
  (20254, NULL),
  (20294, NULL),
  (20295, NULL),
  (20371, DATE '2019-08-09'),
  (20393, DATE '2019-08-12'),
  (20420, NULL),
  (20446, NULL),
  (20447, DATE '2019-09-04'),
  (20463, NULL),
  (20506, DATE '2019-09-19'),
  (20517, NULL),
  (20584, NULL),
  (20619, NULL),
  (20629, NULL),
  (20654, NULL),
  (20675, NULL),
  (20700, DATE '2019-11-04'),
  (20726, NULL),
  (20778, NULL),
  (20785, NULL),
  (20806, NULL),
  (20808, NULL),
  (20809, NULL),
  (20831, NULL),
  (20832, NULL),
  (20833, NULL),
  (20834, NULL),
  (20840, NULL),
  (20843, NULL),
  (20845, NULL),
  (20846, NULL),
  (20848, NULL),
  (20854, NULL),
  (20855, NULL),
  (20911, NULL),
  (20947, NULL),
  (20951, NULL),
  (20982, NULL),
  (20983, NULL),
  (21003, NULL),
  (21007, NULL),
  (21008, NULL),
  (21009, NULL),
  (21036, NULL),
  (21048, NULL),
  (21050, NULL),
  (21067, NULL),
  (21088, NULL),
  (21090, NULL),
  (21093, NULL),
  (21094, NULL),
  (21098, NULL),
  (21128, NULL),
  (21133, NULL),
  (21149, NULL),
  (21150, NULL),
  (21152, NULL),
  (21162, NULL),
  (21187, NULL),
  (21192, NULL),
  (21219, NULL),
  (21221, NULL),
  (21223, NULL),
  (21224, NULL),
  (21225, NULL),
  (21230, NULL),
  (21233, NULL),
  (21262, NULL),
  (21276, NULL),
  (21304, NULL),
  (21306, NULL),
  (21307, NULL),
  (21311, NULL),
  (21312, NULL),
  (21313, NULL),
  (21314, NULL),
  (21346, NULL),
  (21347, NULL),
  (21361, NULL),
  (21478, NULL),
  (21484, NULL),
  (21485, NULL),
  (21486, NULL),
  (21487, NULL),
  (21580, NULL),
  (21911, NULL),
  (21920, NULL),
  (22035, NULL),
  (22113, NULL),
  (22144, NULL),
  (22183, NULL),
  (22240, NULL),
  (22399, NULL),
  (22418, NULL),
  (22419, NULL),
  (22706, NULL),
  (22707, NULL),
  (22708, NULL),
  (22744, NULL),
  (22832, DATE '2021-07-02'),
  (22873, NULL),
  (22978, NULL),
  (23011, NULL),
  (23058, NULL),
  (23070, NULL),
  (23071, NULL),
  (23095, NULL),
  (23165, NULL),
  (23194, NULL),
  (23247, NULL),
  (23302, NULL),
  (23372, NULL),
  (23677, NULL),
  (23876, NULL),
  (24350, NULL),
  (24352, NULL),
  (24355, NULL),
  (24458, NULL),
  (24555, NULL),
  (24812, NULL),
  (25057, NULL),
  (25118, NULL),
  (25121, NULL),
  (26296, NULL),
  (26393, DATE '2024-11-27');

DO $$
DECLARE
  v_ok integer;
BEGIN
  SELECT COUNT(*) INTO v_ok
    FROM entregada e
    JOIN ingresos t ON t.id = e.ingreso_id
    JOIN devices d ON d.id = t.device_id
   WHERE t.estado = 'alquilado'
     AND t.presupuesto_estado = 'no_aplica'
     AND COALESCE(d.mg_estado, 'activo') = 'activo'
     AND NOT EXISTS (SELECT 1 FROM ingresos t2 WHERE t2.device_id = t.device_id AND t2.id > t.id);
  IF v_ok <> 190 THEN
    RAISE EXCEPTION 'Se esperaban 190 OS en condiciones y hay %: no se aplica nada', v_ok;
  END IF;
END $$;

\echo == Historial de la OS (esperado: INSERT 0 190) ==
INSERT INTO ingreso_events (ticket_id, de_estado, a_estado, usuario_id, ts, comentario)
SELECT e.ingreso_id, 'alquilado', 'entregado', NULL,
       COALESCE(e.fecha_viejo::timestamptz, CURRENT_TIMESTAMP),
       'Regularizacion 13/10/2026: entregado segun el sistema viejo (sin rastro en Bejerman)'
  FROM entregada e;

\echo == OS alquilado -> entregado (esperado: UPDATE 190) ==
UPDATE ingresos t
   SET estado = 'entregado',
       fecha_entrega = COALESCE(t.fecha_entrega,
                                CASE WHEN e.fecha_viejo >= (t.fecha_ingreso AT TIME ZONE 'America/Argentina/Buenos_Aires')::date
                                     THEN e.fecha_viejo::timestamptz END),
       alquilado = FALSE,
       alquiler_a = NULL,
       alquiler_remito = NULL,
       alquiler_fecha = NULL,
       ubicacion_id = (SELECT id FROM locations WHERE nombre = '-' LIMIT 1)
  FROM entregada e
 WHERE t.id = e.ingreso_id;

\echo == Control: equipos que siguen marcados alquilados (esperado: 0) ==
SELECT COUNT(*) AS siguen_alquilados
  FROM entregada e
  JOIN ingresos t ON t.id = e.ingreso_id
  JOIN devices d ON d.id = t.device_id
 WHERE d.alquilado;

SELECT t.estado::text, COUNT(*) AS os, COUNT(t.fecha_entrega) AS con_fecha_entrega
  FROM entregada e JOIN ingresos t ON t.id = e.ingreso_id
 GROUP BY 1;

COMMIT;
