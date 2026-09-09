"""Un mismo equipo cargado dos veces el mismo día.

Que un equipo llegue con la OS anterior todavía abierta es un conflicto legítimo: volvió antes
de que se cerrara la anterior, el cliente está esperando el remito y el lote lo deja pasar
anotando el conflicto (``bejerman_ingreso_conflicts``). Ese permiso —``_allow_open_duplicate``—
tapaba también el caso que no es un conflicto sino un error: el mismo remito cargado dos veces.
La pantalla se cuelga esperando el PDF, el operador vuelve a cargar los mismos cuatro equipos y
salen cuatro OS gemelas del mismo día, con el mismo cliente y el mismo número de serie.

La diferencia entre uno y otro es la fecha de la OS que choca: si es de hoy, no se anota, se
frena.
"""

from datetime import date, datetime

from django.utils import timezone


# Estados en los que la OS ya soltó el equipo: no bloquean un ingreso nuevo.
INGRESO_ESTADOS_NO_BLOQUEAN_ALTA = {
    "entregado",
    "alquilado",
    "baja",
    "vendido_pendiente_entrega",
    "vendido_entregado",
}


def _fecha_local(valor):
    """La fecha calendario de un valor de la base, en la zona horaria de la casa."""

    if isinstance(valor, datetime):
        if timezone.is_aware(valor):
            valor = timezone.localtime(valor)
        return valor.date()
    if isinstance(valor, date):
        return valor
    return None


def ingreso_es_de_hoy(row) -> bool:
    """¿La OS nació hoy?

    Mira las dos fechas: ``fecha_creacion`` es cuándo se cargó de verdad y ``fecha_ingreso``
    puede venir escrita a mano. Con que cualquiera de las dos sea hoy alcanza para tratarlo
    como una recarga del mismo día.
    """

    hoy = timezone.localdate()
    return any(
        _fecha_local((row or {}).get(campo)) == hoy
        for campo in ("fecha_creacion", "fecha_ingreso")
    )


def ingreso_esta_abierto(row) -> bool:
    estado = str((row or {}).get("estado") or "").strip().lower()
    return bool(estado) and estado not in INGRESO_ESTADOS_NO_BLOQUEAN_ALTA


__all__ = [
    "INGRESO_ESTADOS_NO_BLOQUEAN_ALTA",
    "ingreso_es_de_hoy",
    "ingreso_esta_abierto",
]
