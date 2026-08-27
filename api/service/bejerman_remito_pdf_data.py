"""Completa el comprobante con los datos que el PDF muestra pero la emisión no devuelve.

`IngresarComprobanteJSON` responde con el número de comprobante y los renglones tal
como se enviaron, pero el PDF que arma Bejerman muestra dos columnas más, que resuelve
por su cuenta contra sus maestros:

* **PM** — `dbo.Articulos.art_DescRedu` del artículo. Verificado contra el papel
  escaneado: 1204001 -> `2046-10`, 1205013 -> `2046-12`.
* **VTO.** — el vencimiento de la partida. Éste **ya lo tiene NEXORA**: una partida
  histórica puede estar en `delivery_order_items`, mientras que las partidas de los
  insumos se guardan en `delivery_order_item_partidas`. Por eso se busca primero en
  casa y sólo se sale a Bejerman por lo que falte.

Nada de esto puede fallar el remito: si no se consigue un dato, la columna sale vacía
y el PDF se entrega igual, que es justamente el problema que estamos resolviendo.
"""

from __future__ import annotations

import json
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


def _pm_lookup_timeout() -> int:
    """Plazo de la consulta remota de PM. Corto a propósito: hay alguien esperando."""

    from django.conf import settings

    try:
        value = int(getattr(settings, "BEJERMAN_REMITO_PDF_PM_LOOKUP_TIMEOUT", 10) or 10)
    except (TypeError, ValueError):
        return 10
    return value if value > 0 else 10


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


def expiration_dates_from_nexora(
    partidas: list[str], article_codes: list[str]
) -> dict[tuple[str, str], Any]:
    """Vencimientos por ``(artículo, partida)`` ya guardados en NEXORA.

    Las órdenes históricas guardaban la partida en el ítem padre; las órdenes
    actuales de insumos la guardan en su tabla hija. Se prioriza la carga más
    reciente y se conserva el artículo en la clave para no cruzar lotes homónimos.
    """

    normalized_partidas = sorted({_clean(partida).upper() for partida in partidas if _clean(partida)})
    if not normalized_partidas:
        return {}
    normalized_codes = sorted({_clean(code).upper() for code in article_codes if _clean(code)})
    with connection.cursor() as cur:
        cur.execute(
            """
            WITH expiration_rows AS (
                SELECT UPPER(TRIM(COALESCE(item.article_code, ''))) AS article_code,
                       UPPER(TRIM(item.partida)) AS partida,
                       item.partida_expiration_date,
                       item.updated_at,
                       1 AS source_priority
                  FROM delivery_order_items AS item
                 WHERE item.partida_expiration_date IS NOT NULL
                   AND UPPER(TRIM(item.partida)) = ANY(%s)
                   AND (%s = '{}'::text[] OR UPPER(TRIM(COALESCE(item.article_code, ''))) = ANY(%s))

                UNION ALL

                SELECT UPPER(TRIM(COALESCE(item.article_code, ''))) AS article_code,
                       UPPER(TRIM(partida.partida)) AS partida,
                       partida.partida_expiration_date,
                       partida.updated_at,
                       2 AS source_priority
                  FROM delivery_order_item_partidas AS partida
                  JOIN delivery_order_items AS item ON item.id = partida.order_item_id
                 WHERE partida.partida_expiration_date IS NOT NULL
                   AND UPPER(TRIM(partida.partida)) = ANY(%s)
                   AND (%s = '{}'::text[] OR UPPER(TRIM(COALESCE(item.article_code, ''))) = ANY(%s))
            )
            SELECT DISTINCT ON (article_code, partida)
                   article_code,
                   partida,
                   partida_expiration_date
              FROM expiration_rows
             ORDER BY article_code, partida, updated_at DESC, source_priority DESC
            """,
            [
                normalized_partidas,
                normalized_codes,
                normalized_codes,
                normalized_partidas,
                normalized_codes,
                normalized_codes,
            ],
        )
        return {(row[0], row[1]): row[2] for row in cur.fetchall()}


