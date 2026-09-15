-- Aplica "no aplica" al presupuesto de las OS "alquilado" (equipos propios) con
-- fecha de ingreso 2025 que hoy figuran en la cola "Pendientes de Presupuesto"
-- (mismo criterio que PendientesPresupuestoView en ingresos_views.py, acotado por
-- el usuario a estado='alquilado' el 14/09/2026). Correr DESPUES de revisar
-- presupuesto_no_aplica_ingresos_2025_medir_20260914.sql y confirmar el alcance.
--
-- Contexto: son casos viejos que volvieron a aparecer como pendientes despues del
-- fix del trigger sync_quote_with_ingreso (commit 90428c9, ver memoria
-- nexora-presupuesto-no-aplica-lo-pisa-el-trigger). Antes ese trigger le devolvia
-- 'no_aplica' a 'pendiente' apenas se tocaba `quotes` (p.ej. al abrir la pestaña
-- Presupuesto); ahora que esta corregido, el estado real que habia quedado en
-- `quotes` (pendiente) es el que se ve, y hay que resolverlo a mano como "no aplica"
-- para las OS de 2025 de equipos propios: son alquileres viejos que no necesitan
-- presupuesto real.
--
-- Misma regla de seguridad que marcar_quote_no_aplica() en quotes_views.py: solo se
-- pisa una version PENDIENTE (o vacia) o se crea la fila si no existe ninguna. Un
-- presupuesto ya emitido/aprobado/rechazado/anulado no se toca nunca aca. Ademas, si
-- la version pendiente ya tiene importe o items cargados (un presupuesto real a medio
-- hacer), se deja afuera para revisar a mano -- ver OS 28214 ($50.000) y 28503
-- ($212.250) detectadas en la medicion del 14/09.
--
-- v2 (14/09/2026): la v1 fallaba -- un WITH ... UPDATE no comparte su CTE con la
-- sentencia INSERT siguiente (cada sentencia es su propio scope). Se corrigio
-- repitiendo el filtro en cada sentencia en vez de depender de un CTE compartido.
-- La v1 llego a correr "UPDATE 16" antes de fallar en el INSERT con "relation
-- objetivo does not exist"; como nunca llego al COMMIT y la sesion de psql termino
-- por el error (ON_ERROR_STOP=1, exit 3), Postgres deberia haber revertido esas 16
-- filas solo al cerrarse la conexion sin commit -- confirmar con la consulta de
-- verificacion de abajo antes de asumirlo.
--
-- Uso:  psql ... -v ON_ERROR_STOP=1 -f presupuesto_no_aplica_ingresos_2025_aplicar_20260914.sql

BEGIN;

-- 1) Ya existe una version de presupuesto pendiente (o con estado vacio) para esa OS:
-- se pisa a no_aplica. Sin CTE compartido: JOIN directo contra la version vigente.
UPDATE quotes q
   SET estado = 'no_aplica'
  FROM (
    SELECT DISTINCT ON (ingreso_id) ingreso_id, id AS quote_id, estado::text AS quote_estado
      FROM quotes
     ORDER BY ingreso_id, COALESCE(version_num, 1) DESC, id DESC
  ) cur
  JOIN ingresos t ON t.id = cur.ingreso_id
  JOIN devices d ON d.id = t.device_id
 WHERE q.id = cur.quote_id
   AND COALESCE(cur.quote_estado, '') IN ('', 'pendiente')
   AND COALESCE(q.subtotal, 0) = 0
   AND NOT EXISTS (SELECT 1 FROM quote_items qi WHERE qi.quote_id = q.id)
   AND t.estado = 'alquilado'
   AND COALESCE(t.presupuesto_estado, 'pendiente') = 'pendiente'
   AND EXTRACT(YEAR FROM (t.fecha_ingreso AT TIME ZONE 'America/Argentina/Buenos_Aires')) = 2025
   AND (
     (
       BTRIM(COALESCE(d.numero_interno,'')) ~* '^(MG|NM|NV|CE)\s*0*(\d{1,6})$'
       AND LOWER(BTRIM(COALESCE(d.mg_estado,'activo'))) <> 'inactivo_venta'
     )
     OR LOWER(BTRIM(COALESCE(t.motivo::text,''))) IN ('baja alquiler','reparación alquiler','reparacion alquiler')
   );

-- 2) El ingreso todavia no tiene ninguna fila en quotes: nace directamente en no_aplica.
INSERT INTO quotes(ingreso_id, version_num, estado)
SELECT t.id, 1, 'no_aplica'
  FROM ingresos t
  JOIN devices d ON d.id = t.device_id
 WHERE t.estado = 'alquilado'
   AND COALESCE(t.presupuesto_estado, 'pendiente') = 'pendiente'
   AND EXTRACT(YEAR FROM (t.fecha_ingreso AT TIME ZONE 'America/Argentina/Buenos_Aires')) = 2025
   AND (
     (
       BTRIM(COALESCE(d.numero_interno,'')) ~* '^(MG|NM|NV|CE)\s*0*(\d{1,6})$'
       AND LOWER(BTRIM(COALESCE(d.mg_estado,'activo'))) <> 'inactivo_venta'
     )
     OR LOWER(BTRIM(COALESCE(t.motivo::text,''))) IN ('baja alquiler','reparación alquiler','reparacion alquiler')
   )
   AND NOT EXISTS (SELECT 1 FROM quotes q2 WHERE q2.ingreso_id = t.id);

\echo == Lo que queda pendiente despues de correr esto ==
\echo == (esperado: solo OS 28214 y 28503, que tienen presupuesto con importe cargado -- revisar a mano) ==
SELECT t.id AS os, t.estado, t.presupuesto_estado::text AS presupuesto_estado, cur.quote_estado, cur.subtotal
  FROM ingresos t
  JOIN devices d ON d.id = t.device_id
  LEFT JOIN (
    SELECT DISTINCT ON (ingreso_id) ingreso_id, id AS quote_id, estado::text AS quote_estado, COALESCE(subtotal, 0) AS subtotal
      FROM quotes
     ORDER BY ingreso_id, COALESCE(version_num, 1) DESC, id DESC
  ) cur ON cur.ingreso_id = t.id
 WHERE t.estado = 'alquilado'
   AND COALESCE(t.presupuesto_estado, 'pendiente') = 'pendiente'
   AND EXTRACT(YEAR FROM (t.fecha_ingreso AT TIME ZONE 'America/Argentina/Buenos_Aires')) = 2025
   AND (
     (
       BTRIM(COALESCE(d.numero_interno,'')) ~* '^(MG|NM|NV|CE)\s*0*(\d{1,6})$'
       AND LOWER(BTRIM(COALESCE(d.mg_estado,'activo'))) <> 'inactivo_venta'
     )
     OR LOWER(BTRIM(COALESCE(t.motivo::text,''))) IN ('baja alquiler','reparación alquiler','reparacion alquiler')
   )
 ORDER BY t.id;

-- psql ya muestra "UPDATE n" e "INSERT 0 n" para cada sentencia de arriba con la
-- cantidad de filas tocadas: no hace falta un conteo aparte.

COMMIT;
