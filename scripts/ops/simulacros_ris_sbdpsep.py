"""Simulacros de emisión de RIS contra la empresa de PRUEBA de Bejerman (SBDPSEP).

Prueba previa a todo deploy que toque el circuito del RIS (15/10/2026). Emite RIS reales en la
empresa de prueba con el código del árbol y cubre: partida nueva, partida en 0 en STR, partida
sólo en VAL, partida con stock en STR, lote, CPAP + humidificador (nuevos, en 0 y mixto),
registración manual, artículo sin fila de stock en STR y un control con el signo viejo (+1),
que tiene que fallar con "No hay stock suficiente".

Cómo correrlo (desde la raíz del repo, con el stack de dev levantado):
    tar -cf - --exclude=__pycache__ -C api manage.py app service | docker exec -i nexora-dev-api-1         sh -c 'rm -rf /tmp/nxtest && mkdir -p /tmp/nxtest && tar -xf - -C /tmp/nxtest'
    docker cp scripts/ops/simulacros_ris_sbdpsep.py nexora-dev-api-1:/tmp/nxtest/simulacros_ris.py
    docker exec -e NX_BASE=<ingreso base sin usar> -w /tmp/nxtest nexora-dev-api-1         sh -c 'python manage.py shell < simulacros_ris.py'
NX_BASE tiene que cambiar en cada corrida: Bejerman descarta los ENT con un IdOrigen repetido.
Después verificar por SQL en SBDPSEP (scripts/samtronic_sql.py) que cada partida NXSIM-* quedó con
STR=1 y el renglón del RIS en -1, y borrar /tmp/nxtest. Aborta solo si el WSDL no es el de prueba.
"""
import json
import time
import traceback
from unittest.mock import patch

from django.conf import settings
from django.test import override_settings
from django.utils import timezone

from service import bejerman_ris
from service.bejerman_sdk import BejermanSDKClient as ClienteReal
from service.bejerman_sdk import is_ok_response, sdk_signed_article_quantity
from service.management.commands.smoke_bejerman_remitos_test import _find_free_manual_number

if "WS-TEST" not in settings.BEJERMAN_WSDL_URL.upper():
    raise SystemExit("ABORTADO: el WSDL no es el SDK de prueba")
print("WSDL:", settings.BEJERMAN_WSDL_URL)
print("signo RIS con el código nuevo:", sdk_signed_article_quantity("RIS"))

TS = time.strftime("%H%M%S")
HOY = timezone.localdate().isoformat()
POLIGRAFO = ("1110001", "Polígrafo portátil Polywatch YH-600B Pro")
CPAP = ("1105013", "Auto CPAP YAMIND DM26-20A")
HUMIDIFICADOR = ("1111009", "Calentador humidificador YAMIND DM26-20C/A")
VIDEOLARINGOSCOPIO = ("1120002", "Videolaringoscopio CORERAY CR-31")
BOMBA = ("1113005", "Bomba de alimentación PATROL")
_ingreso = [int(__import__("os").environ.get("NX_BASE", "99970"))]
_mov = [0]
resultados = []


def cliente():
    return ClienteReal(company_key="SEPID", allow_system_credentials=True)


def nuevo_ingreso():
    _ingreso[0] += 1
    return _ingreso[0]


def partida(n):
    return f"NXSIM-{TS}-{n}"


def mov_stock(tipo, part, articulo, deposito, cantidad):
    """Condición previa: ENT/SAL por el circuito STOCK, igual que los arma NEXORA."""
    _mov[0] += 1
    numero = f"97{int(TS) % 10000:04d}{_mov[0]:02d}"
    item = {
        "Comprobante_FechaEmision": HOY,
        "Comprobante_Tipo": tipo,
        "Comprobante_Letra": "",
        "Comprobante_PtoVenta": "",
        "Comprobante_Numero": numero,
        "Comprobante_Art_CodGen": articulo,
        "Comprobante_ArtPartida": part,
        "Comprobante_DescArticulo": f"SIMULACRO {part}"[:50],
        "Comprobante_ArtCodTipo": "1",
        "Comprobante_ArtDeposito": deposito,
        "Comprobante_CantidadUM1": cantidad,
        "Comprobante_CantidadUM2": cantidad,
        "Comprobante_PrecioTotalMonLocal": 0,
        "Comprobante_IdOrigen": f"NXSIM-PRE-{numero}-{part}"[:50],
    }
    respuesta = cliente().ingresar_lista_comprobantes_stock_json([item], numera_flex="N")
    if not is_ok_response(respuesta):
        raise RuntimeError(f"precondición {tipo} {part} {articulo} {deposito}: {respuesta}")


