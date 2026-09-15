-- Elimina exclusivamente liberaciones históricas importadas del Portal que se
-- muestran como REF-<referencia>: no tienen OS/ingreso asociado ni documento.
-- Uso exclusivo en desarrollo local.
\set ON_ERROR_STOP on

BEGIN ISOLATION LEVEL SERIALIZABLE;

CREATE TEMPORARY TABLE legacy_ref_delivery_orders_to_delete ON COMMIT DROP AS
SELECT o.id, o.order_number, o.source_reference
  FROM delivery_orders o
 WHERE o.delivery_type = 'service_release'
   AND o.source_system = 'portal'
   AND o.status IN ('pendiente_stock', 'pendiente_armado', 'armado_pendiente_entrega')
   AND o.ingreso_id IS NULL
   AND COALESCE(BTRIM(o.remito_number), '') = ''
   AND COALESCE(BTRIM(o.invoice_number), '') = ''
   AND o.bejerman_remito_group_id IS NULL
   AND NOT EXISTS (
     SELECT 1
       FROM delivery_order_items oi
      WHERE oi.order_id = o.id
        AND oi.ingreso_id IS NOT NULL
   )
 FOR UPDATE OF o;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
      FROM delivery_order_remito_links rl
      JOIN legacy_ref_delivery_orders_to_delete target ON target.id = rl.order_id
  ) THEN
    RAISE EXCEPTION 'La eliminación se canceló: hay órdenes REF con vínculos de remito.';
  END IF;
END;
$$;

WITH deleted AS (
  DELETE FROM delivery_orders o
   USING legacy_ref_delivery_orders_to_delete target
   WHERE o.id = target.id
   RETURNING o.id
)
SELECT COUNT(*) AS deleted_orders
  FROM deleted;

COMMIT;
