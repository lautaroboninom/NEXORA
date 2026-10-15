"""Equipos alquilados con el remito de alquiler emitido pero con stock todavía en un depósito nuestro.

Pedido del usuario 13/10/2026 (OS 26975 como ejemplo): "completá el circuito, pasalo a STL y luego
con el remito de salida que aparece sacalo del stock".

Correr dentro del contenedor de producción (dev escribe en SBDPSEP):
    docker exec -i nexora-api python manage.py shell < stock_alquiler_remito_sin_descontar_20261013.py
Por defecto es una prueba: verifica el stock en Bejerman y muestra qué emitiría, sin escribir.
Con la variable APPLY=1 emite los movimientos.

Lo que se encontró caso por caso (SBDASEP / SBDAMGBI, 13/10/2026): el remito de alquiler sí descontó
STL, y después otro movimiento volvió a cargar la unidad:
  - STR (5 casos): el ENT de la migración MGBI->SEP del 23/04/2026 (ENT 00000917/921/927) trajo a
    SEPID el stock que MG BIO tenía de la devolución de Praxair de 12/2024, pero el equipo ya había
    salido alquilado desde SEPID en 2025.
  - STL (4 casos): la carga inicial RT 00022635 del 09/04/2024 entró dos veces (MG 7212, 7133, 3526),
    o un ENT de NEXORA del 12/08/2026 lo recargó al día siguiente del RTA (MG 7179).
Circuito: si está en STR, el traslado STR->STL de siempre (job stock_str_to_stl, el mismo de un
equipo propio liberado); después una SAL de STL cuyo IdOrigen cita el remito de alquiler.
"""
import os

from service.bejerman_stock_corrections import (
    _group_stock,
    _stock_rows_for_serial,
    build_stock_correction_sal_payload,
)
from service.bejerman_sync import (
    BejermanSDKClient,
    enqueue_stock_transfer_for_ingreso,
    process_bejerman_jobs,
    validate_bejerman_config,
)

APPLY = os.environ.get("APPLY") == "1"

# os, MG, partida, artículo, depósito donde está hoy, remito de alquiler (cliente)
CASOS = [
    (26951, "MG 6746", "H2120222297", "1111001", "STR", "RTN 00001-00024805", "GUARDIS"),
    (26955, "MG 5877", "H2120301143", "1111001", "STR", "RTN 00001-00025136", "GUARDIS"),
    (26971, "MG 6768", "MZJ5S416987", "1115003", "STR", "RTN 00001-00024609", "BIOMEDIX"),
    (26972, "MG 6413", "MZJ5S121195", "1115003", "STR", "RTN 00001-00024640", "SIED"),
    (26975, "MG 5048", "MZJ5S122032", "1115005", "STR", "RTN 00001-00024640", "SIED"),
    (25863, "MG 7212", "E2321905419", "1105001", "STL", "RTN 00001-00023833", "PERELETEGUI"),
    (25955, "MG 7133", "C3121A01298", "1103003", "STL", "RTN 00001-00023927", "OXIGENOTERAPIA LOMAS"),
    (26244, "MG 3526", "T0123444", "1113004", "STL", "RTN 00001-00024370", "SIED"),
    (28352, "MG 7179", "MC22040040099", "1115001", "STL", "RTA 00004-00005011", "SLOBINSKY"),
    # Agregado 13/10/2026 después de sacarlo de DES (stock_alta_desde_des_20261013.py): el RTN
    # 00001-00024640 se emitió con la partida mal escrita "MJZ5S121893" (quedó en 0), así que la
    # partida real "MZJ5S121893" conservó la unidad.
    (26974, "MG 4742", "MZJ5S121893", "1115003", "STR", "RTN 00001-00024640", "SIED"),
    # Agregados 13/10/2026 con la limpieza de la cola (cola_presupuesto_realquiladas_y_ventas_aplicar_20261013.sql):
    # OS que ya volvieron a salir con RTA y quedaron con una unidad de más. En STR: el ENT 00000927 de la
    # migración del 23/04 (MG 6107) o un ENT de NEXORA duplicando el RDA (MG 7301); en STL: un ENT de
    # NEXORA posterior al RTA (MG 4788, MG 6864, MG 5166; este último en MG BIO).
    (29445, "MG 6107", "H2120301223", "1111001", "STR", "RTA 00004-00004455", "MERNO"),
    (29793, "MG 7301", "MZJ10S72911", "1115004", "STR", "RTA 00004-00005416", "BIOMEDICAL"),
    (29770, "MG 4788", "MZJ5S121644", "1115003", "STL", "RTA 00004-00005387", "SIED"),
    (29269, "MG 6864", "214-9280", "1114002", "STL", "RTA 00004-00004294", "ALCLA"),
    (29593, "MG 5166", "CJD19631180", "1111002", "STL", "RTA 00001-00023972", "PRAXAIR", "MGBIO"),
]


def stock(client, serial):
    groups = _group_stock(_stock_rows_for_serial(client.stock_by_deposit_partida("", serial), serial))
    return [(g["articulo"], g["deposito"], float(g["cantidad"])) for g in groups if float(g["cantidad"]) > 0]


validate_bejerman_config()
validate_bejerman_config(comprobante="BEJERMAN_STOCK_CORRECTION_COMPROBANTE")
clients = {}
print("MODO:", "APLICAR" if APPLY else "PRUEBA (no escribe)")

for os_id, mg, serial, art, dep, remito, cliente, *resto in CASOS:
    empresa = resto[0] if resto else "SEPID"
    if empresa not in clients:
        clients[empresa] = BejermanSDKClient(company_key=empresa, allow_system_credentials=True)
    client = clients[empresa]
    print(f"\n== OS {os_id} {mg} {serial} art {art} | {remito} {cliente} | {empresa}")
    antes = stock(client, serial)
    print("   stock antes:", antes)
    if antes != [(art, dep, 1.0)]:
        print(f"   SALTEADO: se esperaba exactamente {art} {dep}=1")
        continue

    if dep == "STR":
        if APPLY:
            job = enqueue_stock_transfer_for_ingreso(os_id)
            print("   traslado STR->STL: job", job.get("id"), job.get("status"))
            print("   proceso:", process_bejerman_jobs(job_id=int(job["id"]), client=client))
            medio = stock(client, serial)
            print("   stock después del traslado:", medio)
            if medio != [(art, "STL", 1.0)]:
                print("   FRENADO: el traslado no dejó la unidad en STL; no se emite la SAL")
                continue
        else:
            print("   emitiría traslado STR->STL (job stock_str_to_stl)")

    payload = build_stock_correction_sal_payload(
        numero_serie=serial, articulo=art, deposito="STL", cantidad=1, run_id="ALQ20261013"
    )
    payload[0]["Comprobante_IdOrigen"] = f"NEXORA-ALQ-{remito.replace(' ', '')}-{serial}"[:50]
    print("   SAL STL:", payload[0]["Comprobante_Numero"], payload[0]["Comprobante_IdOrigen"])
    if not APPLY:
        continue
    print("   respuesta:", str(client.ingresar_lista_comprobantes_json(payload))[:300])
    despues = stock(client, serial)
    print("   stock después:", despues or "sin stock (OK)")