def equipo(part, articulo, ingreso_id):
    return {
        "ingresoId": ingreso_id,
        "osLabel": str(ingreso_id),
        "articleCode": articulo[0],
        "articleName": articulo[1],
        "serial": part,
        "equipmentType": "SIMULACRO",
        "brand": "SIM",
        "model": "SIM",
        "repairReason": "Simulacro de RIS",
    }


def payload_ris(equipos, *, cliente_codigo="DIA", cliente_nombre="DIAGNOSTIC MEDIC S.A."):
    ingreso_ids = [e["ingresoId"] for e in equipos]
    payload = {
        "requestId": f"simulacro-ris-{TS}-{'-'.join(map(str, ingreso_ids))}",
        "ingresoId": ingreso_ids[0],
        "ingresoIds": ingreso_ids,
        "companyKey": "SEPID",
        "companyLabel": "SEPID SA",
        "bejermanCompany": "SEP",
        "issueDate": HOY,
        "customerCode": cliente_codigo,
        "customerName": cliente_nombre,
        "sellerCode": "ADM",
        "paymentTermCode": "30",
        "documentProfile": {
            "key": "ris", "type": "RIS", "label": "RIS", "letter": "R",
            "reason": "Simulacro de ingreso a servicio técnico.",
            "deposit": "STR", "operation": "REP", "pointOfSale": "00004", "updateStock": True,
        },
        "equipment": equipos[0],
    }
    if len(equipos) > 1:
        payload["equipments"] = equipos
    return payload


def con_sdk_de_prueba():
    return (
        patch("service.bejerman_ris.BejermanSDKClient",
              side_effect=lambda *a, **k: ClienteReal(*a, **{**k, "allow_system_credentials": True})),
        patch("service.bejerman_ris._ensure_equipment_remito_not_duplicated"),
    )


def escenario(nombre, esperado, partidas, accion):
    print(f"\n=== {nombre}\n    esperado: {esperado}")
    fila = {"escenario": nombre, "esperado": esperado, "partidas": partidas}
    try:
        request_payload, response = accion()
        lineas = [i for i in request_payload["comprobante"]["Comprobante_Items"] if i["Item_Tipo"] == "A"]
        fila.update(
            resultado="OK",
            remito=response.get("remitoNumber"),
            actualiza_stock=request_payload["comprobante"].get("Comprobante_ActualizaStock"),
            cantidades=[ln.get("Item_CantidadUM1") for ln in lineas],
            recuperado=bool(request_payload.get("partidaRejectionRecovered")),
            neutralizado=bool(request_payload.get("partidaNeutralized")),
        )
    except Exception as exc:
        fila.update(resultado="ERROR", tipo_error=type(exc).__name__, error=str(exc)[:300])
    print("    ->", json.dumps({k: v for k, v in fila.items() if k not in ("escenario", "esperado")}, ensure_ascii=False))
    resultados.append(fila)


def emitir(payload):
    a, b = con_sdk_de_prueba()
    with a, b:
        return bejerman_ris._emit_payload_to_bejerman(payload)


# 1. Partida nueva: no existe en ningún depósito.
p = partida(1)
escenario("1 partida nueva", "Partida inexistente -> ENT/SAL -> RIS entra con +1; STR=1", [p],
          lambda: emitir(payload_ris([equipo(p, POLIGRAFO, nuevo_ingreso())])))

# 2. Partida en cero en STR (OS 29879-29881 de hoy).
p = partida(2)
mov_stock("ENT", p, POLIGRAFO[0], "STR", 1)
mov_stock("SAL", p, POLIGRAFO[0], "STR", -1)
escenario("2 partida en 0 en STR", "entra al primer intento, sin ENT/SAL; STR=1", [p],
          lambda: emitir(payload_ris([equipo(p, POLIGRAFO, nuevo_ingreso())])))

# 3. Partida sólo en VAL con cero (equipo vendido al cliente, OS 29878).
p = partida(3)
mov_stock("ENT", p, POLIGRAFO[0], "VAL", 1)
mov_stock("SAL", p, POLIGRAFO[0], "VAL", -1)
escenario("3 partida sólo en VAL (vendida)", "Partida inexistente en STR -> ENT/SAL -> RIS +1; STR=1, VAL=0", [p],
          lambda: emitir(payload_ris([equipo(p, POLIGRAFO, nuevo_ingreso())])))

