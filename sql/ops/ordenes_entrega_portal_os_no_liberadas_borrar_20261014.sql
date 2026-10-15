-- Órdenes de entrega de servicio técnico importadas del portal viejo (junio 2026) cuya OS ya no
-- está liberada. Pedido del usuario 14/10/2026: "las 25 OS que ya no están liberadas sacalas
-- directamente, borralas".
--
-- Alcance (leído en producción el 14/10/2026): 25 órdenes urgentes y pendientes del portal cuya OS
-- (el número de source_reference) está entregada (12), reparada (8), alquilada (3), de baja (1)
-- o diagnosticada (1).
--   * 24 no tienen remito, ni vínculos de remito, ni paradas de hoja de ruta: se BORRAN. Sus
--     renglones y eventos se van por ON DELETE CASCADE. Copia completa antes de borrar:
--     docs/reportes/ordenes_entrega_portal_borradas_20261014.json
--   * OES-20260602-F824E9A3 (ARGENTINA DE GASES, OS 29253) tiene el remito RSS R 00004-00004924
--     emitido en Bejerman. No se borra para no perder el vínculo con ese comprobante: se cierra
--     como entregada no facturable (RSS no se factura), con la fecha de entrega de la OS.
--
-- No toca las OS ni Bejerman.
--
-- Uso:  psql ... -v ON_ERROR_STOP=1 -f ordenes_entrega_portal_os_no_liberadas_borrar_20261014.sql

BEGIN;

CREATE TEMP TABLE portal_borrar ON COMMIT DROP AS
SELECT o.id, o.order_number
  FROM delivery_orders o
  JOIN ingresos i
    ON i.id = NULLIF(regexp_replace(COALESCE(o.source_reference, ''), '[^0-9]', '', 'g'), '')::integer
 WHERE o.source_system = 'portal'
   AND o.delivery_type = 'service_release'
   AND o.priority = 'urgente'
   AND o.status IN ('pendiente_stock', 'pendiente_armado', 'armado_pendiente_entrega')
   AND o.ingreso_id IS NULL
   AND i.estado <> 'liberado'
   AND NULLIF(BTRIM(COALESCE(o.remito_number, '')), '') IS NULL
   AND o.bejerman_remito_group_id IS NULL
   AND NOT EXISTS (SELECT 1 FROM delivery_order_items doi WHERE doi.order_id = o.id AND doi.ingreso_id IS NOT NULL)
   AND NOT EXISTS (SELECT 1 FROM delivery_order_remito_links rl WHERE rl.order_id = o.id)
   AND NOT EXISTS (SELECT 1 FROM route_stops s WHERE s.delivery_order_id = o.id);

DO $$
DECLARE
  total integer;
BEGIN
  SELECT COUNT(*) INTO total FROM portal_borrar;
  RAISE NOTICE 'Órdenes del portal a borrar: %', total;
  IF total <> 24 THEN
    RAISE EXCEPTION 'Se esperaban 24 órdenes a borrar y hay %: revisar antes de aplicar', total;
  END IF;
END $$;

-- La del remito RSS se cierra; sólo si sigue exactamente como se leyó.
WITH cerrada AS (
  UPDATE delivery_orders o
     SET status = 'entregado_no_facturable',
         priority = 'normal',
         prepared_at = COALESCE(o.prepared_at, i.fecha_entrega::timestamptz),
         delivered_at = COALESCE(o.delivered_at, i.fecha_entrega::timestamptz),
         updated_at = CURRENT_TIMESTAMP
    FROM ingresos i
   WHERE o.order_number = 'OES-20260602-F824E9A3'
     AND o.source_system = 'portal'
     AND o.status = 'armado_pendiente_entrega'
     AND o.remito_number = 'RSS R 00004-00004924'
     AND i.id = 29253
     AND i.estado = 'entregado'
  RETURNING o.id
)
INSERT INTO delivery_order_events (order_id, actor_user_id, source_actor_user_id, event_type, note, metadata)
SELECT c.id,
       NULL,
       NULL,
       'delivery_order_closed_legacy_portal',
       'Orden del portal viejo cerrada: la OS 29253 ya figuraba entregada con el RSS R 00004-00004924 (14/10/2026).',
       jsonb_build_object('remitoNumber', 'RSS R 00004-00004924', 'ingresoId', 29253)
  FROM cerrada c;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM delivery_orders
     WHERE order_number = 'OES-20260602-F824E9A3' AND status = 'entregado_no_facturable'
  ) THEN
    RAISE EXCEPTION 'No se pudo cerrar OES-20260602-F824E9A3: revisar antes de aplicar';
  END IF;
END $$;

DELETE FROM delivery_orders o
 USING portal_borrar p
 WHERE o.id = p.id;

SELECT COUNT(*) AS quedan_urgentes_del_portal_pendientes
  FROM delivery_orders o
 WHERE o.source_system = 'portal'
   AND o.delivery_type = 'service_release'
   AND o.priority = 'urgente'
   AND o.status IN ('pendiente_stock', 'pendiente_armado', 'armado_pendiente_entrega');

COMMIT;
