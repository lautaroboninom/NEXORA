-- Órdenes de entrega de servicio técnico importadas del portal viejo que siguen pendientes con
-- prioridad "urgente" (13/10/2026). Recepción reportó que las ventas nuevas quedaban al final de
-- Órdenes de entrega: arriba de todo había un bloque de 57 órdenes de junio marcadas urgentes.
--
-- Origen: el portal viejo marcaba "urgente" casi todas sus liberaciones de servicio técnico (64 de
-- 93); NEXORA las crea siempre "normal". La importación del 01/06 al 10/06 copió la marca tal cual
-- y esas órdenes no se tocaron más. Dos de ellas (OS-29842 de NOVAMED y OS-29773 de TMD) además
-- absorbieron liberaciones nuevas de septiembre y octubre por la agrupación por cliente, que las
-- tomaba por ser la orden abierta más vieja (corregido en el código el mismo día).
--
-- Se corre DESPUÉS de ordenes_entrega_portal_os_no_liberadas_borrar_20261014.sql, que sacó las 25
-- cuya OS ya no estaba liberada. Quedan 32, todas con la OS liberada, y el usuario pidió (14/10)
-- sacarles la urgencia automática: en servicio técnico la urgencia se marca a mano.
--
-- Efecto: la prioridad pasa a 'normal' y cada orden queda ordenada por su fecha, intercalada con
-- las ventas. No cambia el estado, los renglones ni nada de Bejerman. Queda un evento por orden.
--
-- Para deshacer: volver a 'urgente' las órdenes con el evento
-- 'delivery_order_priority_normalized_portal' de esta corrida.
--
-- Uso:  psql ... -v ON_ERROR_STOP=1 -f ordenes_entrega_portal_urgentes_a_normal_aplicar_20261013.sql

BEGIN;

CREATE TEMP TABLE portal_urgente ON COMMIT DROP AS
SELECT o.id, o.order_number
  FROM delivery_orders o
 WHERE o.source_system = 'portal'
   AND o.delivery_type = 'service_release'
   AND o.priority = 'urgente'
   AND o.status IN ('pendiente_stock', 'pendiente_armado', 'armado_pendiente_entrega');

-- Leído en producción el 14/10 después del borrado: 32. Si hay otra cantidad, algo cambió: se aborta.
DO $$
DECLARE
  total integer;
BEGIN
  SELECT COUNT(*) INTO total FROM portal_urgente;
  RAISE NOTICE 'Órdenes del portal urgentes y pendientes: %', total;
  IF total <> 32 THEN
    RAISE EXCEPTION 'Se esperaban 32 órdenes y hay %: revisar antes de aplicar', total;
  END IF;
END $$;

INSERT INTO delivery_order_events (order_id, actor_user_id, source_actor_user_id, event_type, note, metadata)
SELECT p.id,
       NULL,
       NULL,
       'delivery_order_priority_normalized_portal',
       'Prioridad urgente heredada del portal viejo pasada a normal (13/10/2026).',
       jsonb_build_object('from', 'urgente', 'to', 'normal')
  FROM portal_urgente p;

UPDATE delivery_orders o
   SET priority = 'normal',
       updated_at = CURRENT_TIMESTAMP
  FROM portal_urgente p
 WHERE o.id = p.id;

SELECT COUNT(*) AS quedan_urgentes_del_portal_pendientes
  FROM delivery_orders o
 WHERE o.source_system = 'portal'
   AND o.delivery_type = 'service_release'
   AND o.priority = 'urgente'
   AND o.status IN ('pendiente_stock', 'pendiente_armado', 'armado_pendiente_entrega');

COMMIT;
