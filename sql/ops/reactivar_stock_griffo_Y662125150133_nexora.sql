-- Reactiva los dos pases STR -> STC que quedaron cerrados con la serie mal tipeada.
--
-- La OS 29648 (CPAP) debe mover el artículo 1103015 y la OS 29649 (humidificador),
-- el 1111009. La limpieza previa de Bejerman quita el ENT fantasma y su IdOrigen,
-- por eso estos jobs se reinician sin `sal_done` ni comprobantes previos: el worker
-- normal debe emitir SAL desde STR y ENT a STC para cada artículo.
--
-- Ejecutar en SERVERDATA con:
--   Get-Content -Raw "E:\Datos\Servicio Tecnico\1_SISTEMA REPARACIONES\NEXORA\sql\ops\reactivar_stock_griffo_Y662125150133_nexora.sql" |
--     wsl -d Ubuntu-24.04 -- docker exec -i nexora-postgres psql -U sepid -d servicio_tecnico

\set ON_ERROR_STOP on
\echo '--- PREFLIGHT jobs y renglones de entrega ---'

SELECT j.id, j.ingreso_id, j.device_id, j.numero_serie, j.sync_type,
       j.source_deposit, j.target_deposit, j.article_code, j.status, j.attempts
  FROM bejerman_sync_jobs j
 WHERE j.id IN (1211, 1214)
 ORDER BY j.id;

SELECT doi.id, doi.ingreso_id, doi.device_id, doi.article_code, doi.partida,
       doi.stock_deposit_code
  FROM delivery_order_items doi
 WHERE doi.ingreso_id IN (29648, 29649)
 ORDER BY doi.ingreso_id;

BEGIN;

DO $$
DECLARE
  jobs_ok integer;
  items_ok integer;
BEGIN
  SELECT count(*)
    INTO jobs_ok
    FROM bejerman_sync_jobs j
   WHERE (j.id = 1211 AND j.ingreso_id = 29648 AND j.device_id = 29234
          AND j.numero_serie = 'Y662124170133' AND j.sync_type = 'stock_str_to_stc'
          AND j.source_deposit = 'STR' AND j.target_deposit = 'STC'
          AND j.status = 'succeeded' AND j.article_code = '1111009')
      OR (j.id = 1214 AND j.ingreso_id = 29649 AND j.device_id = 29235
          AND j.numero_serie = 'Y662124170133' AND j.sync_type = 'stock_str_to_stc'
          AND j.source_deposit = 'STR' AND j.target_deposit = 'STC'
          AND j.status = 'succeeded' AND j.article_code = '1111009');

  SELECT count(*)
    INTO items_ok
    FROM delivery_order_items doi
   WHERE (doi.ingreso_id = 29648 AND doi.device_id = 29234
          AND doi.article_code = '1103015' AND doi.partida = 'Y662125150133'
          AND doi.stock_deposit_code = 'STC')
      OR (doi.ingreso_id = 29649 AND doi.device_id = 29235
          AND doi.article_code = '1111009' AND doi.partida = 'Y662125150133'
          AND doi.stock_deposit_code = 'STC');

  IF jobs_ok <> 2 OR items_ok <> 2 THEN
    RAISE EXCEPTION
      'Preflight inesperado (jobs correctos=%; renglones correctos=%). Abortado.',
      jobs_ok, items_ok;
  END IF;
END $$;

WITH correction (job_id, ingreso_id, serial, article_code, variante) AS (
  VALUES
    (1211, 29648, 'Y662125150133', '1103015', '20C'),
    (1214, 29649, 'Y662125150133', '1111009', '')
)
UPDATE bejerman_sync_jobs j
   SET numero_serie = c.serial,
       article_code = c.article_code,
       status = 'pending',
       attempts = 0,
       next_attempt_at = CURRENT_TIMESTAMP,
       last_error = NULL,
       request_payload = jsonb_build_object(
         'source', 'nexora',
         'trigger', 'correccion_serie_y662125150133',
         'reference', 'NEXORA-OS-' || c.ingreso_id::text,
         'companyKey', 'SEPID',
         'marca', 'Yamind',
         'modelo', 'DM26',
         'variante', c.variante,
         'article_code', c.article_code
       ),
       response_payload = jsonb_build_object(
         'correction', jsonb_build_object(
           'reason', U&'Se reinicia luego de eliminar el ENT STC con serie err\00F3nea.',
           'previousSerial', j.numero_serie,
           'correctedSerial', c.serial,
           'correctedArticleCode', c.article_code
         ),
         'previous_request_payload', COALESCE(j.request_payload, '{}'::jsonb),
         'previous_response_payload', COALESCE(j.response_payload, '{}'::jsonb)
       ),
       updated_at = CURRENT_TIMESTAMP
  FROM correction c
 WHERE j.id = c.job_id
 RETURNING j.id, j.ingreso_id, j.numero_serie, j.article_code, j.status, j.attempts;

COMMIT;

\echo '--- VERIFICACION ---'
SELECT id, ingreso_id, numero_serie, article_code, status, attempts,
       request_payload ->> 'trigger' AS trigger,
       response_payload -> 'correction' AS correction
  FROM bejerman_sync_jobs
 WHERE id IN (1211, 1214)
 ORDER BY id;
