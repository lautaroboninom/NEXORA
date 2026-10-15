-- Revierte mg_numero_interno_normalizar_aplicar_20261014.sql.
--
-- Saca el trigger trg_devices_normalize_numero_interno y sus dos funciones, y devuelve las 27
-- fichas normalizadas a como estaban escritas. Ojo: mientras apply_devices_numero_interno_schema
-- siga en SCHEMA_COMMANDS, el próximo rebuild lo vuelve a instalar y a barrer.
--
-- Uso:  psql ... -v ON_ERROR_STOP=1 -f mg_numero_interno_normalizar_revertir_20261014.sql

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

DO $$
DECLARE v integer;
BEGIN
  SELECT COUNT(*) INTO v FROM devices d JOIN esperado e ON e.id = d.id WHERE d.numero_interno = e.nuevo;
  IF v <> 27 THEN
    RAISE EXCEPTION 'Sólo % de las 27 fichas siguen como las dejó la corrida: no se revierte nada', v;
  END IF;
END $$;

\echo == Sin trigger ni funciones ==
DROP TRIGGER IF EXISTS trg_devices_normalize_numero_interno ON devices;
DROP FUNCTION IF EXISTS public.normalize_device_numero_interno();
DROP FUNCTION IF EXISTS public.normalize_numero_interno(text);

\echo == Las 27 fichas como estaban escritas (esperado: UPDATE 27) ==
UPDATE devices d
   SET numero_interno = e.actual
  FROM esperado e
 WHERE d.id = e.id AND d.numero_interno = e.nuevo;

COMMIT;
