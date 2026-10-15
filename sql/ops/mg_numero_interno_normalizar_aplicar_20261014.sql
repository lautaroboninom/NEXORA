-- Regla del usuario 14/10/2026: "El mg no puede distinguir mayúsculas de minúsculas, si el
-- usuario escribió 'mg' NEXORA lo tiene que normalizar a 'MG'!! aplicá esta norma".
--
-- Instala en producción lo mismo que apply_devices_numero_interno_schema (que el próximo rebuild
-- vuelve a aplicar sin cambios, porque es CREATE OR REPLACE y el barrido ya no encuentra nada):
--   * public.normalize_numero_interno(text): la regla de service/mg_codes.py en SQL.
--     'mg 7359' / 'MG7359' / 'MG 007359' -> 'MG 7359'; 'MG 42' -> 'MG 0042'; espacios duros
--     (NBSP) como espacio; lo que no es código numérico sólo sube el prefijo.
--   * trg_devices_normalize_numero_interno: BEFORE INSERT OR UPDATE OF numero_interno ON devices.
--     Cualquier escritura queda canónica, venga de una vista, un comando o un script.
--   * Barrido de las 27 fichas guardadas fuera de forma (medidas en vivo el 14/10, ensayadas sobre
--     el dump de 01:30 restaurado en dev). Paridad SQL/Python/JS verificada en 3248 valores.
--
-- NO toca los 17 MG duplicados (dos fichas con el mismo número): normalizarlos chocaría con el
-- índice único y taparía el duplicado. Se resuelven unificando, caso por caso. Con el trigger
-- puesto, un duplicado nuevo en minúsculas ("mg 7217" con "MG 7217" ya cargado) lo rechaza el
-- índice uq_devices_numint_norm.
--
-- Para deshacer: mg_numero_interno_normalizar_revertir_20261014.sql (y sacar
-- apply_devices_numero_interno_schema de SCHEMA_COMMANDS, o el rebuild lo vuelve a poner).
--
-- Uso:  psql ... -v ON_ERROR_STOP=1 -f mg_numero_interno_normalizar_aplicar_20261014.sql

SET client_encoding = 'UTF8';

BEGIN;

SELECT set_config('app.user_id', '2', true);
SELECT set_config('app.user_role', 'jefe', true);

CREATE TEMP TABLE esperado (id integer PRIMARY KEY, actual text NOT NULL, nuevo text NOT NULL) ON COMMIT DROP;
INSERT INTO esperado (id, actual, nuevo) VALUES
  (19248, 'MG 131', 'MG 0131'),
  (28732, 'MG 160', 'MG 0160'),
  (19146, 'MG0814', 'MG 0814'),
  (15385, 'MG  1148', 'MG 1148'),
  (18170, 'MG1295', 'MG 1295'),
  (17907, 'MG1779', 'MG 1779'),
  (18097, 'MG2494', 'MG 2494'),
  (15803, 'MG3257', 'MG 3257'),
  (15355, 'MG3294', 'MG 3294'),
  (20668, 'mg 5015', 'MG 5015'),
  (29208, 'MG5548', 'MG 5548'),
  (29210, 'MG5826', 'MG 5826'),
  (29209, 'MG5980', 'MG 5980'),
  (29212, 'MG6294', 'MG 6294'),
  (29207, 'MG6407', 'MG 6407'),
  (25297, 'MG6806', 'MG 6806'),
  (29241, 'MG7217', 'MG 7217'),
  (29322, 'MG7301', 'MG 7301'),
  (29334, 'MG7330', 'MG 7330'),
  (29321, 'MG7851', 'MG 7851'),
  (29280, 'MG7887', 'MG 7887'),
  (29281, 'MG7888', 'MG 7888'),
  (29369, 'MG7980', 'MG 7980'),
  (28895, 'NM0228', 'NM 0228'),
  (25175, 'NM0251', 'NM 0251'),
  (19241, 'NM0272', 'NM 0272'),
  (16153, 'NM2740', 'NM 2740');

\echo == Función, función del trigger y trigger ==
CREATE OR REPLACE FUNCTION public.normalize_numero_interno(valor text)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
  limpio text;
  partes text[];
BEGIN
  IF valor IS NULL THEN
    RETURN NULL;
  END IF;
  limpio := regexp_replace(translate(valor, chr(160) || chr(8199) || chr(8239), '   '), '^\s+|\s+$', '', 'g');
  partes := regexp_match(UPPER(limpio), '^(MG|NM|NV|CE)\s*0*(\d{1,6})$');
  IF partes IS NOT NULL THEN
    RETURN partes[1] || ' ' || CASE WHEN length(partes[2]) < 4 THEN lpad(partes[2], 4, '0') ELSE partes[2] END;
  END IF;
  IF UPPER(limpio) ~ '^(MG|NM|NV|CE)' THEN
    RETURN UPPER(left(limpio, 2)) || substr(limpio, 3);
  END IF;
  RETURN valor;