def pm_from_bejerman(article_codes: list[str], *, company_key: Any = None) -> dict[str, str]:
    """PM (`art_DescRedu`) de cada artículo, leído del maestro de Bejerman."""

    if not article_codes:
        return {}
    company = _clean(company_key).upper() or "SEPID"
    ahora = time.monotonic()
    resultado = cached_pm_from_bejerman(article_codes, company_key=company, now=ahora)
    faltantes = [code for code in article_codes if code not in resultado]
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
        filas = _remote_bejerman_sql_csv(
            sql, company_key=company, timeout_seconds=_pm_lookup_timeout()
        )
    except Exception:
        logger.warning("bejerman_remito_pdf_pm_lookup_failed", extra={"company_key": company}, exc_info=True)
        return resultado

    for fila in filas or []:
        codigo, pm = _clean(fila.get("Codigo")), _clean(fila.get("PM"))
        if codigo:
            resultado[codigo] = pm
            _PM_CACHE[(company, codigo)] = (ahora, pm)
    return resultado


def cached_pm_from_bejerman(
    article_codes: list[str],
    *,
    company_key: Any = None,
    now: float | None = None,
) -> dict[str, str]:
    """Devuelve únicamente PM ya disponible en memoria, sin esperar a Bejerman."""

    if not article_codes:
        return {}
    company = _clean(company_key).upper() or "SEPID"
    moment = time.monotonic() if now is None else now
    return {
        code: cached[1]
        for code in article_codes
        if (cached := _PM_CACHE.get((company, code))) and moment - cached[0] < PM_CACHE_TTL_SECONDS
    }


def _maybe_json(value: Any) -> Any:
    """Parsea un texto que en realidad es JSON.

    Hace falta dos veces: la columna JSONB vuelve como `str`, y adentro el SDK
    guarda `DatosJSON` como un string JSON dentro del JSON (doble codificado).
    """

    if not isinstance(value, str):
        return value
    texto = value.strip()
    if not texto.startswith(("{", "[")):
        return value
    try:
        return json.loads(texto)
    except (ValueError, TypeError):
        return value


def _find_comprobante(node: Any, depth: int = 0) -> dict[str, Any] | None:
    """Busca, dentro de un JSON guardado, el dict que parece el comprobante.

    Los dos circuitos guardan la emisión con forma distinta (el de ingreso en
    `bejerman_ingreso_remitos`, el de órdenes de entrega en
    `bejerman_integration_attempts`) y además el SDK anida su respuesta y la
    vuelve a serializar. En vez de codificar cada forma se busca el dict que
    trae los renglones, que es lo único que el renderer necesita sí o sí.
    """

    if depth > 10:
        return None
    node = _maybe_json(node)
    if isinstance(node, dict):
        items = node.get("Comprobante_Items")
        if isinstance(items, str):  # los renglones también pueden venir serializados
            items = _maybe_json(items)
        if isinstance(items, list) and items:
            return dict(node, Comprobante_Items=items)
        for value in node.values():
            encontrado = _find_comprobante(value, depth + 1)
            if encontrado is not None:
                return encontrado
    elif isinstance(node, list):
        for value in node:
            encontrado = _find_comprobante(value, depth + 1)
            if encontrado is not None:
                return encontrado
    return None


_COMPROBANTE_HEADER_ALIASES = {
    "Comprobante_Tipo": ("Comprobante_Tipo", "comprobanteTipo"),
    "Comprobante_Letra": ("Comprobante_Letra", "comprobanteLetra"),
    "Comprobante_PtoVenta": ("Comprobante_PtoVenta", "comprobantePtoVenta", "pointOfSale"),
    "Comprobante_Numero": ("Comprobante_Numero", "comprobanteNumero"),
    "Comprobante_FechaEmision": ("Comprobante_FechaEmision", "issueDate", "fechaEmision"),
    "Cliente_Codigo": ("Cliente_Codigo", "customerCode"),
}


def _comprobante_headers(node: Any, *, depth: int = 0) -> dict[str, Any]:
    """Busca la cabecera confirmada sin recorrer los renglones del comprobante."""

    if depth > 10:
        return {}
    node = _maybe_json(node)
    headers: dict[str, Any] = {}
    if isinstance(node, dict):
        for field, aliases in _COMPROBANTE_HEADER_ALIASES.items():
            for alias in aliases:
                value = node.get(alias)
                if _clean(value):
                    headers[field] = value
                    break
        for key, value in node.items():
            if key == "Comprobante_Items":
                continue
            for field, header_value in _comprobante_headers(value, depth=depth + 1).items():
                headers.setdefault(field, header_value)
    elif isinstance(node, list):
        for value in node:
            for field, header_value in _comprobante_headers(value, depth=depth + 1).items():
                headers.setdefault(field, header_value)
    return headers


