"""Equipos que volvieron a estar de alta en NEXORA pero siguen en DES en Bejerman.

Pedido del usuario 13/10/2026 (OS 26974, MG 4742): "se dio de baja pero se volvió a dar de alta, solo
que no estaba ese circuito incorporado para volverlo a STR; chequeá todos los que volvieron a estar de
alta y sacalos de DES".

Correr dentro del contenedor de producción (dev escribe en SBDPSEP):
    docker exec -i nexora-api python manage.py shell < stock_alta_desde_des_20261013.py
Por defecto es una prueba: verifica el stock y muestra qué haría. Con APPLY=1 emite.

Barrido del 13/10/2026: 141 partidas con stock en DES (SEPID + MG BIO). 127 bien (la última OS
del equipo está de baja), 1 vendida en NEXORA, 13 de alta. A los 13 los mandó a DES el backfill de
bajas históricas del 30/06/2026 (ENT numerado con una OS de baja VIEJA), aunque el equipo ya tenía
OS posteriores. Se corrigen 9 con el circuito de alta de NEXORA (job stock_from_desguace, DES ->
STR); 4 quedan afuera porque el equipo no está en casa (17433833 de cliente entregado, MG 1569
vendido a ETICA en 02/2025, CE 0102 entregado a NOVAMED con RSS 4490, MG 4931 aporte a NOVAMED con
RTN 4511).

Después del alta:
  - MG 6430 (OS 29151) está en Sarmiento: se reabre el job 1366 (STR -> SAR) que quedó bloqueado
    porque la partida estaba en DES.
  - MG 4568 (OS 29289) y MG 2160 (OS 29452) están alquilados a OXICASTHOMECARE con RTA emitido: se
    completa el circuito STR -> STL y SAL de STL citando el RTA. En MG 2160 el RTA 00004-00004709 se
    emitió sobre la partida "5151340" (sin la N), que es la partida real del equipo en Bejerman y
    está bien en 0; la "N5151340" de NEXORA es un fantasma creado por el ENT del 26/06/2026.
"""
import os

from service.bejerman_stock_corrections import (
    _group_stock,
    _stock_rows_for_serial,
    build_stock_correction_sal_payload,
)
from service.bejerman_sync import (
    BejermanSDKClient,
    enqueue_stock_alta_for_ingreso,
    enqueue_stock_transfer_for_ingreso,
    process_bejerman_jobs,
    validate_bejerman_config,
)

APPLY = os.environ.get("APPLY") == "1"

# os (última del equipo), MG, partida, artículo, después del alta: None | ("SAR", job) | ("ALQ", remito)
CASOS = [
    (29314, "MG 7989", "A1-0139", "1101005", None),
    (27775, "MG 6170", "E2120301151", "1103001", None),
    (29154, "MG 4551", "MZJ5S121973", "1115003", None),
    (29251, "MG 3441", "N10360716556", "1107011", None),
    (26974, "MG 4742", "MZJ5S121893", "1115003", None),
    (25891, "MG 1912", "G02812653", "1109020", None),
    (29151, "MG 6430", "MZJ5S121348", "1115003", ("SAR", 1366)),
    (29289, "MG 4568", "MZJ5S121967", "1115003", ("ALQ", "RTA 00004-00004453")),
    (29452, "MG 2160", "N5151340", "1115007", ("ALQ", "RTA 00004-00004709")),
]


def stock(client, serial):
    groups = _group_stock(_stock_rows_for_serial(client.stock_by_deposit_partida("", serial), serial))
    return [(g["articulo"], g["deposito"], float(g["cantidad"])) for g in groups if float(g["cantidad"]) > 0]


validate_bejerman_config()
validate_bejerman_config(comprobante="BEJERMAN_STOCK_CORRECTION_COMPROBANTE")
client = BejermanSDKClient(company_key="SEPID", allow_system_credentials=True)
print("MODO:", "APLICAR" if APPLY else "PRUEBA (no escribe)")

for os_id, mg, serial, art, despues in CASOS:
    print(f"\n== OS {os_id} {mg} {serial} art {art} | después: {despues}")
    antes = stock(client, serial)
    print("   stock antes:", antes)
    if antes != [(art, "DES", 1.0)]:
        print(f"   SALTEADO: se esperaba exactamente {art} DES=1")
        continue
    if not APPLY:
        print("   haría alta DES->STR (job stock_from_desguace)", "y luego", despues or "nada más")
        continue

    job = enqueue_stock_alta_for_ingreso(os_id)
    print("   alta: job", job.get("id"), job.get("status"))
    print("   proceso:", process_bejerman_jobs(job_id=int(job["id"]), client=client))
    medio = stock(client, serial)
    print("   stock después del alta:", medio)
    if medio != [(art, "STR", 1.0)]:
        print("   FRENADO: el alta no dejó la unidad en STR")
        continue

    if despues and despues[0] == "SAR":
        print("   reabro job", despues[1], process_bejerman_jobs(job_id=despues[1], client=client, reopen_blocked=True))
        print("   stock final:", stock(client, serial))
    elif despues and despues[0] == "ALQ":
        remito = despues[1]
        job = enqueue_stock_transfer_for_ingreso(os_id, requeue_succeeded=True)
        print("   traslado STR->STL: job", job.get("id"), job.get("status"))
        print("   proceso:", process_bejerman_jobs(job_id=int(job["id"]), client=client))
        stl = stock(client, serial)
        print("   stock después del traslado:", stl)
        if stl != [(art, "STL", 1.0)]:
            print("   FRENADO: el traslado no dejó la unidad en STL; no se emite la SAL")
            continue
        payload = build_stock_correction_sal_payload(
            numero_serie=serial, articulo=art, deposito="STL", cantidad=1, run_id="ALTA20261013"
        )
        payload[0]["Comprobante_IdOrigen"] = f"NEXORA-ALQ-{remito.replace(' ', '')}-{serial}"[:50]
        print("   SAL STL:", payload[0]["Comprobante_Numero"], payload[0]["Comprobante_IdOrigen"])
        print("   respuesta:", str(client.ingresar_lista_comprobantes_json(payload))[:200])
        print("   stock final:", stock(client, serial) or "sin stock (OK)")
