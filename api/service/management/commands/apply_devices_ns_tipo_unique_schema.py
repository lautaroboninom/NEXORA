from django.core.management.base import BaseCommand
from django.db import connection, transaction


class Command(BaseCommand):
    help = (
        "Cambia la unicidad de devices de (numero de serie) a (numero de serie, tipo de equipo) "
        "para que dos equipos distintos puedan compartir el numero de serie "
        "(ej. el CPAP 'DM26' y el calentador humidificador que viene con el, que no trae serie propia). "
        "Es la contraparte de apply_models_tipo_equipo_unique_schema, una capa mas abajo."
    )

    def handle(self, *args, **opts):
        with transaction.atomic():
            with connection.cursor() as cur:
                # 1) El indice no puede hacer join, asi que el tipo tiene que estar en la fila
                #    de devices. Hasta ahora muchas filas lo tenian vacio y el tipo efectivo salia
                #    de COALESCE(NULLIF(d.tipo_equipo,''), m.tipo_equipo). Se copia desde el modelo.
                cur.execute(
                    """
                    UPDATE devices d
                       SET tipo_equipo = m.tipo_equipo
                      FROM models m
                     WHERE m.id = d.model_id
                       AND COALESCE(TRIM(d.tipo_equipo), '') = ''
                       AND COALESCE(TRIM(m.tipo_equipo), '') <> ''
                    """
                )
                backfilled = cur.rowcount

                # 2) La unicidad nueva es mas laxa que la vieja (agrega una columna a la clave),
                #    asi que ningun dato existente puede violarla. El chequeo queda igual como red,
                #    por si la base llego hasta aca sin el indice viejo puesto.
                cur.execute(
                    """
                    SELECT UPPER(REPLACE(REPLACE(numero_serie, ' ', ''), '-', '')) AS ns,
                           LOWER(COALESCE(TRIM(tipo_equipo), '')) AS tipo,
                           COUNT(*) AS cnt
                      FROM devices
                     WHERE NULLIF(TRIM(numero_serie), '') IS NOT NULL
                     GROUP BY 1, 2
                    HAVING COUNT(*) > 1
                    """
                )
                duplicados = cur.fetchall()
                if duplicados:
                    # Este comando corre en el arranque de la API, antes de gunicorn y con el
                    # frontend esperando que la API este sana. Abortar aca dejaria produccion
                    # caida por un problema de datos que no es urgente: el indice unico es una
                    # guarda, no algo de lo que dependa el codigo para funcionar. Se avisa
                    # fuerte, se deja el indice viejo en su lugar y el deploy sigue.
                    self.stderr.write(
                        "OMITIDO: hay equipos duplicados con el mismo numero de serie y tipo, "
                        "asi que no se puede crear el indice unico nuevo. El indice anterior "
                        "queda como esta y el CPAP no va a poder convivir con su humidificador "
                        "hasta que se unifiquen estos equipos:"
                    )
                    for ns, tipo, cnt in duplicados:
                        self.stderr.write(f"  numero_serie={ns!r} tipo={tipo!r} x{cnt}")
                    self.stdout.write(
                        f"PARCIAL: {backfilled} tipo_equipo completados desde models; "
                        "indice unico sin cambios por duplicados preexistentes"
                    )
                    return

                # El nuevo se crea antes de soltar el viejo: si el CREATE fallara, la
                # transaccion vuelve atras con la unicidad anterior todavia puesta.
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_devices_ns_tipo_norm
                      ON devices (
                        (UPPER(REPLACE(REPLACE(numero_serie, ' ', ''), '-', ''))),
                        (LOWER(COALESCE(TRIM(tipo_equipo), '')))
                      )
                      WHERE NULLIF(TRIM(numero_serie), '') IS NOT NULL
                    """
                )
                cur.execute("DROP INDEX IF EXISTS uq_devices_ns_norm")

        self.stdout.write(
            f"APLICADO OK: devices ahora es unico por numero de serie + tipo de equipo "
            f"({backfilled} tipo_equipo completados desde models)"
        )
