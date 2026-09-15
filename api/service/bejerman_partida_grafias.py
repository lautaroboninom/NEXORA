"""Bejerman busca la partida distinguiendo mayúsculas, pero la guarda sin distinguirlas.

`ObtenerStockPartida` compara la partida tal cual llega: con `g04850336` no devuelve la fila
`G04850336`. SQL Server, en cambio, usa una intercalación que no distingue mayúsculas
(`SQL_Latin1_General_CP1_CI_AS`), así que un ENT con `g04850336` suma sobre la fila
`G04850336` que ya existía en ese depósito y crea una fila `g04850336` en uno donde no había.
La misma unidad queda repartida en filas con distinta grafía y cada consulta ve sólo una parte.

Pasó con MG 4771 (OS 29707, job 1306) el 11/09/2026: la SAL de STR y el ENT a STL entraron
bien, pero la fila de STL era la `G04850336` de 2024. La verificación preguntó por
`g04850336`, vio sólo la fila de STR en cero y el job se agotó reintentando un movimiento
que ya estaba hecho.

Por eso se consulta cada grafía posible —tal cual, en mayúsculas y en minúsculas— y se
juntan las filas. Una serie sin letras sigue siendo una sola consulta.

Sin imports de Django a propósito: se prueba con Python pelado.
"""

from __future__ import annotations

import json
from typing import Any, Callable


def grafias_de_partida(partida: Any) -> list[str]:
    """La partida tal cual llega, en mayúsculas y en minúsculas, sin repetir."""
    texto = "" if partida is None else str(partida)
    return list(dict.fromkeys((texto, texto.upper(), texto.lower())))


def _filas(datos: Any) -> list[dict[str, Any]] | None:
    """Las filas de `DatosJSON`, o `None` si no vino la lista que devuelve el SDK."""
    if isinstance(datos, list):
        valor: Any = datos
    else:
        texto = str(datos or "").strip()
        if not texto:
            return []
        try:
            valor = json.loads(texto)
        except ValueError:
            return None
    if not isinstance(valor, list):
        return None
    return [fila for fila in valor if isinstance(fila, dict)]


def stock_partida_sin_distinguir_mayusculas(
    consultar: Callable[[str], dict[str, Any]], partida: Any
) -> dict[str, Any]:
    """Ejecuta `ObtenerStockPartida` con cada grafía y devuelve una sola respuesta.

    La primera consulta es la de siempre (la partida tal cual), así que nunca se ven menos
    filas que antes. Si alguna respuesta no trae la lista esperada no se inventa una fusión:
    se devuelve la de la grafía original. Un error en cualquier grafía se propaga: ver sólo
    una parte del stock es justo lo que hace emitir un ENT sin su SAL.
    """
    respuestas = [consultar(grafia) for grafia in grafias_de_partida(partida)]
    principal = respuestas[0]
    if len(respuestas) == 1:
        return principal
    filas: list[dict[str, Any]] = []
    vistas: set[str] = set()
    for respuesta in respuestas:
        propias = _filas((respuesta or {}).get("DatosJSON"))
        if propias is None:
            return principal
        for fila in propias:
            clave = json.dumps(fila, sort_keys=True, ensure_ascii=False, default=str)
            if clave in vistas:
                continue
            vistas.add(clave)
            filas.append(fila)
    return {**principal, "DatosJSON": json.dumps(filas, ensure_ascii=False)}