END;
$$;

CREATE OR REPLACE FUNCTION public.normalize_device_numero_interno()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.numero_interno := public.normalize_numero_interno(NEW.numero_interno);
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_devices_normalize_numero_interno ON devices;

CREATE TRIGGER trg_devices_normalize_numero_interno
BEFORE INSERT OR UPDATE OF numero_interno ON devices
FOR EACH ROW EXECUTE FUNCTION public.normalize_device_numero_interno();

DO $$
DECLARE v integer;
BEGIN
  SELECT COUNT(*) INTO v FROM (
    (SELECT * FROM (WITH normalizados AS (
  SELECT id, numero_interno AS actual, public.normalize_numero_interno(numero_interno) AS nuevo
    FROM devices
   WHERE NULLIF(BTRIM(numero_interno), '') IS NOT NULL
), repetidos AS (
  SELECT nuevo FROM normalizados GROUP BY nuevo HAVING COUNT(*) > 1
)
SELECT id, actual, nuevo
  FROM normalizados
 WHERE nuevo IS DISTINCT FROM actual
   AND nuevo NOT IN (SELECT nuevo FROM repetidos)) c EXCEPT SELECT id, actual, nuevo FROM esperado)
    UNION ALL
    (SELECT id, actual, nuevo FROM esperado EXCEPT SELECT * FROM (WITH normalizados AS (
  SELECT id, numero_interno AS actual, public.normalize_numero_interno(numero_interno) AS nuevo
    FROM devices
   WHERE NULLIF(BTRIM(numero_interno), '') IS NOT NULL
), repetidos AS (
  SELECT nuevo FROM normalizados GROUP BY nuevo HAVING COUNT(*) > 1
)
SELECT id, actual, nuevo
  FROM normalizados
 WHERE nuevo IS DISTINCT FROM actual
   AND nuevo NOT IN (SELECT nuevo FROM repetidos)) c)
  ) diferencia;
  IF v <> 0 THEN
    RAISE EXCEPTION 'Las fichas a normalizar no son las 27 medidas (% diferencias): no se aplica nada', v;
  END IF;
END $$;

\echo == Barrido (esperado: UPDATE 27) ==
UPDATE devices d
   SET numero_interno = e.nuevo
  FROM esperado e
 WHERE d.id = e.id AND d.numero_interno = e.actual;

DO $$
DECLARE v integer;
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
     WHERE tgrelid = 'devices'::regclass AND tgname = 'trg_devices_normalize_numero_interno'
  ) THEN
    RAISE EXCEPTION 'Verificación: el trigger no quedó instalado';
  END IF;
  SELECT COUNT(*) INTO v FROM devices d JOIN esperado e ON e.id = d.id WHERE d.numero_interno = e.nuevo;
  IF v <> 27 THEN RAISE EXCEPTION 'Verificación: % de 27 fichas quedaron normalizadas', v; END IF;
  SELECT COUNT(*) INTO v FROM (WITH normalizados AS (
  SELECT id, numero_interno AS actual, public.normalize_numero_interno(numero_interno) AS nuevo
    FROM devices
   WHERE NULLIF(BTRIM(numero_interno), '') IS NOT NULL
), repetidos AS (
  SELECT nuevo FROM normalizados GROUP BY nuevo HAVING COUNT(*) > 1
)
SELECT id, actual, nuevo
  FROM normalizados
 WHERE nuevo IS DISTINCT FROM actual
   AND nuevo NOT IN (SELECT nuevo FROM repetidos)) c;
  IF v <> 0 THEN RAISE EXCEPTION 'Verificación: quedan % fichas sin normalizar fuera de los duplicados', v; END IF;
END $$;

\echo == Duplicados pendientes de unificar (esperado: 17) ==
WITH normalizados AS (
  SELECT id, numero_interno AS actual, public.normalize_numero_interno(numero_interno) AS nuevo
    FROM devices
   WHERE NULLIF(BTRIM(numero_interno), '') IS NOT NULL
)
SELECT nuevo AS numero_interno, string_agg(id::text || '=' || quote_literal(actual), ', ' ORDER BY id) AS fichas
  FROM normalizados
 GROUP BY nuevo
HAVING COUNT(*) > 1
 ORDER BY nuevo;

COMMIT;