# 4. Partida que ya tiene stock en STR (dato inconsistente previo).
p = partida(4)
mov_stock("ENT", p, POLIGRAFO[0], "STR", 1)
escenario("4 partida con stock en STR", "para documentar: el RIS suma otra unidad (STR=2)", [p],
          lambda: emitir(payload_ris([equipo(p, POLIGRAFO, nuevo_ingreso())])))

# 5. Lote con dos equipos distintos, partidas nuevas.
p5a, p5b = partida("5a"), partida("5b")
escenario("5 lote de dos equipos", "ENT/SAL de las dos -> RIS con dos renglones +1; STR=1 cada una", [p5a, p5b],
          lambda: emitir(payload_ris([equipo(p5a, POLIGRAFO, nuevo_ingreso()), equipo(p5b, POLIGRAFO, nuevo_ingreso())])))

# 6. CPAP + humidificador con la misma partida, nuevos.
p = partida(6)
escenario("6 CPAP + humidificador nuevos", "ENT/SAL por artículo -> RIS +1 en cada artículo", [p],
          lambda: emitir(payload_ris([equipo(p, CPAP, nuevo_ingreso()), equipo(p, HUMIDIFICADOR, nuevo_ingreso())])))

# 7. CPAP + humidificador en cero en STR (estado del lote OS 29880/29881 de hoy).
p = partida(7)
for art in (CPAP, HUMIDIFICADOR):
    mov_stock("ENT", p, art[0], "STR", 1)
    mov_stock("SAL", p, art[0], "STR", -1)
escenario("7 CPAP + humidificador en 0 en STR", "entra al primer intento; +1 en cada artículo", [p],
          lambda: emitir(payload_ris([equipo(p, CPAP, nuevo_ingreso()), equipo(p, HUMIDIFICADOR, nuevo_ingreso())])))

# 8. CPAP en cero en STR y humidificador inexistente.
p = partida(8)
mov_stock("ENT", p, CPAP[0], "STR", 1)
mov_stock("SAL", p, CPAP[0], "STR", -1)
escenario("8 CPAP en 0 + humidificador inexistente", "Partida inexistente -> ENT/SAL de los dos -> RIS +1 en cada artículo", [p],
          lambda: emitir(payload_ris([equipo(p, CPAP, nuevo_ingreso()), equipo(p, HUMIDIFICADOR, nuevo_ingreso())])))

# 9. Registración manual de un RIS (punto 00001, número libre alto).
p = partida(9)


def registrar():
    a, b = con_sdk_de_prueba()
    with a, b, patch("service.bejerman_ris._local_registered_ris_duplicate", return_value=None):
        numero = _find_free_manual_number(cliente())
        payload = payload_ris([equipo(p, POLIGRAFO, nuevo_ingreso())])
        payload["documentProfile"] = {**payload["documentProfile"], "pointOfSale": "00001", "updateStock": False}
        return bejerman_ris._register_payload_to_bejerman(payload, f"RIS R 00001-{numero}", payload["ingresoIds"])


escenario("9 registración manual", "ENT +1 previo y RIS con ActualizaStock=N; STR=1", [p], registrar)


# 11. Artículo que nunca tuvo fila de stock en STR (el videolaringoscopio de la OS 29470, 02/07).
p = partida(11)
escenario("11 artículo sin fila de stock en STR", "No hay stock ingresado -> ENT/SAL -> RIS +1; STR=1", [p],
          lambda: emitir(payload_ris([equipo(p, VIDEOLARINGOSCOPIO, nuevo_ingreso())])))

# 12. Lote: un artículo sin fila en STR y otro normal, partidas nuevas.
p12a, p12b = partida("12a"), partida("12b")
escenario("12 lote con artículo sin fila en STR", "recupera las dos partidas -> RIS +1 en cada una", [p12a, p12b],
          lambda: emitir(payload_ris([equipo(p12a, BOMBA, nuevo_ingreso()), equipo(p12b, POLIGRAFO, nuevo_ingreso())])))

# 10. Control: el signo viejo (+1) con el código actual, como quedó producción el 14/10.
p = partida(10)
mov_stock("ENT", p, POLIGRAFO[0], "STR", 1)
mov_stock("SAL", p, POLIGRAFO[0], "STR", -1)


def control():
    with override_settings(BEJERMAN_RIS_NEGATIVE_SDK_QUANTITY_TYPES="RD,RDN,RDA"):
        return emitir(payload_ris([equipo(p, POLIGRAFO, nuevo_ingreso())]))


escenario("10 CONTROL signo viejo +1", "falla con 'No hay stock suficiente' (lo que pasó en producción)", [p], control)

print("\nRESUMEN_JSON " + json.dumps(resultados, ensure_ascii=False))
