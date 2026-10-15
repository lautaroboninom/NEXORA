"""Contadores de novedades de las colas de trabajo.

El sidebar necesita saber *cuántas órdenes nuevas* hay en cada cola sin bajarse
las cuatro listas completas: esas vistas traen cliente, equipo, MG, derivaciones
y serializan todo, y acá alcanza con los ids.

Quién decide qué es "nuevo" es el front (guarda en el navegador los ids que ya
mostró, ver `web/src/lib/queue-badges.js`). Este endpoint sólo contesta "esto es
lo que hay ahora en cada cola", que es la parte que la base sabe.
"""

from django.db import connection
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from ..permissions import user_has_permission
from ..mg_codes import sql_is_mg_code
from ..release_gate import sql_presupuesto_bloquea_salida


# Las colas viven en el taller: es el mismo recorte por ubicación que hacen las
# vistas de cada listado.
UBICACION_TALLER = "taller"

# Los WHERE son un espejo de los de `ingresos_views.py`. Si allá cambia el filtro
# de una cola, acá también: un contador que no coincide con la lista que abre el
# usuario es peor que no tener contador.
_SQL_PENDIENTES_GENERAL = """
    SELECT t.id
      FROM ingresos t
      LEFT JOIN locations loc ON loc.id = t.ubicacion_id
     WHERE LOWER(loc.nombre) = LOWER(%s)
       AND t.estado NOT IN ('liberado','entregado','alquilado','baja',
                            'vendido_pendiente_entrega','vendido_entregado')
"""

_SQL_MIS_PENDIENTES = """
    SELECT t.id
      FROM ingresos t
      LEFT JOIN locations loc ON loc.id = t.ubicacion_id
     WHERE t.asignado_a = %s
       AND LOWER(loc.nombre) = LOWER(%s)
       AND t.estado NOT IN ('entregado','liberado','alquilado','baja','no_se_repara',
                            'vendido_pendiente_entrega','vendido_entregado')
"""

_SQL_PENDIENTES_PRESUPUESTO = f"""
    SELECT t.id
      FROM ingresos t
      JOIN devices d ON d.id = t.device_id
     WHERE COALESCE(t.presupuesto_estado, 'pendiente') = 'pendiente'
       -- Es el mismo recorte que PendientesPresupuestoView: ni una nueva salida de
       -- alquiler ni la baja del equipo resuelven el presupuesto pendiente.
       AND t.estado NOT IN ('ingresado','entregado','liberado','no_se_repara',
                            'vendido_pendiente_entrega','vendido_entregado')
       AND (
         t.estado <> 'alquilado'
         OR (
           {sql_is_mg_code('d.numero_interno')}
           AND LOWER(BTRIM(COALESCE(d.mg_estado,'activo'))) <> 'inactivo_venta'
         )
         OR LOWER(BTRIM(COALESCE(t.motivo::text,''))) IN ('baja alquiler','reparación alquiler','reparacion alquiler')
       )
"""


def _sql_listos_para_liberar() -> str:
    """El contador no puede contar una OS que la lista no muestra.

    El corte de presupuesto se arma en `release_gate` -no está escrito acá- justamente
    porque este SQL y el de `AprobadosYReparadosView` tienen que decir lo mismo. Se
    resuelve por request y no en una constante de módulo porque el fragmento consulta el
    esquema (`devices.mg_estado` puede no existir) y a la hora de importar todavía no hay
    conexión.
    """
    return f"""
    SELECT t.id
      FROM ingresos t
      JOIN devices d ON d.id = t.device_id
      LEFT JOIN locations loc ON loc.id = t.ubicacion_id
     WHERE LOWER(loc.nombre) = LOWER(%s)
       AND t.estado IN ('reparado','controlado_sin_defecto','no_se_repara')
       AND NOT {sql_presupuesto_bloquea_salida("t", "d")}
"""


def _ids(cur, sql, params):
    cur.execute(sql, params)
    return [int(row[0]) for row in cur.fetchall()]


def _request_user_id(request):
    return (
        getattr(getattr(request, "user", None), "id", None)
        or getattr(request, "user_id", None)
    )


class ColasNovedadesView(APIView):
    """Ids de cada cola que el usuario tiene permiso de ver.

    No está en `VIEW_PERMISSION_MATRIX` a propósito: abarca dos permisos
    distintos (`page.work_queues` y `page.budget_queues`), así que el recorte se
    hace por cola y no por endpoint. Una cola sin permiso no viene en la
    respuesta —no viene vacía—, para que el front no dibuje el indicador.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        # Sin `_set_audit_user`: esto lo consulta el sidebar en loop y no es una
        # apertura de pantalla que valga la pena auditar.
        user_id = _request_user_id(request)
        ver_colas = user_has_permission(request, "page.work_queues")
        ver_presupuestos = user_has_permission(request, "page.budget_queues")

        colas = {}
        with connection.cursor() as cur:
            if ver_colas:
                colas["pendientes_general"] = _ids(
                    cur, _SQL_PENDIENTES_GENERAL, [UBICACION_TALLER]
                )
                colas["listos_para_liberar"] = _ids(
                    cur, _sql_listos_para_liberar(), [UBICACION_TALLER]
                )
                # Sin usuario resuelto la cola propia no existe; devolverla vacía
                # sería mentir con un cero en vez de no mostrar nada.
                if user_id:
                    colas["mis_pendientes"] = _ids(
                        cur, _SQL_MIS_PENDIENTES, [user_id, UBICACION_TALLER]
                    )
            if ver_presupuestos:
                colas["pendientes_presupuesto"] = _ids(
                    cur, _SQL_PENDIENTES_PRESUPUESTO, []
                )

        return Response({"colas": colas})
