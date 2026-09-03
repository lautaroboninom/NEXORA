-- Regulariza en NEXORA el RSS R 00001-00026328, ya corregido y verificado en Bejerman.
-- No emite comprobantes ni genera movimientos de stock.
--
-- Vista previa transaccional:
--   psql -v ON_ERROR_STOP=1 -v apply=0 -f sql/ops/regularizar_rss_00001_00026328_nexora.sql
-- Aplicación:
--   psql -v ON_ERROR_STOP=1 -v apply=1 -f sql/ops/regularizar_rss_00001_00026328_nexora.sql

\if :{?apply}
\else
  \set apply 0
\endif

\set ON_ERROR_STOP on
\pset format unaligned
\pset fieldsep '|'

BEGIN;

DO $guardas$
DECLARE
  v_count INTEGER;
BEGIN
  PERFORM id
    FROM delivery_orders
   WHERE id IN (
     'do-26d7ebc5-cd36-4fad-9d45-317fff5b4b26',
     'do-fef3c105-62ac-418b-ae49-c4ba965037d5'
   )
   FOR UPDATE;

  SELECT COUNT(*) INTO v_count
    FROM delivery_orders
   WHERE id IN (
     'do-26d7ebc5-cd36-4fad-9d45-317fff5b4b26',
     'do-fef3c105-62ac-418b-ae49-c4ba965037d5'
   )
     AND delivery_type = 'service_release'
     AND company_key = 'SEPID'
     AND status IN ('armado_pendiente_entrega', 'entregado_no_facturable');
  IF v_count <> 2 THEN
    RAISE EXCEPTION 'Guarda fallida: no están las dos órdenes de servicio en el estado esperado';
  END IF;

  PERFORM id
    FROM bejerman_remito_groups
   WHERE id = 'brg-0dcf5162-b5ce-4ee1-b6b8-b04561e04571'
   FOR UPDATE;

  SELECT COUNT(*) INTO v_count
    FROM bejerman_remito_groups
   WHERE id = 'brg-0dcf5162-b5ce-4ee1-b6b8-b04561e04571'
     AND company_key = 'SEPID'
     AND comprobante_tipo = 'RSS'
     AND comprobante_letra = 'R'
     AND NULLIF(regexp_replace(comprobante_pto_venta, '[^0-9]', '', 'g'), '')::BIGINT = 1
     AND NULLIF(regexp_replace(comprobante_numero, '[^0-9]', '', 'g'), '')::BIGINT = 26328
     AND remito_number = 'RSS R 00001-00026328'
     AND status = 'generated'
     AND deposit_code = 'STC';
  IF v_count <> 1 THEN
    RAISE EXCEPTION 'Guarda fallida: el grupo del RSS no coincide con el comprobante verificado';
  END IF;

  PERFORM id
    FROM ingresos
   WHERE id IN (29369, 29413, 29437, 29496)
   FOR UPDATE;

  SELECT COUNT(*) INTO v_count
    FROM delivery_order_items
   WHERE (id, order_id, ingreso_id, article_code) IN (
     ('doi-ace9caf0-1a0d-4f9e-886f-a8e0a44f28d5', 'do-26d7ebc5-cd36-4fad-9d45-317fff5b4b26', 29413, '1103002'),
     ('doi-408aebf6-847f-4472-8acf-ce4ceda3cf5c', 'do-26d7ebc5-cd36-4fad-9d45-317fff5b4b26', 29496, '1107006'),
     ('doi-58975813-ef1c-4e4c-a5a2-fe016666c315', 'do-fef3c105-62ac-418b-ae49-c4ba965037d5', 29437, '1107006'),
     ('doi-b71a0d9b-5c86-467f-bd45-052cdb05de97', 'do-fef3c105-62ac-418b-ae49-c4ba965037d5', 29369, '1107006')
   )
     AND quantity = 1;
  IF v_count <> 4 THEN
    RAISE EXCEPTION 'Guarda fallida: los cuatro renglones de las órdenes no coinciden';
  END IF;

  SELECT COUNT(*) INTO v_count
    FROM delivery_order_item_partidas
   WHERE (id, order_item_id, ingreso_id, partida, assigned_quantity) IN (
     ('doip-04f766e9-2018-4646-a4fb-534f95abf3d1', 'doi-ace9caf0-1a0d-4f9e-886f-a8e0a44f28d5', 29413, 'ES119301028', 1),
     ('doip-a26ef944-cff5-48ac-85d8-dfbc5da35cd8', 'doi-408aebf6-847f-4472-8acf-ce4ceda3cf5c', 29496, '40966K0796', 1)
   )
     AND stock_deposit_code IN ('VAL', 'STC');
  IF v_count <> 2 THEN
    RAISE EXCEPTION 'Guarda fallida: las partidas de OS-29413 no coinciden';
  END IF;

  SELECT COUNT(*) INTO v_count
    FROM ingresos
   WHERE id IN (29369, 29413, 29437, 29496)
     AND estado = 'entregado'
     AND fecha_entrega IS NOT NULL
     AND remito_salida IN (
       id::TEXT,
       'RSS R 00001-00026328'
     );
  IF v_count <> 4 THEN
    RAISE EXCEPTION 'Guarda fallida: los cuatro ingresos no coinciden con la entrega histórica';
  END IF;

  SELECT COUNT(*) INTO v_count
    FROM delivery_order_remito_links
   WHERE order_id = 'do-26d7ebc5-cd36-4fad-9d45-317fff5b4b26'
     AND remito_group_id <> 'brg-0dcf5162-b5ce-4ee1-b6b8-b04561e04571';
  IF v_count <> 0 THEN
    RAISE EXCEPTION 'Guarda fallida: OS-29413 ya tiene otro vínculo de remito';
  END IF;

  SELECT COUNT(*) INTO v_count
    FROM delivery_order_remito_links l
    JOIN delivery_order_remito_item_allocations a ON a.link_id = l.id
   WHERE l.order_id = 'do-fef3c105-62ac-418b-ae49-c4ba965037d5'
     AND l.remito_group_id = 'brg-0dcf5162-b5ce-4ee1-b6b8-b04561e04571'
     AND l.allocation_status = 'known';
  IF v_count <> 2 THEN
    RAISE EXCEPTION 'Guarda fallida: el vínculo original de OS-29437 no conserva sus dos asignaciones';
  END IF;
