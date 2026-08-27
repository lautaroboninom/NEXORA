"""Cierra los dos procesos duplicados de MARK (OS-29399) del 01/07/2026.

Verificado por SQL directo contra Bejerman el 13/08/2026: existen los dos RSS,
ninguno anulado.

    RSS R 00004-00004740  (scv_ID 17958, sello NEXORA brg-b8420d57...)
    RSS R 00004-00004741  (scv_ID 17959, sello NEXORA brg-2662a0d5...)

La orden OS-29399 quedó documentada con el 00004741, en poder del grupo
brg-7842fbfe. Los otros dos procesos no representan nada: no se pueden verificar
(la orden pertenece a otro grupo) ni reemitir, así que se cierran como duplicados.

Queda para una persona: **anular el RSS 00004-00004740 en Bejerman**, que movió
stock de más.

    wsl -d Ubuntu-24.04 -- docker exec -i nexora-api python manage.py shell < "/mnt/e/Datos/Servicio Tecnico/1_SISTEMA REPARACIONES/NEXORA/scripts/cerrar_duplicados_mark_20260813.py"
"""

from service.bejerman_delivery import resolve_bejerman_remito_review

ACTOR = 2  # Lautaro, que autorizó la corrección

MOTIVO = (
    "Verificado por SQL directo contra Bejerman el 13/08/2026: existen los dos RSS del 01/07 "
    "(00004-00004740 scv_ID 17958 y 00004-00004741 scv_ID 17959), ninguno anulado. Los tres "
    "intentos fueron de la misma entrega de OS-29399: los dos primeros fallaron con el error "
    "expCuotasV pero igual emitieron. La orden quedó con el 00004741. Este proceso se cierra "
    "como duplicado. Pendiente: anular el 00004-00004740 en Bejerman."
)

DUPLICADOS = [
    "brg-b8420d57-0d3a-4f0c-ba62-dcad259fc19e",  # emitió el 00004740 (el sobrante)
    "brg-2662a0d5-9906-4f73-a592-998f0c779acd",  # emitió el 00004741 (el que usa la orden)
]

for group_id in DUPLICADOS:
    try:
        resultado = resolve_bejerman_remito_review(
            group_id,
            ACTOR,
            {"decision": "duplicate", "confirm": True, "reason": MOTIVO},
        )
        print(f"{group_id}: {resultado.get('reviewState')}")
    except Exception as exc:
        print(f"{group_id}: ERROR {type(exc).__name__}: {exc}")
