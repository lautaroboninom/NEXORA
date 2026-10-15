"""Lotes de insumos vendidos con RT emitidos con ActualizaStock=N: la cantidad vendida nunca salió de VAL.

Diagnóstico 13/10/2026 (SBDASEP). Se corrigen sólo los lotes donde la cuenta es inequívoca:
  A) sin ningún movimiento posterior al RT y con VAL suficiente (RT 5321 y RT 5453);
  B) con movimientos posteriores pero sin ajustes de inventario y con VAL suficiente: como todo lo
     posterior está registrado, VAL sigue inflado exactamente en lo vendido.
Quedan para conteo físico: D2507112G (12 vendidas, VAL=1), D2507112F y D2504231E (ya en 0, con ajustes),
D2504232E (ENT +3 el día del RT), D2607031U (16 vendidas, VAL=15) y los renglones sin partida (art.
1203007 del RT 5453 y los 3 RT de MG BIO).

**UM2:** estos insumos se cuentan en cajas en la segunda unidad (`Articulos.art_FactorConv`: 16 a 48 por
caja). La SAL lleva UM2 = UM1 / factor redondeado a 3 decimales, como los RT. Una primera corrida mandó
UM2 = UM1 (como `build_stock_correction_sal_payload`) y Bejerman rechazó 10 de 11 por "no hay stock
suficiente para el artículo"; la que entró (D2507113F) descontó 2 cajas en vez de 0,111: se revierte con
un ENT idéntico y se rehace bien (REPARAR).

Correr dentro de nexora-api: docker exec -i nexora-api python manage.py shell < este_archivo.py
Por defecto es una prueba; APPLY=1 emite. Exige que VAL tenga exactamente lo medido.
"""
import os

from service.bejerman_stock_corrections import (
    _group_stock,
    _stock_rows_for_serial,
    build_stock_correction_sal_payload,
)
from service.bejerman_sync import BejermanSDKClient, validate_bejerman_config

APPLY = os.environ.get("APPLY") == "1"

# partida, artículo, cantidad a sacar de VAL, VAL medido (UM1), factor UM2, comprobante(s)
CASOS = [
    ("D2507114E", "1201008", 14, 14, 48, "RT 5321"),
    ("D2504171E", "1201008", 1, 1, 48, "RT 5321"),
    ("21091737", "1202007", 10, 48, 16, "RT 5321"),
    ("D2607032T", "1201036", 35, 91, 35, "RT 5453"),
    ("D2607031C", "1201018", 29, 57, 29, "RT 5453"),
    ("D2504233F", "1201012", 2, 2, 18, "RT 5453"),
    ("D25042427", "1202009", 17, 17, 16, "RT 4906+5321"),
    ("D25042417", "1202008", 14, 44, 16, "RT 5321"),
    ("D2507113G", "1202011", 4, 4, 20, "RT 5090"),
    ("D2507115F", "1201012", 1, 1, 18, "RT 4906"),
]
# Cuando la SAL vacía la partida, UM2 = la UM2 que la partida tiene en VAL (medida el 13/10): por los
# redondeos acumulados de los RT, cantidad/factor puede superarla (D25042427: 1,062 contra 1,054) y
# Bejerman la rechazaría. Así la partida queda en cero en las dos unidades.
UM2_AL_VACIAR = {
    "D2507114E": 0.29, "D2504171E": 0.016, "D2504233F": 0.106,
    "D25042427": 1.054, "D2507113G": 0.2, "D2507115F": 0.053,
}
# La SAL 95546883 de la primera corrida: UM1 -2 y UM2 -2. Se revierte y se rehace con UM2 -0,111.
REPARAR = ("D2507113F", "1201010", 2, 38, 18, "RT 5453")


def val_qty(client, serial, art):
    groups = _group_stock(_stock_rows_for_serial(client.stock_by_deposit_partida("", serial), serial))
    return sum(float(g["cantidad"]) for g in groups if g["articulo"] == art and g["deposito"] == "VAL")


def movimiento(serial, art, um1, um2, tipo, origen, seq):
    payload = build_stock_correction_sal_payload(
        numero_serie=serial, articulo=art, deposito="VAL", cantidad=abs(um1), run_id=f"INS2{origen}", sequence=seq
    )
    payload[0]["Comprobante_Tipo"] = tipo
    payload[0]["Comprobante_CantidadUM1"] = um1
    payload[0]["Comprobante_CantidadUM2"] = um2
    payload[0]["Comprobante_IdOrigen"] = f"NEXORA-{origen}-{serial}"[:50]
    return payload


def emitir(client, payload):
    print("  ", payload[0]["Comprobante_Tipo"], payload[0]["Comprobante_Numero"], payload[0]["Comprobante_IdOrigen"],
          "UM1", payload[0]["Comprobante_CantidadUM1"], "UM2", payload[0]["Comprobante_CantidadUM2"])
    if not APPLY:
        return True
    try:
        print("   respuesta:", str(client.ingresar_lista_comprobantes_json(payload))[:60])
        return True
    except Exception as exc:
        print("   RECHAZADO:", str(exc)[:200])
        return False


validate_bejerman_config()
validate_bejerman_config(comprobante="BEJERMAN_STOCK_CORRECTION_COMPROBANTE")
client = BejermanSDKClient(company_key="SEPID", allow_system_credentials=True)
print("MODO:", "APLICAR" if APPLY else "PRUEBA (no escribe)")

serial, art, qty, medido, factor, ref = REPARAR
antes = val_qty(client, serial, art)
print(f"\n== REPARAR {serial} art {art} | VAL ahora {antes:g} (medido {medido})")
if antes == medido:
    if emitir(client, movimiento(serial, art, qty, qty, "ENT", "INSREV-VAL-RT5453", 1)):
        emitir(client, movimiento(serial, art, -qty, -round(qty / factor, 3), "SAL", "INSOK-VAL-RT5453", 2))
    if APPLY:
        print("   VAL después:", val_qty(client, serial, art))
else:
    print("   SALTEADO: VAL cambió desde la medición")

for serial, art, qty, medido, factor, ref in CASOS:
    antes = val_qty(client, serial, art)
    print(f"\n== {serial} art {art} | {ref} | sacar {qty} de VAL (UM2 {round(qty / factor, 3)}) | VAL ahora {antes:g} (medido {medido})")
    if antes != medido:
        print("   SALTEADO: VAL cambió desde la medición")
        continue
    origen = "INSOK-VAL-" + ref.replace(" ", "").replace("+", "-")
    um2 = UM2_AL_VACIAR[serial] if qty == medido and serial in UM2_AL_VACIAR else round(qty / factor, 3)
    emitir(client, movimiento(serial, art, -qty, -um2, "SAL", origen, 1))
    if APPLY:
        print("   VAL después:", val_qty(client, serial, art))
