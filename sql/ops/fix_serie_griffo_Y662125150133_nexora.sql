-- Corrige la serie mal tipeada de las OS 29648/29649 (GRIFFO, RIS R 00004-00004988).
-- Cargada como Y662124170133; la real es Y662125150133 (ver renglones 6-7 del RIS).
-- Ensayado contra el dump de prod del 02/09: 2 filas en devices + 2 en delivery_order_items.
-- Correr:  wsl -d Ubuntu-24.04 -- docker exec -i nexora-postgres psql -U sepid -d servicio_tecnico < este_archivo

\set ON_ERROR_STOP on
\echo '--- PREFLIGHT (antes) ---'
SELECT 'devices' AS t, id::text AS ref, numero_serie, tipo_equipo FROM devices WHERE id IN (29234,29235)
UNION ALL
SELECT 'doi', id, partida, description FROM delivery_order_items
 WHERE id IN ('doi-8e74711f-3dbc-4aa1-84a7-a93983f9bc97','doi-d3d252ca-d7c2-4aac-9148-d28fbfd90d66');

\echo '--- GUARDA: la serie correcta no debe existir ya en devices ---'
SELECT count(*) AS colisiones FROM devices
 WHERE UPPER(REPLACE(REPLACE(numero_serie,' ',''),'-','')) = 'Y662125150133';

BEGIN;

UPDATE devices SET numero_serie = 'Y662125150133'
 WHERE id IN (29234,29235) AND TRIM(numero_serie) = 'Y662124170133';

UPDATE delivery_order_items SET partida = 'Y662125150133'
 WHERE id IN ('doi-8e74711f-3dbc-4aa1-84a7-a93983f9bc97','doi-d3d252ca-d7c2-4aac-9148-d28fbfd90d66')
   AND TRIM(partida) = 'Y662124170133';

COMMIT;

\echo '--- VERIFICACION (despues) ---'
SELECT 'devices' AS t, id::text AS ref, numero_serie, tipo_equipo FROM devices WHERE id IN (29234,29235)
UNION ALL
SELECT 'doi', id, partida, description FROM delivery_order_items
 WHERE id IN ('doi-8e74711f-3dbc-4aa1-84a7-a93983f9bc97','doi-d3d252ca-d7c2-4aac-9148-d28fbfd90d66');

\echo '--- RESIDUAL operativo (debe ser 0) ---'
SELECT (SELECT count(*) FROM devices WHERE numero_serie ILIKE '%Y662124170133%') AS devices_resto,
       (SELECT count(*) FROM delivery_order_items WHERE partida ILIKE '%Y662124170133%') AS doi_resto;
