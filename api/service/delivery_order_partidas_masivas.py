"""Carga masiva de partidas en un renglón de una orden de entrega: qué pasa con cada fila.

Es el recuadro de partidas del formulario para muchas unidades. En vez de escanear o elegir de a
una, se pega la columna de partidas (números de serie) de un Excel, o se sube la planilla, y esto
dice fila por fila si cada una se puede tomar del depósito antes de cargarla en el renglón. De a una
no se llegaba: la OE-00478 (40 Auto CPAP y 60 humidificadores desde SAR) terminó cargada a mano en
Bejerman.

No graba nada. El formulario agrega al renglón las partidas que pasan, y el guardado de la orden las
vuelve a validar contra Bejerman (`_validate_delivery_partidas_stock`; en alquiler,
`resolve_rental_item_partidas`). Las reglas de acá son las de ese guardado, adelantadas:

* stock positivo de **ese artículo** en ese depósito. La identidad de stock es partida + artículo:
  el CPAP y su humidificador comparten la serie, así que la partida que sólo tiene stock en otro
  artículo es un error y no una coincidencia;
* la misma partida no va dos veces para el mismo artículo, ni en la lista ni en otro renglón: el
  guardado sumaría las dos contra el mismo stock;
* en alquiler, además, que el NS se pueda alquilar (`rental_stock_lot_availability`).

A diferencia del recuadro, que valida contra el listado del artículo (con tope), acá se mira el
volcado entero del depósito: si una partida no aparece, no está.

Este módulo no toca la base ni Bejerman: todo lo que sabe le llega por parámetro. Las consultas
viven en `views/delivery_order_partidas_masivas_views.py`.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable

from .mg_codes import MG_CODE_RE

PARTIDAS_MASIVAS_MAX_FILAS = 500
ACCION_CARGAR = "cargar"
ACCION_YA_CARGADA = "ya_cargada"

# Excel muestra así los números largos de una columna con formato General ("2,1E+11"), y la
# partida original ya no se puede recuperar del archivo.
_NOTACION_CIENTIFICA_RE = re.compile(r"^\d+(?:[.,]\d+)?E[+-]\d+$", re.IGNORECASE)
# Número interno: el criterio único de `mg_codes`, que exige el prefijo, porque un número suelto
# puede ser una partida de verdad.
_NUMERO_INTERNO_RE = MG_CODE_RE


def _texto(value: Any) -> str:
    # Excel y las páginas web pegan espacios duros (U+00A0).
    return str(value if value is not None else "").replace(" ", " ").strip()


def clave_partida(value: Any) -> str:
    """Sin espacios ni guiones y en mayúsculas, como `_clave_serie` del alta masiva de equipos."""
    return re.sub(r"[\s-]+", "", _texto(value)).upper()


def _codigo_articulo(value: Any) -> str:
    """Igual que `normalize_article_code` del SDK."""
    return re.sub(r"[\s-]+", "", _texto(value)).upper()


def es_numero_interno(value: Any) -> bool:
    return bool(_NUMERO_INTERNO_RE.match(_texto(value)))


def _numero(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _cantidad_efectiva(row: dict[str, Any]) -> float:
    """La de `build_article_stock_lots`: la disponible y, si Bejerman no la informa, la real."""
    cantidad = _numero(row.get("availableQuantity"))
    if cantidad is None:
        cantidad = _numero(row.get("realQuantity"))
    return cantidad or 0.0


def _cantidad_texto(cantidad: float) -> str:
    return str(int(cantidad)) if float(cantidad).is_integer() else f"{cantidad:g}".replace(".", ",")


def leer_filas(raw: Any) -> list[dict[str, Any]]:
    """`[{fila, partida}]` tal como las manda el formulario.

    `fila` es el renglón de la lista que ve el operador: cada error le señala la línea que tiene
    que corregir, y es lo que permite sacar del texto las filas con error.
    """
    filas = []
    for index, entry in enumerate(raw if isinstance(raw, list) else [], start=1):
        if isinstance(entry, dict):
            partida, fila = entry.get("partida"), entry.get("fila")
        else:
            partida, fila = entry, None
        try:
            fila = int(fila)
        except (TypeError, ValueError):
            fila = index
        filas.append({"fila": fila, "partida": _texto(partida)})
    return filas


def leer_cargadas(raw: Any) -> list[dict[str, Any]]:
    """Las partidas del mismo artículo que la orden ya tiene: `[{partida, renglon, actual}]`.

    `actual` marca las del renglón donde se hace la carga, que no se repiten pero tampoco son un
    error: pegar la lista entera otra vez tiene que dar lo mismo.
    """
    cargadas = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict) or not _texto(entry.get("partida")):
            continue
        try:
            renglon = int(entry.get("renglon"))
        except (TypeError, ValueError):
            renglon = None
        cargadas.append(
            {"partida": _texto(entry.get("partida")), "renglon": renglon, "actual": bool(entry.get("actual"))}
        )
    return cargadas


def _stock_por_clave(stock_rows: Iterable[dict[str, Any]], deposito: str) -> dict[str, list[dict[str, Any]]]:
    """Las filas con stock positivo del depósito, de todos los artículos, por partida normalizada.

    Las de otros artículos no se pueden cargar, pero explican por qué la partida no está.
    """
    por_clave: dict[str, list[dict[str, Any]]] = {}
    for row in stock_rows or []:
        partida = _texto(row.get("partida"))
        if not partida or _texto(row.get("depositCode")).upper() != deposito:
            continue
        if _cantidad_efectiva(row) <= 0:
            continue
        por_clave.setdefault(clave_partida(partida), []).append(row)
    return por_clave


def _buscar_en_stock(texto, *, articulo, deposito, por_clave, series_por_numero_interno):
    """Dónde está la partida escrita: (fila de stock del artículo, artículos donde sí está, errores, avisos)."""
    errores: list[str] = []
    avisos: list[str] = []
    buscada = texto
    candidatos = por_clave.get(clave_partida(texto)) or []
    if not candidatos and es_numero_interno(texto):
        series = [s for s in dict.fromkeys(_texto(s) for s in series_por_numero_interno.get(texto) or []) if s]
        if len(series) > 1:
            errores.append(f"{texto} está cargado en más de un equipo ({', '.join(series)}): cargá la serie.")
            return None, [], errores, avisos
        if series:
            buscada = series[0]
            candidatos = por_clave.get(clave_partida(buscada)) or []
            avisos.append(f"{texto} es el equipo de serie {buscada}.")

    del_articulo = [row for row in candidatos if _codigo_articulo(row.get("articleCode")) == articulo]
    if not del_articulo:
        otros = sorted({_texto(row.get("articleCode")) for row in candidatos if _texto(row.get("articleCode"))})
        if not otros:
            errores.append(f"No tiene stock en {deposito}.")
        return None, otros, errores, avisos

    # La coincidencia exacta gana; la normalizada (espacios, guiones) sirve si es una sola.
    exactas = [row for row in del_articulo if _texto(row.get("partida")).casefold() == buscada.casefold()]
    elegibles = exactas or del_articulo
    textos = sorted({_texto(row.get("partida")) for row in elegibles})
    if len(textos) > 1:
        errores.append(
            f"Coincide con más de una partida de {deposito} ({', '.join(textos)}): escribila tal como "
            "figura en Bejerman."
        )
        return None, [], errores, avisos
    if textos[0].casefold() != buscada.casefold():
        avisos.append(f"Se carga como {textos[0]}, que es como figura en Bejerman.")
    return elegibles[0], [], errores, avisos


def evaluar_partidas(
    filas: list[dict[str, Any]],
    *,
    article_code: str,
    deposit_code: str,
    stock_rows: Iterable[dict[str, Any]],
    cargadas: Iterable[dict[str, Any]] = (),
    series_por_numero_interno: dict[str, list[str]] | None = None,
    descripciones: Callable[[list[str]], dict[str, str]] | None = None,
    disponibilidad_alquiler: Callable[[list[str]], list[dict[str, Any] | None]] | None = None,
    vencimiento: Callable[[str, str], Any] | None = None,
    checked_at: str = "",
) -> dict[str, Any]:
    """Qué pasa con cada fila. Función pura: lo que sabe de la base y de Bejerman le llega.

    * `stock_rows`: el volcado del depósito (`bejerman_catalog_cache.stock_rows`).
    * `series_por_numero_interno`: para cada MG de la lista, las series de los equipos que lo
      tienen. Bejerman sólo conoce la serie como partida, pero el operador suele tener el MG.
    * `descripciones`: códigos de artículo → descripción, para explicar una partida que está en
      otro artículo. Se llama sólo si hace falta.
    * `disponibilidad_alquiler`: sólo en alquiler, el veredicto de `rental_stock_lot_availability`
      para cada serie, en el mismo orden.
    """
    articulo = _codigo_articulo(article_code)
    deposito = _texto(deposit_code).upper()
    por_clave = _stock_por_clave(stock_rows, deposito)

    cargadas_por_clave: dict[str, dict[str, Any]] = {}
    for cargada in cargadas or []:
        clave = clave_partida(cargada.get("partida"))
        previa = cargadas_por_clave.get(clave)
        # Si también está en otro renglón, manda la del propio: es la que ya se ve cargada.
        if clave and (previa is None or (cargada.get("actual") and not previa.get("actual"))):
            cargadas_por_clave[clave] = cargada

    resueltas = []
    for fila in filas:
        texto = _texto(fila.get("partida"))
        resuelta = {"fila": fila.get("fila"), "texto": texto, "lote": None, "otros": [], "errores": [], "avisos": []}
        if not texto:
            resuelta["errores"].append("Falta la partida.")
        elif _NOTACION_CIENTIFICA_RE.match(texto.replace(" ", "")):
            resuelta["errores"].append(
                "La partida quedó en notación científica (Excel la convirtió en número): cargala con la "
                "columna en formato Texto."
            )
        else:
            resuelta["lote"], resuelta["otros"], resuelta["errores"], resuelta["avisos"] = _buscar_en_stock(
                texto,
                articulo=articulo,
                deposito=deposito,
                por_clave=por_clave,
                series_por_numero_interno=series_por_numero_interno or {},
            )
        resueltas.append(resuelta)

    otros_articulos = sorted({code for resuelta in resueltas for code in resuelta["otros"]})
    etiquetas = (descripciones(otros_articulos) or {}) if otros_articulos and descripciones else {}

    def etiqueta(code: str) -> str:
        descripcion = _texto(etiquetas.get(code))
        return f"{code} ({descripcion})" if descripcion else code

    primera_fila: dict[str, Any] = {}
    for resuelta in resueltas:
        if resuelta["otros"]:
            articulos = ", ".join(etiqueta(code) for code in resuelta["otros"])
            cual = "los artículos" if len(resuelta["otros"]) > 1 else "el artículo"
            resuelta["errores"].append(
                f"En {deposito} la partida tiene stock en {cual} {articulos}, no en el {_texto(article_code)}."
            )
        lote = resuelta["lote"]
        resuelta["partida"] = _texto(lote.get("partida")) if lote else ""
        resuelta["accion"] = ""
        clave = clave_partida(resuelta["partida"] or resuelta["texto"])
        if not clave:
            continue
        if clave in primera_fila:
            resuelta["errores"].append(f"Repetida: ya está en la fila {primera_fila[clave]}.")
            continue
        primera_fila[clave] = resuelta["fila"]
        cargada = cargadas_por_clave.get(clave)
        if cargada and cargada.get("actual"):
            resuelta["accion"] = ACCION_YA_CARGADA
            resuelta["avisos"].append("Ya estaba cargada en este renglón: no se agrega de nuevo.")
        elif cargada:
            renglon = cargada.get("renglon")
            resuelta["errores"].append(f"Ya está cargada en el renglón {renglon}." if renglon else "Ya está cargada en otro renglón.")
        elif lote and _cantidad_efectiva(lote) < 1:
            # Cada partida de la lista se carga como una unidad.
            resuelta["errores"].append(
                f"Tiene stock {_cantidad_texto(_cantidad_efectiva(lote))} en {deposito}: no alcanza para una unidad."
            )

    a_cargar = [r for r in resueltas if r["lote"] and not r["errores"] and r["accion"] != ACCION_YA_CARGADA]
    veredictos: dict[int, dict[str, Any]] = {}
    if disponibilidad_alquiler and a_cargar:
        for resuelta, veredicto in zip(a_cargar, disponibilidad_alquiler([r["partida"] for r in a_cargar])):
            veredictos[id(resuelta)] = veredicto or {}
            if veredicto and veredicto.get("available") is False:
                resuelta["errores"].append(
                    _texto(veredicto.get("reason")) or f"El NS {resuelta['partida']} no está disponible para alquilar."
                )

    resultado = []
    for resuelta in resueltas:
        errores = resuelta["errores"]
        lote = resuelta["lote"]
        accion = "" if errores or not lote else (resuelta["accion"] or ACCION_CARGAR)
        resultado.append(
            {
                "fila": resuelta["fila"],
                "partida": resuelta["texto"],
                "estado": "error" if errores else "ok",
                "accion": accion,
                "errores": errores,
                "avisos": resuelta["avisos"],
                "lote": (
                    {
                        "partida": resuelta["partida"],
                        "articleCode": _texto(lote.get("articleCode")),
                        "stockDepositCode": _texto(lote.get("depositCode")).upper() or deposito,
                        "stockAvailableQuantity": _cantidad_efectiva(lote),
                        "partidaExpirationDate": (vencimiento(deposito, resuelta["partida"]) if vencimiento else None) or None,
                        "stockCheckedAt": checked_at or None,
                        "equipmentInternalNumber": _texto(veredictos.get(id(resuelta), {}).get("equipmentInternalNumber")),
                    }
                    if accion
                    else None
                ),
            }
        )

    ok = [fila for fila in resultado if fila["estado"] == "ok"]
    return {
        "filas": resultado,
        "resumen": {
            "total": len(resultado),
            "ok": len(ok),
            "con_error": len(resultado) - len(ok),
            "cargar": sum(1 for fila in ok if fila["accion"] == ACCION_CARGAR),
            "ya_cargadas": sum(1 for fila in ok if fila["accion"] == ACCION_YA_CARGADA),
            "con_aviso": sum(1 for fila in resultado if fila["avisos"]),
        },
    }


__all__ = [
    "ACCION_CARGAR",
    "ACCION_YA_CARGADA",
    "PARTIDAS_MASIVAS_MAX_FILAS",
    "clave_partida",
    "es_numero_interno",
    "evaluar_partidas",
    "leer_cargadas",
    "leer_filas",
]
