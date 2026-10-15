"""Equipos vendidos a NOVAMED con stock en STR o STC que ningún equipo físico respalda.

Pedido del usuario 13/10/2026: un vendido puede estar en STR (volvió a reparación) o en STC (listo para
retirar), pero sólo si de verdad está acá. Se revisó cada unidad contra las OS de NEXORA: está
justificada si el equipo tiene una OS abierta (ingresado, diagnosticado, reparado, controlado,
liberado, derivado, vendido_pendiente_entrega). Estas 16 unidades (14 equipos) tienen todas sus OS
cerradas:
  - STR del bloque de aportes del 08/06: entraron con el ENT 00000921 de la migración del 23/04 y/o el
    RDA del 04/06, antes del RTN de aporte que salió de VAL.
  - STR de MG 4988 y MG 6561: ENT de NEXORA duplicado al reingresar (ENT + RIS el mismo 21/09).
  - STC de reparaciones de NOVAMED que ya salieron con RSS (el RSS no descuenta STC) y NM 0258 (ENT del
    restore del 08/06 con la OS cerrada).
Quedan afuera MG 007103 (STR -1 / +1 con artículos distintos: equipo duplicado en NEXORA) y las 7
unidades justificadas (MG 4198, 4552, 4700, 6793, 7985, 7989 en STR y MG 4801 en STC).

Correr dentro del contenedor de producción:
    docker exec -i nexora-api python manage.py shell < stock_vendidos_novamed_str_stc_sobrante_20261013.py
Por defecto es una prueba; APPLY=1 emite. Exige el stock completo esperado de cada partida.
"""
import os

from service.bejerman_stock_corrections import (
    _group_stock,
    _stock_rows_for_serial,
    build_stock_correction_sal_payload,
)
from service.bejerman_sync import BejermanSDKClient, validate_bejerman_config

APPLY = os.environ.get("APPLY") == "1"

# MG, partida, artículo, depósitos a sacar, comprobante que debió descontarlo, stock completo esperado
CASOS = [
    ("MG 4546", "MZJ5S121739", "1115003", ["STR"], "RTN R 00004-00004511", ["STR"]),
    ("MG 4595", "MZJ5S121687", "1115003", ["STR"], "RTN R 00004-00004510", ["STR"]),
    ("MG 4734", "MZJ5S121817", "1115003", ["STR"], "RTN R 00004-00004511", ["STR"]),
    ("MG 4894", "MZJ5S121901", "1115003", ["STR"], "RTN R 00004-00004511", ["STR"]),
    ("MG 4929", "MZJ5S121799", "1115003", ["STR"], "RTN R 00004-00004511", ["STR"]),
    ("MG 4993", "MZJ5S121651", "1115003", ["STR"], "RTN R 00004-00004510", ["STR"]),
    ("MG 6411", "MZJ5S121193", "1115003", ["STR"], "RTN R 00004-00004510", ["STR"]),
    ("MG 6420", "MZJ5S121235", "1115003", ["STR"], "RTN R 00004-00004510", ["STR"]),
    ("MG 6778", "MZJ5S416941", "1115003", ["STR"], "RTN R 00004-00004511", ["STR"]),
    ("MG 2051", "N5145150", "1115007", ["STC"], "RSS R 00001-00026300", ["STC"]),
    ("MG 4988", "MZJ5S121621", "1115003", ["STC", "STR"], "RSS R 00004-00005366", ["STC", "STR"]),
    ("MG 5010", "MZJ5S121627", "1115003", ["STC"], "RSS R 00004-00005366", ["STC"]),
    ("MG 6561", "MZJ5S121156", "1115003", ["STC", "STR"], "RSS R 00004-00005366", ["STC", "STR"]),
    ("NM 0258", "F0019", "1109012", ["STC"], "RTN R 00001-00023778", ["STC"]),
]


def stock(client, serial):
    groups = _group_stock(_stock_rows_for_serial(client.stock_by_deposit_partida("", serial), serial))
    return sorted((g["articulo"], g["deposito"], float(g["cantidad"])) for g in groups if float(g["cantidad"]) > 0)


validate_bejerman_config()
validate_bejerman_config(comprobante="BEJERMAN_STOCK_CORRECTION_COMPROBANTE")
client = BejermanSDKClient(company_key="SEPID", allow_system_credentials=True)
print("MODO:", "APLICAR" if APPLY else "PRUEBA (no escribe)")

for mg, serial, art, sacar, ref, esperado in CASOS:
    print(f"\n== {mg} {serial} art {art} | sacar {sacar} | {ref}")
    antes = stock(client, serial)
    print("   stock antes:", antes)
    if antes != sorted((art, d, 1.0) for d in esperado):
        print(f"   SALTEADO: se esperaba {art} con 1 en {esperado}")
        continue
    for seq, dep in enumerate(sacar, 1):
        payload = build_stock_correction_sal_payload(
            numero_serie=serial, articulo=art, deposito=dep, cantidad=1, run_id="SOB20261013", sequence=seq
        )
        payload[0]["Comprobante_IdOrigen"] = f"NEXORA-SOB-{dep}-{ref.replace(' ', '')}-{serial}"[:50]
        print("   SAL", dep, payload[0]["Comprobante_Numero"], payload[0]["Comprobante_IdOrigen"])
        if APPLY:
            print("   respuesta:", str(client.ingresar_lista_comprobantes_json(payload))[:60])
    if APPLY:
        print("   stock después:", stock(client, serial) or "sin stock")