def comprobante_from_emission(*candidates: Any) -> dict[str, Any] | None:
    """Saca el comprobante de la respuesta de emisión que todavía está en memoria.

    Es el camino preferido: se llama apenas Bejerman confirmó el número, así que no
    hay que ir a buscar nada a la base. Se le pasan la respuesta y el pedido, en ese
    orden de prioridad.
    """

    comprobante = None
    for candidato in candidates:
        comprobante = _find_comprobante(candidato)
        if comprobante:
            break
    if not comprobante:
        return None

    # El SDK suele confirmar sólo la cabecera y deja los renglones en el pedido
    # original. Juntamos ambos datos para que el renderer propio tenga el número
    # definitivo, sin volver a consultar el PDF al ERP.
    headers: dict[str, Any] = {}
    for candidato in candidates:
        for field, value in _comprobante_headers(candidato).items():
            headers.setdefault(field, value)
    return {**comprobante, **headers}


def _comprobante_from_payload_rows(rows: Any) -> dict[str, Any] | None:
    """Combina respuestas y pedidos de varios intentos de la misma emisión.

    Los intentos separados guardan primero el cuerpo enviado y luego la respuesta
    confirmada. Ninguna de esas filas aislada tiene todo lo necesario para
    reproducir el remito, pero juntas sí.
    """

    candidates: list[Any] = []
    for response, request in rows or []:
        candidates.extend((response, request))
    return comprobante_from_emission(*candidates)


def load_comprobante_for_document(row: dict[str, Any]) -> dict[str, Any] | None:
    """Recupera el comprobante emitido a partir de la fila de la cola de PDF.

    Se mira primero lo que respondió Bejerman y después lo que se le mandó: la
    respuesta es la fuente de verdad, pero si vino recortada el pedido tiene los
    mismos renglones.
    """

    tipo, punto = _clean(row.get("tipo")), _clean(row.get("pto_venta"))
    numero, empresa = _clean(row.get("numero")), _clean(row.get("company_key"))
    grupo = _clean(row.get("remito_group_id"))

    consultas: list[tuple[str, list[Any]]] = [
        (
            """
            SELECT response_payload, request_payload
              FROM bejerman_ingreso_remitos
             WHERE COALESCE(comprobante_tipo, '') = %s
               AND COALESCE(comprobante_pto_venta, '') = %s
               AND COALESCE(comprobante_numero, '') = %s
               AND (%s = '' OR COALESCE(company_key, '') = %s)
             ORDER BY id DESC
             LIMIT 3
            """,
            [tipo, punto, numero, empresa, empresa],
        )
    ]
    if grupo:
        consultas.append(
            (
                """
                SELECT response_payload, request_payload
                  FROM bejerman_integration_attempts
                 WHERE group_id = %s
                 ORDER BY id DESC
                 LIMIT 5
                """,
                [grupo],
            )
        )

    for sql, params in consultas:
        try:
            with connection.cursor() as cur:
                cur.execute(sql, params)
                filas = cur.fetchall()
        except Exception:
            logger.warning("bejerman_remito_pdf_payload_query_failed", exc_info=True)
            continue
        comprobante = _comprobante_from_payload_rows(filas)
        if comprobante:
            return comprobante
    return None


def enrich_comprobante_for_pdf(
    comprobante: dict[str, Any],
    *,
    company_key: Any = None,
    allow_remote_pm_lookup: bool = True,
) -> dict[str, Any]:
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
        pms = (
            pm_from_bejerman(codes, company_key=company_key)
            if allow_remote_pm_lookup
            else cached_pm_from_bejerman(codes, company_key=company_key)
        )
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
            copia["Item_Vencimiento"] = vencimientos.get((codigo.upper(), partida)) or ""
        nuevos_items.append(copia)
    enriquecido["Comprobante_Items"] = nuevos_items
    return enriquecido
