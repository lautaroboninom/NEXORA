"""Remitos de salida emitidos con ActualizaStock=N (junio-octubre 2026): unidades que nunca salieron.

Diagnóstico 13/10/2026 (SBDASEP, origen E): 13 remitos de salida sin movimiento de stock (4 RSS, 9 RT).
La recuperación por "partida inexistente / no informada" (`_recover_delivery_partida_rejection`) los emitió
enteros con ActualizaStock=N, así que ningún renglón descontó su depósito.

Este script corrige lo que es inequívoco: renglones de cantidad 1 cuya partida sigue con exactamente esa
unidad en el depósito del renglón y sin ningún movimiento posterior al remito.
  - RT 00004-00004616 (BRITA, 18/06): 4 equipos en VAL.
  - RT 00004-00005037 (DIAGNOSTIC MEDIC, 13/08): 7 equipos y 1 lote con 1 unidad en VAL.
  - MG 0089 (N5125573, equipo de NOVAMED entregado con el RSS 00004-00005366): STL del art. 1115007 (lo
    dejó ahí un ENT de NEXORA el 22/09, debió ir a STC) y VAL del art. 1115006 (el ENT de recuperación del
    renglón equivocado del RSS 5366).
Los lotes de insumos con cantidades mayores se tratan aparte: su stock quedó mezclado con otras ventas,
transferencias y ajustes.

Correr dentro de nexora-api: docker exec -i nexora-api python manage.py shell < este_archivo.py
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

# partida, [(artículo, depósito, cantidad a sacar)], comprobante, stock completo esperado [(art, dep, cant)]
CASOS = [
    ("Y2D23802009", [("1104012", "VAL", 1)], "RT R 00004-00004616", [("1104012", "VAL", 1)]),
    ("H2123806112", [("1111001", "VAL", 1)], "RT R 00004-00004616", [("1111001", "VAL", 1)]),
    ("H226502007D", [("1110003", "VAL", 1)], "RT R 00004-00004616", [("1110003", "VAL", 1)]),
    ("H226502008D", [("1110003", "VAL", 1)], "RT R 00004-00004616", [("1110003", "VAL", 1)]),
    ("S226D504009", [("1110001", "VAL", 1)], "RT R 00004-00005037", [("1110001", "VAL", 1)]),
    ("S225DC14021", [("1110001", "VAL", 1)], "RT R 00004-00005037", [("1110001", "VAL", 1)]),
    ("M1A26513018", [("1105004", "VAL", 1)], "RT R 00004-00005037", [("1105004", "VAL", 1)]),
    ("M1A26505140", [("1105004", "VAL", 1)], "RT R 00004-00005037", [("1105004", "VAL", 1)]),
    ("M1A26505135", [("1105004", "VAL", 1)], "RT R 00004-00005037", [("1105004", "VAL", 1)]),
    ("M1A26513036", [("1105004", "VAL", 1)], "RT R 00004-00005037", [("1105004", "VAL", 1)]),
    ("M1A26513035", [("1105004", "VAL", 1)], "RT R 00004-00005037", [("1105004", "VAL", 1)]),
    ("D2308053E", [("1201009", "VAL", 1)], "RT R 00004-00005037", [("1201009", "VAL", 1)]),
    ("N5125573", [("1115007", "STL", 1), ("1115006", "VAL", 1)], "RSS R 00004-00005366",
     [("1115006", "VAL", 1), ("1115007", "STL", 1)]),
]


def stock(client, serial):
    groups = _group_stock(_stock_rows_for_serial(client.stock_by_deposit_partida("", serial), serial))
    return sorted((g["articulo"], g["deposito"], float(g["cantidad"])) for g in groups if float(g["cantidad"]) > 0)


validate_bejerman_config()
validate_bejerman_config(comprobante="BEJERMAN_STOCK_CORRECTION_COMPROBANTE")
client = BejermanSDKClient(company_key="SEPID", allow_system_credentials=True)
print("MODO:", "APLICAR" if APPLY else "PRUEBA (no escribe)")

for serial, sacar, ref, esperado in CASOS:
    print(f"\n== {serial} | sacar {sacar} | {ref}")
    antes = stock(client, serial)
    print("   stock antes:", antes)
    if antes != sorted((a, d, float(q)) for a, d, q in esperado):
        print(f"   SALTEADO: se esperaba {esperado}")
        continue
    for seq, (art, dep, qty) in enumerate(sacar, 1):
        payload = build_stock_correction_sal_payload(
            numero_serie=serial, articulo=art, deposito=dep, cantidad=qty, run_id="ASN20261013", sequence=seq
        )
        payload[0]["Comprobante_IdOrigen"] = f"NEXORA-ASN-{dep}-{ref.replace(' ', '')}-{serial}"[:50]
        print("   SAL", art, dep, qty, payload[0]["Comprobante_Numero"], payload[0]["Comprobante_IdOrigen"])
        if APPLY:
            # D2308053E (13/10): Bejerman la rechazó por "no hay stock suficiente" con VAL=1: la unidad
            # está comprometida por otro comprobante. Un rechazo no frena al resto.
            try:
                print("   respuesta:", str(client.ingresar_lista_comprobantes_json(payload))[:60])
            except Exception as exc:
                print("   RECHAZADO:", str(exc)[:200])
    if APPLY:
        print("   stock después:", stock(client, serial) or "sin stock")
