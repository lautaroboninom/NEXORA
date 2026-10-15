-- OS 29704: Longfian JAY-10D N/S MZJ2P00005 figuraba como equipo de cliente (NATIVA) porque al
-- device 29266 nunca se le cargó el número interno. Pedido del usuario 13/10/2026: "tuvo que haber
-- sido un equipo nuestro, fijate en el Excel de madres y en Bejerman; hay que indicar que se vendió
-- a NOVAMED".
--
-- MADRE 2026.xlsx (Z:\MG BIO\MADRES), hoja "ALTAS Y BAJAS", fila 74: MZJ2P00005 = MG 7321, alta
-- 15/10/2024, BAJA 23/09/2026 por VENTA a NOVAMED.
-- Bejerman SBDASEP, partida MZJ2P00005: RTN R 00001-00024353 (ALQ, NATIVA, 15/10/2024); RDA R
-- 00004-00005200 (03/09/2026, cita la OS 29704); STR -> STL el 07/09; RTN R 00001-00026417 (MC,
-- NOVAMED, 15/09/2026). Stock final 0 en todos los depósitos: Bejerman ya está bien.
--
-- En NEXORA: se carga MG 7321 en el device y se registra la venta como DeviceMgVentaView; la OS
-- queda vendido_entregado (el equipo ya salió con el RTN) en la ubicación '-'.
--
-- Uso:  psql ... -v ON_ERROR_STOP=1 -f mg7321_venta_novamed_os29704_aplicar_20261013.sql

BEGIN;

DO $$
DECLARE v integer;
BEGIN
  SELECT COUNT(*) INTO v
    FROM ingresos t JOIN devices d ON d.id = t.device_id
   WHERE t.id = 29704 AND d.id = 29266 AND d.numero_serie = 'MZJ2P00005'
     AND COALESCE(d.numero_interno, '') = '' AND COALESCE(d.mg_estado, 'activo') = 'activo'
     AND t.estado = 'reparado'
     AND NOT EXISTS (SELECT 1 FROM ingresos t2 WHERE t2.device_id = d.id AND t2.id > t.id);
  IF v <> 1 THEN RAISE EXCEPTION 'La OS 29704 / device 29266 no está como se midió: no se aplica nada'; END IF;
  SELECT COUNT(*) INTO v FROM devices WHERE UPPER(REPLACE(numero_interno, ' ', '')) IN ('MG7321', 'MG07321');
  IF v <> 0 THEN RAISE EXCEPTION 'El MG 7321 ya existe en otro equipo: no se aplica nada'; END IF;
END $$;

\echo == Equipo: MG 7321 y venta a NOVAMED (esperado: UPDATE 1) ==
UPDATE devices
   SET numero_interno = 'MG 7321',
       mg_estado = 'inactivo_venta',
       mg_inactivo_desde = '2026-09-15 00:00-03',
       mg_venta_fecha = '2026-09-15 00:00-03',
       mg_venta_factura_numero = NULL,
       mg_venta_remito_numero = 'RTN R 00001-00026417',
       mg_venta_observaciones = 'Regularizacion 13/10/2026: MG 7321 segun MADRE 2026 (ALTAS Y BAJAS fila 74: baja 23/09/2026 por VENTA a NOVAMED). Salio con RTN R 00001-00026417 del 15/09/2026, despues del RDA R 00004-00005200 de NATIVA del 03/09/2026.',
       mg_venta_usuario_id = NULL,
       mg_venta_customer_id = 12,
       alquilado = FALSE,
       alquiler_a = NULL
 WHERE id = 29266;

\echo == Evento de venta del MG (esperado: INSERT 0 1) ==
INSERT INTO device_mg_events(
  device_id, accion, numero_interno_snapshot, fecha_evento, factura_numero, remito_numero,
  observaciones, usuario_id, ingreso_id, venta_customer_id, source
)
SELECT 29266, 'venta', 'MG 7321', '2026-09-15 00:00-03', NULL, 'RTN R 00001-00026417',
       mg_venta_observaciones, NULL, 29704, 12, 'equipos'
  FROM devices WHERE id = 29266;

\echo == OS 29704: reparado -> vendido_entregado (esperado: UPDATE 1) ==
UPDATE ingresos
   SET estado = 'vendido_entregado',
       fecha_entrega = COALESCE(fecha_entrega, '2026-09-15 00:00-03'),
       remito_salida = COALESCE(NULLIF(remito_salida, ''), 'RTN R 00001-00026417'),
       alquilado = FALSE,
       alquiler_a = NULL,
       alquiler_remito = NULL,
       alquiler_fecha = NULL,
       ubicacion_id = (SELECT id FROM locations WHERE nombre = '-' LIMIT 1)
 WHERE id = 29704;

\echo == Historial de la OS (esperado: INSERT 0 1) ==
INSERT INTO ingreso_events (ticket_id, de_estado, a_estado, usuario_id, ts, comentario)
VALUES (29704, 'reparado', 'vendido_entregado', NULL, '2026-09-15 00:00-03',
        'Venta registrada y entrega confirmada (MG 7321 a NOVAMED, RTN R 00001-00026417)');

SELECT t.id, t.estado::text, d.numero_interno, d.mg_estado, c.razon_social AS comprador, t.remito_salida, t.fecha_entrega::date, l.nombre AS ubicacion
  FROM ingresos t JOIN devices d ON d.id = t.device_id
  LEFT JOIN customers c ON c.id = d.mg_venta_customer_id LEFT JOIN locations l ON l.id = t.ubicacion_id
 WHERE t.id = 29704;

COMMIT;
