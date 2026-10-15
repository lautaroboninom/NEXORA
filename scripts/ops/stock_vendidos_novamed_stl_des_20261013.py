"""Equipos vendidos a NOVAMED que en Bejerman siguen con stock en STL o DES.

Regla del usuario (13/10/2026): un equipo vendido puede tener stock en STR (volvió a reparación) o en
STC (listo para retirar), pero en STL o DES es imposible.

Correr dentro del contenedor de producción (dev escribe en SBDPSEP):
    docker exec -i nexora-api python manage.py shell < stock_vendidos_novamed_stl_des_20261013.py
Por defecto es una prueba: verifica el stock completo de cada partida y muestra qué haría. APPLY=1 emite.

Origen de la unidad sobrante (MovStock SBDASEP): ENT de NEXORA numerados con la OS vieja (restore del
08/06/2026 y "liberado" del 14-15/09) que volvieron a cargar en STL equipos ya vendidos, y el backfill de
bajas del 30/06 para el DES de MG 4931. Acciones:
  - SAL del depósito imposible, con IdOrigen que cita el comprobante de venta (13 casos).
  - MG 4801 (OS 29643, vendido_pendiente_entrega: está acá esperando que NOVAMED lo retire): traslado
    STL -> STC con el job de traslado de ubicación de NEXORA, en vez de sacarlo del stock.
Las unidades en STR/STC no se tocan.
"""
import os

from service.bejerman_stock_corrections import (
    _group_stock,
    _stock_rows_for_serial,
    build_stock_correction_sal_payload,
)
from service.bejerman_sync import (
    BejermanSDKClient,
    enqueue_location_transfer_for_ingreso,
    process_bejerman_jobs,
    validate_bejerman_config,
)

APPLY = os.environ.get("APPLY") == "1"

# os, MG, partida, artículo, depósito a sacar, comprobante de venta, stock completo esperado (depósitos con 1)
SALIDAS = [
    (28124, "MG 1006", "06-0888-09-A", "1101004", "STL", "RTN R 00001-00023776", ["STL"]),
    (28774, "MG 1292", "07-0558-10-A", "1101004", "STL", "RTN R 00001-00023863", ["STL"]),
    (29284, "MG 2029", "G00857688", "1109019", "STL", "FC A 00002-00009909", ["STL"]),
    (29366, "MG 3078", "P133516769DED", "1103010", "STL", "FC A 00002-00009911", ["STL"]),
    (28817, "MG 3121", "011514040251", "1109012", "STL", "RTN R 00001-00025983", ["STL"]),
    (29838, "MG 4552", "MZJ5S121776", "1115003", "STL", "RTN R 00004-00004511", ["STL", "STR"]),
    (28740, "MG 4793", "MZJ5S121899", "1115003", "STL", "RTN R 00001-00023865", ["STL"]),
    (28736, "MG 4862", "P-06-400-19A", "1101004", "STL", "RTN R 00001-00023863", ["STL"]),
    (25438, "MG 4899", "G02849323", "1109020", "STL", "RTN R 00004-00004510", ["STL"]),
    (27616, "MG 4931", "MZJ5S121762", "1115003", "DES", "RTN R 00004-00004511", ["DES"]),
    (29781, "MG 6561", "MZJ5S121156", "1115003", "STL", "RTN R 00004-00004510", ["STC", "STL", "STR"]),
    (27482, "MG 7288", "H2122805350", "1111001", "STL", "RT R 00001-00023599", ["STL"]),
    (27481, "MG 7289", "T2A22701091", "1104002", "STL", "RT R 00001-00023599", ["STL"]),
]
TRASLADO_STC = (29643, "MG 4801", "MZJ5S121954", "1115003")


def stock(client, serial):
    groups = _group_stock(_stock_rows_for_serial(client.stock_by_deposit_partida("", serial), serial))
    return sorted((g["articulo"], g["deposito"], float(g["cantidad"])) for g in groups if float(g["cantidad"]) > 0)


validate_bejerman_config()
validate_bejerman_config(comprobante="BEJERMAN_STOCK_CORRECTION_COMPROBANTE")
client = BejermanSDKClient(company_key="SEPID", allow_system_credentials=True)
print("MODO:", "APLICAR" if APPLY else "PRUEBA (no escribe)")

for os_id, mg, serial, art, dep, venta, esperado in SALIDAS:
    print(f"\n== OS {os_id} {mg} {serial} art {art} | sacar de {dep} | venta {venta}")
    antes = stock(client, serial)
    print("   stock antes:", antes)
    if antes != sorted((art, d, 1.0) for d in esperado):
        print(f"   SALTEADO: se esperaba {art} con 1 en {esperado}")
        continue
    payload = build_stock_correction_sal_payload(
        numero_serie=serial, articulo=art, deposito=dep, cantidad=1, run_id="VTA20261013"
    )
    payload[0]["Comprobante_IdOrigen"] = f"NEXORA-VTA-{venta.replace(' ', '')}-{serial}"[:50]
    print("   SAL", dep, payload[0]["Comprobante_Numero"], payload[0]["Comprobante_IdOrigen"])
    if not APPLY:
        continue
    print("   respuesta:", str(client.ingresar_lista_comprobantes_json(payload))[:120])
    print("   stock después:", stock(client, serial) or "sin stock")

os_id, mg, serial, art = TRASLADO_STC
print(f"\n== OS {os_id} {mg} {serial} art {art} | traslado STL -> STC (vendido, pendiente de entrega)")
antes = stock(client, serial)
print("   stock antes:", antes)
if antes != [(art, "STL", 1.0)]:
    print("   SALTEADO: se esperaba exactamente STL=1")
elif APPLY:
    job = enqueue_location_transfer_for_ingreso(
        os_id, source_deposit="STL", target_deposit="STC",
        source_location="Estantería de Alquiler", target_location="Taller",
    )
    print("   job", job.get("id"), job.get("status"))
    print("   proceso:", process_bejerman_jobs(job_id=int(job["id"]), client=client))
    print("   stock después:", stock(client, serial))
else:
    print("   haría el traslado STL -> STC (job stock_location_transfer)")