END
$guardas$;

UPDATE delivery_order_items
   SET stock_deposit_code = 'STC'
 WHERE id IN (
   'doi-ace9caf0-1a0d-4f9e-886f-a8e0a44f28d5',
   'doi-408aebf6-847f-4472-8acf-ce4ceda3cf5c',
   'doi-58975813-ef1c-4e4c-a5a2-fe016666c315',
   'doi-b71a0d9b-5c86-467f-bd45-052cdb05de97'
 );

UPDATE delivery_order_item_partidas
   SET stock_deposit_code = 'STC'
 WHERE id IN (
   'doip-04f766e9-2018-4646-a4fb-534f95abf3d1',
   'doip-a26ef944-cff5-48ac-85d8-dfbc5da35cd8'
 );

UPDATE bejerman_remito_groups
   SET order_ids = '["do-fef3c105-62ac-418b-ae49-c4ba965037d5", "do-26d7ebc5-cd36-4fad-9d45-317fff5b4b26"]'::JSONB,
       response_summary = response_summary || jsonb_build_object(
         'reusedAcrossOrders', TRUE,
         'verifiedInBejerman', TRUE,
         'verifiedEquipmentCount', 4,
         'verifiedLineCount', 8,
         'regularizationId', 'rss-00001-00026328-20260903'
       )
 WHERE id = 'brg-0dcf5162-b5ce-4ee1-b6b8-b04561e04571';

