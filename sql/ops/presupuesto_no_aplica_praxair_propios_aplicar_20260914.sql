-- Aplica "no aplica" al presupuesto de las OS "alquilado" (equipos propios) del
-- cliente PRAXAIR ARGENTINA S.R.L., sin restriccion de anio (pedido del usuario
-- 14/09/2026, ampliando presupuesto_no_aplica_ingresos_2025_* a todo PRAXAIR).
-- Correr DESPUES de revisar presupuesto_no_aplica_praxair_propios_medir_20260914.sql.
--
-- La medicion dio 132 OS (2017-2024 la mayoria, mas 3 de 2026 -- dos recientes y una
-- con fecha 11/12/2026 que el usuario confirmo que es un typo de 2025/2025, no una
-- fecha real futura; el usuario pidio incluir las 3 igual). 0 tenian importe o items
-- cargados, asi que no se espera que el resguardo dejafuera nada -- se deja de todos
-- modos por consistencia con el script anterior.
--
-- Mismo patron v2 (sin CTE compartido entre sentencias, ver
-- presupuesto_no_aplica_ingresos_2025_aplicar_20260914.sql) y misma regla de
-- marcar_quote_no_aplica(): solo pisa version pendiente/vacia sin importe ni items.
--
-- Uso:  psql ... -v ON_ERROR_STOP=1 -f presupuesto_no_aplica_praxair_propios_aplicar_20260914.sql

BEGIN;

UPDATE quotes q
   SET estado = 'no_aplica'
  FROM (
    SELECT DISTINCT ON (ingreso_id) ingreso_id, id AS quote_id, estado::text AS quote_estado
      FROM quotes
     ORDER BY ingreso_id, COALESCE(version_num, 1) DESC, id DESC
  ) cur
  JOIN ingresos t ON t.id = cur.ingreso_id
  JOIN devices d ON d.id = t.device_id
  JOIN customers c ON c.id = d.customer_id
 WHERE q.id = cur.quote_id
   AND COALESCE(cur.quote_estado, '') IN ('', 'pendiente')
   AND COALESCE(q.subtotal, 0) = 0
   AND NOT EXISTS (SELECT 1 FROM quote_items qi WHERE qi.quote_id = q.id)
   AND c.razon_social ILIKE '%PRAXAIR%'
   AND t.estado = 'alquilado'
   AND COALESCE(t.presupuesto_estado, 'pendiente') = 'pendiente'
   AND (
     (
       BTRIM(COALESCE(d.numero_interno,'')) ~* '^(MG|NM|NV|CE)\s*0*(\d{1,6})$'
       AND LOWER(BTRIM(COALESCE(d.mg_estado,'activo'))) <> 'inactivo_venta'
     )
     OR LOWER(BTRIM(COALESCE(t.motivo::text,''))) IN ('baja alquiler','reparación alquiler','reparacion alquiler')
   );

INSERT INTO quotes(ingreso_id, version_num, estado)
SELECT t.id, 1, 'no_aplica'
  FROM ingresos t
  JOIN devices d ON d.id = t.device_id
  JOIN customers c ON c.id = d.customer_id
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
   AND NOT EXISTS (SELECT 1 FROM quotes q2 WHERE q2.ingreso_id = t.id);

\echo == Lo que queda pendiente de PRAXAIR alquilado despues de correr esto (esperado: 0 filas) ==
SELECT t.id AS os, t.estado, t.presupuesto_estado::text AS presupuesto_estado, cur.quote_estado, cur.subtotal
  FROM ingresos t
  JOIN devices d ON d.id = t.device_id
  JOIN customers c ON c.id = d.customer_id
  LEFT JOIN (
    SELECT DISTINCT ON (ingreso_id) ingreso_id, id AS quote_id, estado::text AS quote_estado, COALESCE(subtotal, 0) AS subtotal
      FROM quotes
     ORDER BY ingreso_id, COALESCE(version_num, 1) DESC, id DESC
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
 ORDER BY t.id;

COMMIT;
