from django.core.management.base import BaseCommand
from django.db import connection, transaction

from service.mg_codes import NORMALIZE_NUMERO_INTERNO_FUNCTION_SQL


TRIGGER_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION public.normalize_device_numero_interno()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.numero_interno := public.normalize_numero_interno(NEW.numero_interno);
  RETURN NEW;
END;
$$;
"""

DROP_TRIGGER_SQL = "DROP TRIGGER IF EXISTS trg_devices_normalize_numero_interno ON devices"

CREATE_TRIGGER_SQL = """
CREATE TRIGGER trg_devices_normalize_numero_interno
BEFORE INSERT OR UPDATE OF numero_interno ON devices
FOR EACH ROW EXECUTE FUNCTION public.normalize_device_numero_interno()
"""

# Fichas guardadas antes del trigger. Se saltean las que, ya normalizadas, quedarían con el mismo
# número interno que otra ficha: son equipos duplicados (o un MG mal cargado) que hay que unificar
# a mano. Normalizarlas chocaría con el índice único y, peor, taparía el duplicado.
BARRIDO_SQL = """
WITH normalizados AS (
  SELECT id, numero_interno AS actual, public.normalize_numero_interno(numero_interno) AS nuevo
    FROM devices
   WHERE NULLIF(BTRIM(numero_interno), '') IS NOT NULL
), repetidos AS (
  SELECT nuevo FROM normalizados GROUP BY nuevo HAVING COUNT(*) > 1
)
UPDATE devices d
   SET numero_interno = n.nuevo
  FROM normalizados n
 WHERE d.id = n.id
   AND n.nuevo IS DISTINCT FROM n.actual
   AND n.nuevo NOT IN (SELECT nuevo FROM repetidos)
RETURNING d.id, n.actual, n.nuevo
"""

PENDIENTES_SQL = """
WITH normalizados AS (
  SELECT id, numero_interno AS actual, public.normalize_numero_interno(numero_interno) AS nuevo
    FROM devices
   WHERE NULLIF(BTRIM(numero_interno), '') IS NOT NULL
)
SELECT nuevo, string_agg(id::text || '=' || quote_literal(actual), ', ' ORDER BY id)
  FROM normalizados
 GROUP BY nuevo
HAVING COUNT(*) > 1 AND bool_or(nuevo IS DISTINCT FROM actual)
 ORDER BY nuevo
"""


class Command(BaseCommand):
    help = (
        "Regla del 14/10/2026: el número interno (MG, NM, NV, CE) no distingue mayúsculas y se "
        "guarda normalizado ('mg 7359' -> 'MG 7359'). Instala el trigger que lo normaliza en cada "
        "escritura de devices y normaliza las fichas ya guardadas que no chocan con otra."
    )

    def handle(self, *args, **opts):
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(NORMALIZE_NUMERO_INTERNO_FUNCTION_SQL)
                cur.execute(TRIGGER_FUNCTION_SQL)
                cur.execute(DROP_TRIGGER_SQL)
                cur.execute(CREATE_TRIGGER_SQL)

        # El barrido va en su propia transacción. Este comando corre en el arranque de la API:
        # un problema de datos no puede dejar producción caída, y el trigger, que es lo que
        # protege las escrituras nuevas, ya quedó puesto.
        try:
            with transaction.atomic():
                with connection.cursor() as cur:
                    cur.execute(BARRIDO_SQL)
                    normalizados = cur.fetchall()
                    cur.execute(PENDIENTES_SQL)
                    pendientes = cur.fetchall()
        except Exception as exc:
            self.stderr.write(f"OMITIDO: no se pudieron normalizar los números internos guardados: {exc}")
            self.stdout.write("PARCIAL: trigger instalado; los números internos guardados quedaron como estaban")
            return

        for device_id, anterior, nuevo in normalizados:
            self.stdout.write(f"  device {device_id}: {anterior!r} -> {nuevo!r}")
        if pendientes:
            self.stderr.write(
                "PENDIENTE: estos números internos quedan sin normalizar porque, normalizados, "
                "coinciden con el de otra ficha. Son equipos duplicados: hay que unificarlos."
            )
            for numero, fichas in pendientes:
                self.stderr.write(f"  {numero}: {fichas}")
        self.stdout.write(
            f"APLICADO OK: trigger trg_devices_normalize_numero_interno instalado; "
            f"{len(normalizados)} números internos normalizados, {len(pendientes)} duplicados pendientes"
        )
