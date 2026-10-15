"""Stock que dejaron mal las recuperaciones de RSS/RIS con ActualizaStock=N (diagnóstico 13/10/2026).

  - RSS 00004-00005202 (04/09) y 00004-00005366 (29/09) salieron con ActualizaStock=N porque un renglón
    tenía una partida inexistente en su depósito: los demás equipos, ya entregados (OS en "entregado"),
    siguieron en STC.
  - RSS 00004-00004919 (29/07): el ENT de recuperación dejó M20406700017 en VAL y el remito no lo
    descontó; el equipo ya se entregó (OS 29364).
  - RIS 00004-00005325 (21/09): el reintento entró igual y NEXORA repuso el ENT -> STR duplicado.
    MG 6515 (diagnosticado, sigue acá) queda con 1; MG 4724 (liberado, en STC) pierde el STR;
    MZJ5S117798 (entregado con el RSS 5366) pierde STR y STC.
Queda afuera N5125573 (MG 0089): tiene VAL y STL con dos artículos distintos; se revisa aparte.

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

# descripción, partida, artículo, [(depósito, cantidad a sacar)], comprobante, stock completo esperado
CASOS = [
    ("OS 29340 entregado", "670004", "1109012", [("STC", 1)], "RSS R 00004-00005202", [("STC", 1)]),
    ("OS 29632 entregado", "csx1861k005", "1103004", [("STC", 1)], "RSS R 00004-00005202", [("STC", 1)]),
    ("OS 29667 entregado", "D7#1713", "1111003", [("STC", 1)], "RSS R 00004-00005202", [("STC", 1)]),
    ("OS 29774 entregado", "S12BEA06014", "1115006", [("STC", 1)], "RSS R 00004-00005366", [("STC", 1)]),
    ("OS 29779 entregado", "MZJ5S117798", "1115003", [("STC", 1), ("STR", 1)], "RSS R 00004-00005366", [("STC", 1), ("STR", 1)]),
    ("OS 29364 entregado", "M20406700017", "1108010", [("VAL", 1)], "RSS R 00004-00004919", [("VAL", 1)]),
    ("MG 6515 OS 29782 diagnosticado", "MZJ5S121377", "1115003", [("STR", 1)], "RIS R 00004-00005325", [("STR", 2)]),
    ("MG 4724 OS 29778 liberado", "MZJ5S121719", "1115003", [("STR", 1)], "RIS R 00004-00005325", [("STC", 1), ("STR", 1)]),
]


def stock(client, serial):
    groups = _group_stock(_stock_rows_for_serial(client.stock_by_deposit_partida("", serial), serial))
    return sorted((g["articulo"], g["deposito"], float(g["cantidad"])) for g in groups if float(g["cantidad"]) > 0)


validate_bejerman_config()
validate_bejerman_config(comprobante="BEJERMAN_STOCK_CORRECTION_COMPROBANTE")
client = BejermanSDKClient(company_key="SEPID", allow_system_credentials=True)
print("MODO:", "APLICAR" if APPLY else "PRUEBA (no escribe)")

for desc, serial, art, sacar, ref, esperado in CASOS:
    print(f"\n== {serial} ({desc}) art {art} | sacar {sacar} | {ref}")
    antes = stock(client, serial)
    print("   stock antes:", antes)
    if antes != sorted((art, d, float(q)) for d, q in esperado):
        print(f"   SALTEADO: se esperaba {esperado}")
        continue
    for seq, (dep, qty) in enumerate(sacar, 1):
        payload = build_stock_correction_sal_payload(
            numero_serie=serial, articulo=art, deposito=dep, cantidad=qty, run_id="REC20261013", sequence=seq
        )
        payload[0]["Comprobante_IdOrigen"] = f"NEXORA-REC-{dep}-{ref.replace(' ', '')}-{serial}"[:50]
        print("   SAL", dep, qty, payload[0]["Comprobante_Numero"], payload[0]["Comprobante_IdOrigen"])
        if APPLY:
            print("   respuesta:", str(client.ingresar_lista_comprobantes_json(payload))[:60])
    if APPLY:
        print("   stock después:", stock(client, serial) or "sin stock")
