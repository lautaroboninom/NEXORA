-- Medicion (solo lectura) de OS "alquilado" (equipos propios) del cliente PRAXAIR,
-- sin restriccion de anio, que hoy figuran en la cola "Pendientes de Presupuesto"
-- (mismo criterio de "propio" que se uso en la limpieza de 2025: MG-code activo, o
-- motivo de baja/reparacion de alquiler). Pedido del usuario 14/09/2026, ampliando
-- la limpieza anterior (ver presupuesto_no_aplica_ingresos_2025_*) a todo PRAXAIR.
--
-- No escribe nada. Correr ANTES de presupuesto_no_aplica_praxair_propios_aplicar_20260914.sql.
--
-- Uso:  psql ... -v ON_ERROR_STOP=1 -f presupuesto_no_aplica_praxair_propios_medir_20260914.sql

\echo == Variantes de razon_social que matchean PRAXAIR (revisar que no traiga de mas) ==
SELECT DISTINCT c.razon_social
  FROM ingresos t
  JOIN devices d ON d.id = t.device_id
  JOIN customers c ON c.id = d.customer_id
 WHERE c.razon_social ILIKE '%PRAXAIR%';

\echo == Detalle de OS objetivo ==
SELECT
  t.id AS ingreso_id,
  t.estado,
  t.resolucion::text AS resolucion,
  t.presupuesto_estado::text AS presupuesto_estado,
  (t.fecha_ingreso AT TIME ZONE 'America/Argentina/Buenos_Aires')::date AS fecha_ingreso,
  c.razon_social AS cliente,
  COALESCE(b.nombre, '') AS marca,
  COALESCE(m.nombre, '') AS modelo,
  d.numero_interno,
  d.numero_serie,
  d.mg_estado,
  t.motivo,
  cur.quote_estado,
  cur.subtotal,
  cur.tiene_items,
  (
    cur.quote_id IS NULL
    OR (COALESCE(cur.quote_estado, '') IN ('', 'pendiente') AND COALESCE(cur.subtotal, 0) = 0 AND NOT cur.tiene_items)
  ) AS sera_actualizado
FROM ingresos t
JOIN devices d ON d.id = t.device_id
JOIN customers c ON c.id = d.customer_id
LEFT JOIN marcas b ON b.id = d.marca_id
LEFT JOIN models m ON m.id = d.model_id
LEFT JOIN (
  SELECT DISTINCT ON (q.ingreso_id)
         q.ingreso_id, q.id AS quote_id, q.estado::text AS quote_estado, COALESCE(q.subtotal, 0) AS subtotal,
         EXISTS (SELECT 1 FROM quote_items qi WHERE qi.quote_id = q.id) AS tiene_items
    FROM quotes q
   ORDER BY q.ingreso_id, COALESCE(q.version_num, 1) DESC, q.id DESC
) cur ON cur.ingreso_id = t.id
WHERE c.razon_social ILIKE '%PRAXAIR%'
  AND t.estado = 'alquilado'
  AND COALESCE(t.presupuesto_estado, 'pendiente') = 'pendiente'
  AND (
    (
      BTRIM(COALESCE(d.numero_interno,'')) ~* '^(MG|NM|NV|CE)\s*0*(\d{1,6})$'
      AND LOWER(BTRIM(COALESCE(d.mg_estado,'activo'))) <> 'inactivo_venta'
    )
    OR LOWER(BTRIM(COALESCE(t.motivo::text,''))) IN ('baja alquiler','reparación alquiler','reparacion alquiler')
  )
ORDER BY fecha_ingreso, ingreso_id;

\echo == Totales ==
SELECT
  COUNT(*) AS total,
  COUNT(*) FILTER (
    WHERE cur.quote_id IS NULL
       OR (COALESCE(cur.quote_estado, '') IN ('', 'pendiente') AND COALESCE(cur.subtotal, 0) = 0 AND NOT cur.tiene_items)
  ) AS se_actualizarian,
  COUNT(*) FILTER (
    WHERE cur.quote_id IS NOT NULL
      AND COALESCE(cur.quote_estado, '') IN ('', 'pendiente')
      AND (COALESCE(cur.subtotal, 0) <> 0 OR cur.tiene_items)
  ) AS con_importe_o_items_a_revisar_a_mano,
  COUNT(*) FILTER (
    WHERE NOT (
      BTRIM(COALESCE(d.numero_interno,'')) ~* '^(MG|NM|NV|CE)\s*0*(\d{1,6})$'
      AND LOWER(BTRIM(COALESCE(d.mg_estado,'activo'))) <> 'inactivo_venta'
    )
  ) AS entraron_solo_por_motivo_no_por_codigo_mg
FROM ingresos t
JOIN devices d ON d.id = t.device_id
JOIN customers c ON c.id = d.customer_id
LEFT JOIN (
  SELECT DISTINCT ON (q.ingreso_id)
         q.ingreso_id, q.id AS quote_id, q.estado::text AS quote_estado, COALESCE(q.subtotal, 0) AS subtotal,
         EXISTS (SELECT 1 FROM quote_items qi WHERE qi.quote_id = q.id) AS tiene_items
    FROM quotes q
   ORDER BY q.ingreso_id, COALESCE(q.version_num, 1) DESC, q.id DESC
) cur ON cur.ingreso_id = t.id
WHERE c.razon_social ILIKE '%PRAXAIR%'
  AND t.estado = 'alquilado'
  AND COALESCE(t.presupuesto_estado, 'pendiente') = 'pendiente'
  AND (
    (
      BTRIM(COALESCE(d.numero_interno,'')) ~* '^(MG|NM|NV|CE)\s*0*(\d{1,6})$'
      AND LOWER(BTRIM(COALESCE(d.mg_estado,'activo'))) <> 'inactivo_venta'
    )
    OR LOWER(BTRIM(COALESCE(t.motivo::text,''))) IN ('baja alquiler','reparación alquiler','reparacion alquiler')
  );
