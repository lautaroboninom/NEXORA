import csv
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.utils import timezone


WORK_CANDIDATES = [
    ("Limpieza y desinfección interna y externa", r"limpiez|desinfecci|higieniz"),
    ("Revisión técnica", r"revis|control.{0,16}(funcionamiento|equipo|limpieza)"),
    ("Pruebas y verificación de funcionamiento", r"prueb|verific|testeo|testeado|testear"),
    ("Calibración", r"calibr"),
    ("Ajustes y regulación", r"ajust|regulaci|\mregulad[oa]s?\M"),
    ("Reparación electrónica y soldadura", r"resold|soldad|soldar|repar.{0,20}(placa|circuit|conector)"),
    ("Configuración y programación", r"configur|programac|reprogram"),
    ("Mantenimiento general", r"mantenim|puesta a punto"),
]


class Command(BaseCommand):
    help = "Informe de solo lectura del último año para proponer trabajos estandarizados."

    def add_arguments(self,parser):
        parser.add_argument('--proposals',action='store_true',help='Agrupa menciones en propuestas de trabajos generales.')

    def handle(self,*args,**options):
        until = timezone.now()
        since = until-timedelta(days=365)
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            cursor.execute("SET LOCAL statement_timeout='20s'")
            if options['proposals']:
                rows = []
                for name,pattern in WORK_CANDIDATES:
                    cursor.execute("""SELECT COALESCE(NULLIF(d.tipo_equipo,''),m.tipo_equipo,'Sin tipo'),COUNT(DISTINCT i.id)
                        FROM ingresos i JOIN devices d ON d.id=i.device_id LEFT JOIN models m ON m.id=d.model_id
                        WHERE COALESCE(i.fecha_servicio,i.fecha_ingreso)>=%s AND COALESCE(i.fecha_servicio,i.fecha_ingreso)<=%s
                          AND translate(LOWER(COALESCE(i.trabajos_realizados,'')),'áéíóúü','aeiouu') ~ %s
                        GROUP BY 1 ORDER BY 2 DESC,1""",[since,until,pattern])
                    rows.extend((name,*row) for row in cursor.fetchall())
                writer = csv.writer(self.stdout)
                writer.writerow(['trabajo_propuesto','tipo_equipo','ordenes_con_mencion'])
                writer.writerows(rows)
                return
            cursor.execute("""WITH lineas AS (
              SELECT i.id,COALESCE(NULLIF(d.tipo_equipo,''),m.tipo_equipo,'Sin tipo') AS tipo,
                     BTRIM(regexp_replace(LOWER(p.texto),'\\s+',' ','g')) AS trabajo
              FROM ingresos i JOIN devices d ON d.id=i.device_id LEFT JOIN models m ON m.id=d.model_id
              CROSS JOIN LATERAL regexp_split_to_table(COALESCE(i.trabajos_realizados,''),E'[\\n;]+| - ') AS p(texto)
              WHERE COALESCE(i.fecha_servicio,i.fecha_ingreso)>=%s AND COALESCE(i.fecha_servicio,i.fecha_ingreso)<=%s
             ) SELECT tipo,trabajo,COUNT(DISTINCT id) FROM lineas WHERE trabajo<>'' GROUP BY tipo,trabajo ORDER BY COUNT(DISTINCT id) DESC,tipo,trabajo""",
             [since,until])
            rows = cursor.fetchall()
        writer = csv.writer(self.stdout)
        writer.writerow(['tipo_equipo','trabajo_original','ordenes_con_mencion'])
        writer.writerows(rows)
