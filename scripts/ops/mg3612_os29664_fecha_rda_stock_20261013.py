"""MG 3612 (OS 29664, EDAN M3A, partida M16C11640002): duplicado en Bejerman y RDA pendiente.

Pedido del usuario 13/10/2026: "corregí este equipo que está duplicado en Bejerman".

Diagnóstico:
  - La OS tenía fecha_ingreso 0026-08-28 (la pantalla muestra "Invalid Date"). NEXORA usa esa fecha
    como fecha de emisión del RDA, así que la registración del RDA manual R 00001-00026451 (que existe
    en Bejerman desde el 28/08) fallaba con "inconsistencias en el archivo de texto"; el pedido guardado
    (bejerman_ingreso_remitos 552) también tenía issueDate 0026-08-28 y el reintento lo reutiliza.
  - Stock: el RDA entró la unidad a STR (+1) y el 14/09 NEXORA la pasó a STL con un ENT sin la SAL
    de STR (ENT -00229664): STL=1 y STR=1. El equipo está reparado en la Estantería de Alquiler, así
    que sobra la de STR.

Pasos (en este orden: la registración crea la partida con un ENT si no existe en STR, por eso la SAL
de STR va al final):
  1. fecha_ingreso de la OS e issueDate del pedido guardado -> 2026-08-28.
  2. register_ris_batch: registra el RDA; Bejerman responde "ya existe" y NEXORA lo adopta.
  3. SAL de STR (1 unidad), sólo si el stock sigue siendo STL=1 y STR=1.

Correr dentro de nexora-api: docker exec -i nexora-api python manage.py shell < este_archivo.py
Por defecto es una prueba; APPLY=1 aplica.
"""
import os

from django.db import connection, transaction

from service.bejerman_ris import register_ris_batch
from service.bejerman_stock_corrections import (
    _group_stock,
    _stock_rows_for_serial,
    build_stock_correction_sal_payload,
)
from service.bejerman_sync import BejermanSDKClient, validate_bejerman_config

APPLY = os.environ.get("APPLY") == "1"
OS_ID, ROW_ID, SERIAL, ART = 29664, 552, "M16C11640002", "1108006"
USER_ID = 2  # Lautaro (jefe)


def stock(client):
    groups = _group_stock(_stock_rows_for_serial(client.stock_by_deposit_partida("", SERIAL), SERIAL))
    return sorted((g["articulo"], g["deposito"], float(g["cantidad"])) for g in groups if float(g["cantidad"]) > 0)


validate_bejerman_config()
validate_bejerman_config(comprobante="BEJERMAN_STOCK_CORRECTION_COMPROBANTE")
client = BejermanSDKClient(company_key="SEPID", allow_system_credentials=True)
print("MODO:", "APLICAR" if APPLY else "PRUEBA (no escribe)")

with connection.cursor() as cur:
    cur.execute("SELECT fecha_ingreso::text FROM ingresos WHERE id=%s", [OS_ID])
    print("fecha_ingreso:", cur.fetchone()[0])
    cur.execute("SELECT status, request_payload->>'issueDate' FROM bejerman_ingreso_remitos WHERE id=%s", [ROW_ID])
    print("RDA en NEXORA:", cur.fetchone())
antes = stock(client)
print("stock antes:", antes)

if APPLY:
    with transaction.atomic(), connection.cursor() as cur:
        cur.execute(
            "UPDATE ingresos SET fecha_ingreso = '2026-08-28 00:00-03' "
            "WHERE id=%s AND EXTRACT(YEAR FROM fecha_ingreso) = 26",
            [OS_ID],
        )
        print("1) fecha de la OS corregida:", cur.rowcount)
        cur.execute(
            "UPDATE bejerman_ingreso_remitos "
            "SET request_payload = jsonb_set(request_payload, '{issueDate}', '\"2026-08-28\"'::jsonb) "
            "WHERE id=%s AND request_payload->>'issueDate' = '0026-08-28'",
            [ROW_ID],
        )
        print("1) issueDate del pedido corregida:", cur.rowcount)
    try:
        row = register_ris_batch([OS_ID], "RDA R 00001-00026451", user_id=USER_ID)
        print("2) registración:", row.get("status"), row.get("remito_number"), (row.get("last_error") or "")[:150])
    except Exception as exc:
        print("2) registración FALLÓ:", exc)

medio = stock(client)
print("stock después de registrar:", medio)
if medio != [(ART, "STL", 1.0), (ART, "STR", 1.0)]:
    print("3) SALTEADO: se esperaba STL=1 y STR=1")
else:
    payload = build_stock_correction_sal_payload(
        numero_serie=SERIAL, articulo=ART, deposito="STR", cantidad=1, run_id="DUP20261013"
    )
    payload[0]["Comprobante_IdOrigen"] = f"NEXORA-DUP-STR-OS{OS_ID}-{SERIAL}"
    print("3) SAL STR", payload[0]["Comprobante_Numero"], payload[0]["Comprobante_IdOrigen"])
    if APPLY:
        print("   respuesta:", str(client.ingresar_lista_comprobantes_json(payload))[:80])
        print("   stock final:", stock(client))
