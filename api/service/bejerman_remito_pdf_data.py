"""Completa el comprobante con los datos que el PDF muestra pero la emisión no devuelve.

`IngresarComprobanteJSON` responde con el número de comprobante y los renglones tal
como se enviaron, pero el PDF que arma Bejerman muestra dos columnas más, que resuelve
por su cuenta contra sus maestros:

* **PM** — `dbo.Articulos.art_DescRedu` del artículo. Verificado contra el papel
  escaneado: 1204001 -> `2046-10`, 1205013 -> `2046-12`.
* **VTO.** — el vencimiento de la partida. Éste **ya lo tiene NEXORA**: lo guarda en
  `delivery_order_items.partida_expiration_date` cuando se arma la orden de entrega,
  junto con la partida. Por eso se busca primero en casa y sólo se sale a Bejerman
  por lo que falte.

Nada de esto puede fallar el remito: si no se consigue un dato, la columna sale vacía
y el PDF se entrega igual, que es justamente el problema que estamos resolviendo.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from django.db import connection

from .bejerman_remito_pdf import PM_KEYS, VTO_KEYS


logger = logging.getLogger(__name__)

# El maestro de artículos casi no se mueve y un remito repite códigos, así que un
# cache corto en memoria del worker evita repetir la consulta por SSH.
_PM_CACHE: dict[tuple[str, str], tuple[float, str]] = {}
PM_CACHE_TTL_SECONDS = 900


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _article_codes(items: list[dict[str, Any]]) -> list[str]:
    codes = []
    for item in items:
        code = _clean(item.get("Item_CodigoArticulo"))
        if code and code not in codes:
            codes.append(code)
    return codes


def _partidas(items: list[dict[str, Any]]) -> list[str]:
    partidas = []
    for item in items:
        partida = _clean(item.get("Item_Partida"))
        if partida and partida not in partidas:
            partidas.append(partida)
    return partidas


def expiration_dates_from_nexora(partidas: list[str], article_codes: list[str]) -> dict[str, Any]:
    """Vencimientos por partida, de las órdenes de entrega que ya los cargaron.

    Se prioriza la carga más reciente: la misma partida puede haberse tipeado en
    varias órdenes y la última es la que vale.
    """

    if not partidas:
        return {}
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (UPPER(TRIM(partida)))
                   UPPER(TRIM(partida)) AS partida,
                   partida_expiration_date
              FROM delivery_order_items
             WHERE partida_expiration_date IS NOT NULL
               AND UPPER(TRIM(partida)) = ANY(%s)
               AND (%s = '{}'::text[] OR COALESCE(article_code, '') = ANY(%s))
             ORDER BY UPPER(TRIM(partida)), updated_at DESC
            """,
            [
                [p.upper() for p in partidas],
                article_codes,
                article_codes,
            ],
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def pm_from_bejerman(article_codes: list[str], *, company_key: Any = None) -> dict[str, str]:
    """PM (`art_DescRedu`) de cada artículo, leído del maestro de Bejerman."""

    if not article_codes:
        return {}
    company = _clean(company_key).upper() or "SEPID"
    ahora = time.monotonic()
    resultado: dict[str, str] = {}
    faltantes: list[str] = []
    for code in article_codes:
        cacheado = _PM_CACHE.get((company, code))
        if cacheado and ahora - cacheado[0] < PM_CACHE_TTL_SECONDS:
            resultado[code] = cacheado[1]
        else:
            faltantes.append(code)
    if not faltantes:
        return resultado

    # Import local: `bejerman_delivery` importa un montón de cosas y este módulo se
    # usa desde el worker de PDF, que no necesita arrastrar todo eso salvo acá.
    from .bejerman_delivery import _remote_bejerman_sql_csv, _sql_in

    sql = f"""
SET NOCOUNT ON;

SELECT LTRIM(RTRIM(COALESCE(a.art_CodGen, '') + COALESCE(a.art_CodEle1, '')
             + COALESCE(a.art_CodEle2, '') + COALESCE(a.art_CodEle3, ''))) AS Codigo,
       LTRIM(RTRIM(COALESCE(a.art_DescRedu, ''))) AS PM
  FROM dbo.Articulos a
 WHERE LTRIM(RTRIM(COALESCE(a.art_CodGen, '') + COALESCE(a.art_CodEle1, '')
             + COALESCE(a.art_CodEle2, '') + COALESCE(a.art_CodEle3, ''))) IN ({_sql_in(faltantes)});
""".strip()

    try:
        filas = _remote_bejerman_sql_csv(sql, company_key=company, timeout_seconds=25)
    except Exception:
        logger.warning("bejerman_remito_pdf_pm_lookup_failed", extra={"company_key": company}, exc_info=True)
        return resultado

    for fila in filas or []:
        codigo, pm = _clean(fila.get("Codigo")), _clean(fila.get("PM"))
        if codigo:
            resultado[codigo] = pm
            _PM_CACHE[(company, codigo)] = (ahora, pm)
    return resultado


def enrich_comprobante_for_pdf(comprobante: dict[str, Any], *, company_key: Any = None) -> dict[str, Any]:
    """Devuelve una copia del comprobante con PM y VTO cargados en cada renglón.

    Nunca levanta: lo que no se consigue queda vacío y el remito sale igual.
    """

    if not isinstance(comprobante, dict):
        return comprobante
    items = [item for item in (comprobante.get("Comprobante_Items") or []) if isinstance(item, dict)]
    if not items:
        return comprobante

    codes, partidas = _article_codes(items), _partidas(items)

    try:
        vencimientos = expiration_dates_from_nexora(partidas, codes)
    except Exception:
        logger.warning("bejerman_remito_pdf_vto_lookup_failed", exc_info=True)
        vencimientos = {}

    try:
        pms = pm_from_bejerman(codes, company_key=company_key)
    except Exception:
        logger.warning("bejerman_remito_pdf_pm_lookup_failed", exc_info=True)
        pms = {}

    enriquecido = dict(comprobante)
    nuevos_items = []
    for item in items:
        copia = dict(item)
        codigo = _clean(copia.get("Item_CodigoArticulo"))
        partida = _clean(copia.get("Item_Partida")).upper()
        # Lo que ya venga en el renglón manda: puede haberlo puesto quien emitió.
        if codigo and not any(_clean(copia.get(k)) for k in PM_KEYS):
            copia["Item_PM"] = pms.get(codigo, "")
        if partida and not any(_clean(copia.get(k)) for k in VTO_KEYS):
            copia["Item_Vencimiento"] = vencimientos.get(partida) or ""
        nuevos_items.append(copia)
    enriquecido["Comprobante_Items"] = nuevos_items
    return enriquecido
