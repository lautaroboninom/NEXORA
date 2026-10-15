-- Medicion (solo lectura) de las OS en estado 'baja' que empezarian a figurar en la cola
-- "Pendientes de Presupuesto" cuando la regla deje de excluir 'baja'.
--
-- Pedido del usuario 15/09/2026: de ahora en mas una OS dada de baja puede quedar
-- pendiente de presupuesto (la baja no salda los danos), pero las que YA estan de baja no
-- tienen que aparecer: a las que no tengan un presupuesto emitido se les pone "no aplica"
-- (presupuesto_no_aplica_bajas_aplicar_20260915.sql), asi la cola queda igual que hoy.
--
-- "Aparecerian" = mismo criterio que _SQL_PENDIENTES_PRESUPUESTO (colas_views.py) sin la
-- exclusion de 'baja': estado 'baja' y presupuesto pendiente (NULL cuenta como pendiente).
-- Para 'baja' la condicion de alquilado no juega (estado <> 'alquilado' es verdadero).
--
-- No escribe nada. Correr ANTES del script de aplicar.
--
-- Uso:  psql ... -v ON_ERROR_STOP=1 -f presupuesto_no_aplica_bajas_medir_20260915.sql

\echo == OS en baja por estado de presupuesto ==
SELECT COALESCE(t.presupuesto_estado::text, '(null)') AS presupuesto_estado, COUNT(*) AS os
  FROM ingresos t
 WHERE t.estado = 'baja'
 GROUP BY 1
 ORDER BY 2 DESC;

\echo == Las que aparecerian en la cola, por version vigente del presupuesto ==
\echo == (casos 1-3 los resuelve el script de aplicar; 4-6 no, se revisan aparte) ==
WITH cur AS (
  SELECT DISTINCT ON (q.ingreso_id)
         q.ingreso_id, q.id AS quote_id, q.estado::text AS quote_estado,
         COALESCE(q.subtotal, 0) AS subtotal,
         EXISTS (SELECT 1 FROM quote_items qi WHERE qi.quote_id = q.id) AS tiene_items
    FROM quotes q
   ORDER BY q.ingreso_id, COALESCE(q.version_num, 1) DESC, q.id DESC
),
objetivo AS (
  SELECT t.id,
         t.fecha_ingreso,
         (
           BTRIM(COALESCE(d.numero_interno,'')) ~* '^(MG|NM|NV|CE)\s*0*(\d{1,6})$'
           OR LOWER(BTRIM(COALESCE(t.motivo::text,''))) IN ('baja alquiler','reparación alquiler','reparacion alquiler')
         ) AS es_propio,
         CASE
           WHEN cur.quote_id IS NULL THEN '1 sin presupuesto -> INSERT no_aplica'
           WHEN COALESCE(cur.quote_estado,'') IN ('','pendiente')
                AND cur.subtotal = 0 AND NOT cur.tiene_items THEN '2 pendiente vacio -> UPDATE no_aplica'
           WHEN COALESCE(cur.quote_estado,'') IN ('','pendiente') THEN '3 pendiente con importe o items -> UPDATE no_aplica'
           WHEN cur.quote_estado = 'anulado' THEN '4 version vigente anulada'
           WHEN cur.quote_estado = 'no_aplica' THEN '5 quote no_aplica desincronizada'
           ELSE '6 presupuesto emitido desincronizado'
         END AS caso
    FROM ingresos t
    JOIN devices d ON d.id = t.device_id
    LEFT JOIN cur ON cur.ingreso_id = t.id
   WHERE t.estado = 'baja'
     AND COALESCE(t.presupuesto_estado, 'pendiente') = 'pendiente'
)
SELECT caso,
       COUNT(*) AS os,
       COUNT(*) FILTER (WHERE es_propio) AS propios,
       COUNT(*) FILTER (WHERE NOT es_propio) AS no_propios,
       MIN((fecha_ingreso AT TIME ZONE 'America/Argentina/Buenos_Aires')::date) AS ingreso_desde,
       MAX((fecha_ingreso AT TIME ZONE 'America/Argentina/Buenos_Aires')::date) AS ingreso_hasta
  FROM objetivo
 GROUP BY caso
 ORDER BY caso;

\echo == Detalle de los casos 3 a 6 (los que no son "sin presupuesto" ni "pendiente vacio") ==
WITH cur AS (
  SELECT DISTINCT ON (q.ingreso_id)
         q.ingreso_id, q.id AS quote_id, q.estado::text AS quote_estado,
         COALESCE(q.version_num, 1) AS version_num,
         COALESCE(q.subtotal, 0) AS subtotal,
         (SELECT COUNT(*) FROM quote_items qi WHERE qi.quote_id = q.id) AS items
    FROM quotes q
   ORDER BY q.ingreso_id, COALESCE(q.version_num, 1) DESC, q.id DESC
)
SELECT t.id AS os,
       (t.fecha_ingreso AT TIME ZONE 'America/Argentina/Buenos_Aires')::date AS fecha_ingreso,
       c.razon_social AS cliente,
       d.numero_interno,
       d.numero_serie,
       t.motivo,
       t.presupuesto_estado::text AS presupuesto_estado,
       cur.version_num,
       cur.quote_estado,
       cur.subtotal,
       cur.items
  FROM ingresos t
  JOIN devices d ON d.id = t.device_id
  LEFT JOIN customers c ON c.id = d.customer_id
  JOIN cur ON cur.ingreso_id = t.id
 WHERE t.estado = 'baja'
   AND COALESCE(t.presupuesto_estado, 'pendiente') = 'pendiente'
   AND NOT (COALESCE(cur.quote_estado,'') IN ('','pendiente') AND cur.subtotal = 0 AND cur.items = 0)
 ORDER BY cur.quote_estado, t.id
 LIMIT 100;

\echo == Bajas con presupuesto emitido esperando respuesta (hoy tampoco figuran en Presupuestados) ==
SELECT COUNT(*) AS os
  FROM ingresos t
 WHERE t.estado = 'baja'
   AND t.presupuesto_estado = 'presupuestado';

\echo == Bajas por anio de ingreso (todas, cualquier presupuesto) ==
SELECT EXTRACT(YEAR FROM (t.fecha_ingreso AT TIME ZONE 'America/Argentina/Buenos_Aires'))::int AS anio,
       COUNT(*) AS os,
       COUNT(*) FILTER (WHERE COALESCE(t.presupuesto_estado, 'pendiente') = 'pendiente') AS con_presupuesto_pendiente
  FROM ingresos t
 WHERE t.estado = 'baja'
 GROUP BY 1
 ORDER BY 1;