INSERT INTO delivery_order_remito_links (
  id, order_id, remito_group_id, batch_id, sequence,
  expected_count, allocation_status
)
VALUES (
  'dorl-regularizacion-rss-00001-00026328-os-29413',
  'do-26d7ebc5-cd36-4fad-9d45-317fff5b4b26',
  'brg-0dcf5162-b5ce-4ee1-b6b8-b04561e04571',
  'dorb-regularizacion-rss-00001-00026328-os-29413',
  1, 1, 'known'
)
ON CONFLICT (order_id, remito_group_id) DO UPDATE
   SET batch_id = EXCLUDED.batch_id,
       sequence = EXCLUDED.sequence,
       expected_count = EXCLUDED.expected_count,
       allocation_status = EXCLUDED.allocation_status,
       updated_at = CURRENT_TIMESTAMP;

DELETE FROM delivery_order_remito_item_allocations
 WHERE link_id = (
   SELECT id
     FROM delivery_order_remito_links
    WHERE order_id = 'do-26d7ebc5-cd36-4fad-9d45-317fff5b4b26'
      AND remito_group_id = 'brg-0dcf5162-b5ce-4ee1-b6b8-b04561e04571'
 );

INSERT INTO delivery_order_remito_item_allocations (
  id, link_id, order_item_id, order_item_partida_id, assigned_quantity, sort_order
)
SELECT
  datos.id,
  l.id,
  datos.order_item_id,
  datos.order_item_partida_id,
  1,
  datos.sort_order
FROM delivery_order_remito_links l
CROSS JOIN (
  VALUES
    ('doria-regularizacion-rss-00001-00026328-os-29413',
     'doi-ace9caf0-1a0d-4f9e-886f-a8e0a44f28d5',
     'doip-04f766e9-2018-4646-a4fb-534f95abf3d1', 0),
    ('doria-regularizacion-rss-00001-00026328-os-29496',
     'doi-408aebf6-847f-4472-8acf-ce4ceda3cf5c',
     'doip-a26ef944-cff5-48ac-85d8-dfbc5da35cd8', 1)
) AS datos(id, order_item_id, order_item_partida_id, sort_order)
WHERE l.order_id = 'do-26d7ebc5-cd36-4fad-9d45-317fff5b4b26'
  AND l.remito_group_id = 'brg-0dcf5162-b5ce-4ee1-b6b8-b04561e04571';

UPDATE delivery_orders
   SET status = 'entregado_no_facturable',
       remito_number = 'RSS R 00001-00026328',
       bejerman_remito_group_id = 'brg-0dcf5162-b5ce-4ee1-b6b8-b04561e04571',
       remito_location = COALESCE(remito_location, 'recepcion'),
       remito_location_updated_by = COALESCE(remito_location_updated_by, 1152),
       remito_location_updated_at = COALESCE(remito_location_updated_at, CURRENT_TIMESTAMP),
       prepared_by_user_id = COALESCE(prepared_by_user_id, 1152),
       delivered_by_user_id = COALESCE(delivered_by_user_id, 1152),
       prepared_at = COALESCE(prepared_at, CURRENT_TIMESTAMP),
       delivered_at = COALESCE(delivered_at, CURRENT_TIMESTAMP)
 WHERE id IN (
   'do-26d7ebc5-cd36-4fad-9d45-317fff5b4b26',
   'do-fef3c105-62ac-418b-ae49-c4ba965037d5'
 );

UPDATE ingresos
   SET remito_salida = 'RSS R 00001-00026328'
 WHERE id IN (29369, 29413, 29437, 29496);

