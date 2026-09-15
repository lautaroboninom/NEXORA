"""El pase a la estantería de alquiler, con sus dos mitades juntas.

"Disponible para alquiler" no es un estado del ingreso: es la ubicación 'Estantería
de Alquiler' de NEXORA intersecada con stock positivo en STL en Bejerman (lo mismo
que miran `_rental_context_blocker` y la validación RENTAL_STL_STOCK_REQUIRED de la
orden de entrega). Mover una sola de las dos mitades deja el equipo invisible para
quien lo quiere alquilar -ubicación sin stock- o descontando de un depósito que nunca
lo recibió, así que el pase vive acá y no copiado en cada vista que lo dispara.

Para los equipos propios esta estantería es lo que "Listos para liberar" es para los
de clientes: el lugar donde el equipo queda a disposición cuando el taller terminó.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import connection

from .bejerman_sync import enqueue_stock_transfer_for_ingreso, ingreso_targets_rental_stock
from .constants import is_rental_shelf_location_name

logger = logging.getLogger(__name__)

RENTAL_SHELF_LOCATION_NAME = "Estantería de Alquiler"

# Estados desde los que un equipo propio ya puede volver al parque: el trabajo de taller
# terminó. Antes de eso la unidad sigue abierta sobre el banco y la estantería la
# ofrecería para alquilar.
RENTAL_SHELF_READY_ESTADOS = frozenset({"reparado", "controlado_sin_defecto"})

__all__ = [
    "RENTAL_SHELF_LOCATION_NAME",
    "RENTAL_SHELF_READY_ESTADOS",
    "is_rental_shelf_location_name",
    "ingreso_is_ready_for_rental_shelf",
    "rental_shelf_location",
    "move_ingreso_to_rental_shelf",
]


def rental_shelf_location() -> dict[str, Any] | None:
    """La estantería de alquiler, resuelta por nombre.

    Los ids de `locations` son datos de instancia: clavar un número acá funcionaría en
    dev y mandaría los equipos a cualquier lado en producción.
    """
    # Import diferido: `service.views` importa este módulo y a esta altura todavía se
    # está armando el paquete.
    from .views.helpers_impl import ensure_default_locations

    try:
        ensure_default_locations()
    except Exception:
        logger.exception("No se pudieron asegurar las ubicaciones por defecto")
    with connection.cursor() as cur:
        cur.execute(
            "SELECT id, nombre FROM locations WHERE LOWER(nombre)=LOWER(%s) LIMIT 1",
            [RENTAL_SHELF_LOCATION_NAME],
        )
        row = cur.fetchone()
    if not row:
        return None
    return {"id": row[0], "nombre": row[1]}


def ingreso_is_ready_for_rental_shelf(ingreso_id: int) -> bool:
    """¿El taller ya terminó con este ingreso?"""
    with connection.cursor() as cur:
        cur.execute("SELECT LOWER(COALESCE(estado::text,'')) FROM ingresos WHERE id=%s", [ingreso_id])
        row = cur.fetchone()
    return bool(row) and (row[0] or "").strip() in RENTAL_SHELF_READY_ESTADOS


def move_ingreso_to_rental_shelf(ingreso_id: int, *, actor_user_id: int | None = None) -> dict[str, Any]:
    """Deja el equipo propio en la estantería y pide el pase de su stock a STL.

    No decide si el trabajo terminó -eso lo sabe cada llamador- pero sí revalida que el
    equipo sea del parque de alquiler: un equipo de cliente, o un MG ya vendido, no va a
    STL ni a la estantería.

    Ninguna de las dos mitades corta el flujo de quien la llama: el pase es una
    consecuencia de otra acción (marcar reparado, resolver el presupuesto, mover la
    ubicación a mano) y esa acción ya está hecha. Lo que salió y lo que no vuelve en el
    resultado para poder decirlo en pantalla.
    """
    result: dict[str, Any] = {
        "applies": False,
        "moved": False,
        "already_there": False,
        "location_id": None,
        "location_name": RENTAL_SHELF_LOCATION_NAME,
        "stock_queued": False,
        "stock_error": "",
        "reason": "",
    }

    try:
        applies = ingreso_targets_rental_stock(ingreso_id)
    except Exception:
        logger.exception(
            "No se pudo determinar si el equipo va al parque de alquiler", extra={"ingreso_id": ingreso_id}
        )
        result["reason"] = "No se pudo determinar si el equipo es del parque de alquiler."
        return result

    if not applies:
        result["reason"] = "El equipo no es del parque de alquiler: su stock no va a STL."
        return result
    result["applies"] = True

    try:
        location = rental_shelf_location()
        if location:
            result["location_id"] = location.get("id")
            result["location_name"] = location.get("nombre") or RENTAL_SHELF_LOCATION_NAME
            with connection.cursor() as cur:
                cur.execute("SELECT ubicacion_id FROM ingresos WHERE id=%s", [ingreso_id])
                row = cur.fetchone()
                current_id = row[0] if row else None
                if int(current_id or 0) == int(location.get("id") or 0):
                    result["already_there"] = True
                else:
                    cur.execute(
                        "UPDATE ingresos SET ubicacion_id=%s WHERE id=%s", [location.get("id"), ingreso_id]
                    )
                    result["moved"] = True
        else:
            logger.warning(
                "No existe la ubicación %s: el equipo queda donde estaba",
                RENTAL_SHELF_LOCATION_NAME,
                extra={"ingreso_id": ingreso_id},
            )
    except Exception:
        logger.exception("No se pudo mover el equipo a la estantería de alquiler", extra={"ingreso_id": ingreso_id})

    try:
        enqueue_stock_transfer_for_ingreso(ingreso_id, actor_user_id=actor_user_id)
        result["stock_queued"] = True
    except Exception as exc:
        logger.exception("No se pudo encolar transferencia Bejerman a STL", extra={"ingreso_id": ingreso_id})
        result["stock_error"] = str(exc)

    return result