INSERT INTO delivery_order_events (order_id, actor_user_id, event_type, note, metadata)
SELECT
  'do-26d7ebc5-cd36-4fad-9d45-317fff5b4b26',
  1152,
  'bejerman_remito_associated',
  'Asociación regularizada después de completar y verificar el RSS en Bejerman.',
  jsonb_build_object(
    'groupId', 'brg-0dcf5162-b5ce-4ee1-b6b8-b04561e04571',
    'remitoNumber', 'RSS R 00001-00026328',
    'documentMode', 'register',
    'stockMovementGenerated', FALSE,
    'reusedAcrossOrders', TRUE,
    'verifiedInBejerman', TRUE,
    'regularizationId', 'rss-00001-00026328-20260903'
  )
WHERE NOT EXISTS (
  SELECT 1 FROM delivery_order_events
   WHERE order_id = 'do-26d7ebc5-cd36-4fad-9d45-317fff5b4b26'
     AND event_type = 'bejerman_remito_associated'
     AND metadata->>'regularizationId' = 'rss-00001-00026328-20260903'
);

INSERT INTO delivery_order_events (order_id, actor_user_id, event_type, note, metadata)
SELECT
  'do-26d7ebc5-cd36-4fad-9d45-317fff5b4b26',
  1152,
  'delivery_order_prepared',
  'Preparación reconstruida al vincular el RSS único de las cuatro hojas de servicio.',
  jsonb_build_object(
    'status', 'armado_pendiente_entrega',
    'remitoNumbers', jsonb_build_array('RSS R 00001-00026328'),
    'remitoSetStatus', 'complete',
    'regularizationId', 'rss-00001-00026328-20260903'
  )
WHERE NOT EXISTS (
  SELECT 1 FROM delivery_order_events
   WHERE order_id = 'do-26d7ebc5-cd36-4fad-9d45-317fff5b4b26'
     AND event_type = 'delivery_order_prepared'
     AND metadata->>'regularizationId' = 'rss-00001-00026328-20260903'
);

INSERT INTO delivery_order_events (order_id, actor_user_id, event_type, note, metadata)
SELECT
  o.id,
  1152,
  'delivery_order_delivered',
  'Entrega regularizada a partir de las hojas de servicio ya entregadas.',
  jsonb_build_object(
    'remitoNumber', 'RSS R 00001-00026328',
    'regularizationId', 'rss-00001-00026328-20260903',
    'previousStatus', 'armado_pendiente_entrega'
  )
FROM delivery_orders o
WHERE o.id IN (
  'do-26d7ebc5-cd36-4fad-9d45-317fff5b4b26',
  'do-fef3c105-62ac-418b-ae49-c4ba965037d5'
)
AND NOT EXISTS (
  SELECT 1 FROM delivery_order_events e
   WHERE e.order_id = o.id
     AND e.event_type = 'delivery_order_delivered'
     AND e.metadata->>'regularizationId' = 'rss-00001-00026328-20260903'
);

INSERT INTO bejerman_integration_attempts (
  group_id, order_ids, actor_user_id, phase, status, attempt_number,
  request_payload, response_payload, recovery_payload
)
SELECT
  'brg-0dcf5162-b5ce-4ee1-b6b8-b04561e04571',
  '["do-26d7ebc5-cd36-4fad-9d45-317fff5b4b26"]'::JSONB,
  1152,
  'manual_associate',
  'reconciled',
  1,
  '{}'::JSONB,
  jsonb_build_object(
    'remitoNumber', 'RSS R 00001-00026328',
    'documentMode', 'register',
    'stockMovementGenerated', FALSE,
    'reusedAcrossOrders', TRUE,
    'verifiedInBejerman', TRUE,
    'regularizationId', 'rss-00001-00026328-20260903'
  ),
  '{}'::JSONB
WHERE NOT EXISTS (
  SELECT 1 FROM bejerman_integration_attempts
   WHERE group_id = 'brg-0dcf5162-b5ce-4ee1-b6b8-b04561e04571'
     AND phase = 'manual_associate'
     AND response_payload->>'regularizationId' = 'rss-00001-00026328-20260903'
);

DO $verificacion$
DECLARE
  v_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO v_count
    FROM delivery_orders
   WHERE id IN (
     'do-26d7ebc5-cd36-4fad-9d45-317fff5b4b26',
     'do-fef3c105-62ac-418b-ae49-c4ba965037d5'
   )
     AND status = 'entregado_no_facturable'
     AND remito_number = 'RSS R 00001-00026328'
     AND bejerman_remito_group_id = 'brg-0dcf5162-b5ce-4ee1-b6b8-b04561e04571'
     AND delivered_at IS NOT NULL;
  IF v_count <> 2 THEN
    RAISE EXCEPTION 'Verificación fallida: las órdenes no quedaron cerradas y vinculadas';
  END IF;

  SELECT COUNT(*) INTO v_count
    FROM delivery_order_remito_links l
    JOIN delivery_order_remito_item_allocations a ON a.link_id = l.id
   WHERE l.order_id IN (
     'do-26d7ebc5-cd36-4fad-9d45-317fff5b4b26',
     'do-fef3c105-62ac-418b-ae49-c4ba965037d5'
   )
     AND l.remito_group_id = 'brg-0dcf5162-b5ce-4ee1-b6b8-b04561e04571'
     AND l.allocation_status = 'known';
  IF v_count <> 4 THEN
    RAISE EXCEPTION 'Verificación fallida: el grupo no tiene las cuatro asignaciones';
  END IF;

  SELECT COUNT(*) INTO v_count
    FROM delivery_order_items
   WHERE id IN (
     'doi-ace9caf0-1a0d-4f9e-886f-a8e0a44f28d5',
     'doi-408aebf6-847f-4472-8acf-ce4ceda3cf5c',
     'doi-58975813-ef1c-4e4c-a5a2-fe016666c315',
     'doi-b71a0d9b-5c86-467f-bd45-052cdb05de97'
   )
     AND stock_deposit_code = 'STC';
  IF v_count <> 4 THEN
    RAISE EXCEPTION 'Verificación fallida: los cuatro renglones no quedaron en STC';
  END IF;

  SELECT COUNT(*) INTO v_count
    FROM ingresos
   WHERE id IN (29369, 29413, 29437, 29496)
     AND estado = 'entregado'
     AND remito_salida = 'RSS R 00001-00026328'
     AND fecha_entrega IS NOT NULL;
  IF v_count <> 4 THEN
    RAISE EXCEPTION 'Verificación fallida: los cuatro ingresos no quedaron sincronizados';
  END IF;
END
$verificacion$;

SELECT 'ORDEN_FINAL', id, order_number, status, remito_number,
       bejerman_remito_group_id, delivered_at
  FROM delivery_orders
 WHERE id IN (
   'do-26d7ebc5-cd36-4fad-9d45-317fff5b4b26',
   'do-fef3c105-62ac-418b-ae49-c4ba965037d5'
 )
 ORDER BY id;

SELECT 'GRUPO_FINAL', g.id, g.remito_number, g.status, g.order_ids::TEXT,
       COUNT(DISTINCT l.order_id) AS ordenes,
       COUNT(a.id) AS asignaciones
  FROM bejerman_remito_groups g
  JOIN delivery_order_remito_links l ON l.remito_group_id = g.id
  JOIN delivery_order_remito_item_allocations a ON a.link_id = l.id
 WHERE g.id = 'brg-0dcf5162-b5ce-4ee1-b6b8-b04561e04571'
 GROUP BY g.id, g.remito_number, g.status, g.order_ids;

SELECT 'INGRESO_FINAL', id, estado, remito_salida, fecha_entrega
  FROM ingresos
 WHERE id IN (29369, 29413, 29437, 29496)
 ORDER BY id;

\if :apply
  COMMIT;
  \echo 'REGULARIZACION_APLICADA'
\else
  ROLLBACK;
  \echo 'VISTA_PREVIA_OK_SIN_CAMBIOS'
\endif
