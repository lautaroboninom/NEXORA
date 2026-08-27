"""Trae a la cache de ventas los remitos de salida que citan una partida.

Un equipo puede no tener ninguna factura con la partida cargada y aun asi haber salido
del deposito: el circuito SEPID -> MG BIO -> cliente final factura desde la otra empresa
y en SEPID solo queda el remito. Para esos equipos la fecha del remito es lo unico que
existe para estimar la garantia de fabrica (al 27/08/2026: 2.341 partidas de SEPID y 85
de MG BIO tienen remito y ninguna factura).

El canal es el SQL directo a SAMTRONIC, no el SDK. Bajar 5.228 remitos de a uno con
`detalle_comprobante_ventas` son horas de round-trips; una sola consulta por ventana de
fechas trae los renglones ya filtrados por partida. Es el mismo criterio que ya usa
`bejerman_remito_lookup`, que busca comprobantes por serie por esta via justamente
porque el SDK no sabe filtrar por partida.

Las filas entran a `bejerman_sale_items` con su `document_type` (RT), asi que conviven
con las facturas sin pisarlas y `find_latest_remito_by_serial` las lee sin tocar el
camino de la factura.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .bejerman_sales import (
    SALE_ITEM_TABLE,
    WARRANTY_REMITO_TYPES_DEFAULT,
    _date_windows,
    _document_types,
    _sale_item_key,
    _table_exists,
    upsert_sale_items,
)
from .bejerman_sdk import (
    as_number,
    build_document_id,
    document_number_from_parts,
    normalize_date,
    normalize_serial_for_lookup,
)
from .customer_bejerman_sync import clean, clean_upper, digits_only


# El id de comprobante del SDK y el `scv_ID` de la base son numeraciones distintas que
# se solapan (la FC A 00002-00007340 es 7230 para el SDK y 10919 en SQL). El prefijo
# mantiene los dos espacios separados dentro de la misma columna.
REMOTE_COMPROBANTE_PREFIX = "samtronic:"

# Los remitos de venta arrancan en 2022-06-02; con un piso fijo el backfill completo no
# depende de cuando se lo corre.
DEFAULT_DATE_FROM = "2022-01-01"


def _remito_rows_sql(types: list[str], win_from: str, win_to: str) -> str:
    """Renglones de remito con partida en una ventana de fechas.

    `scv_SgnStk = -1` deja solo lo que sale del deposito. Hoy los 24.513 renglones RT de
    SEPID son todos -1, pero sin la guarda un tipo nuevo que entre mercaderia se colaria
    como si fuera una salida y le inventaria garantia al equipo.
    """
    type_list = ", ".join(f"'{item}'" for item in types)
    # `scv_FEmision` es datetime: el limite superior va exclusivo sobre el dia siguiente
    # para no perder los comprobantes con hora.
    win_to_exclusive = (date.fromisoformat(win_to) + timedelta(days=1)).isoformat()
    return f"""
SET NOCOUNT ON;

SELECT
  LTRIM(RTRIM(t.spvtco_Cod)) AS tipo,
  LTRIM(RTRIM(t.spv_Letra)) AS letra,
  LTRIM(RTRIM(t.spv_CodPvt)) AS punto,
  LTRIM(RTRIM(t.spv_Nro)) AS numero,
  CONVERT(varchar(10), c.scv_FEmision, 23) AS fecha,
  LTRIM(RTRIM(COALESCE(c.scvcli_Cod, ''))) AS cliente_codigo,
  LTRIM(RTRIM(COALESCE(c.scvcli_RazSoc, ''))) AS cliente_nombre,
  LTRIM(RTRIM(COALESCE(c.scvcli_CUIT, ''))) AS cliente_cuit,
  LTRIM(RTRIM(COALESCE(d.sdvstp_Partida, ''))) AS partida,
  LTRIM(RTRIM(COALESCE(d.sdvart_CodGen, '') + COALESCE(d.sdvart_CodEle1, '')
       + COALESCE(d.sdvart_CodEle2, '') + COALESCE(d.sdvart_CodEle3, ''))) AS articulo,
  LTRIM(RTRIM(COALESCE(d.sdv_Desc, ''))) AS descripcion,
  ABS(d.sdv_CantUM1) AS cantidad,
  d.sdv_NReng AS renglon,
  c.scv_ID AS comprobante_id
FROM dbo.SegCabV c
JOIN dbo.SegTiposV t ON t.spvscv_ID = c.scv_ID
JOIN dbo.SegDetV d ON d.sdvscv_ID = c.scv_ID
WHERE LTRIM(RTRIM(t.spvtco_Cod)) IN ({type_list})
  AND LTRIM(RTRIM(COALESCE(d.sdvstp_Partida, ''))) <> ''
  AND c.scv_SgnStk = -1
  AND c.scv_FEmision >= '{win_from}'
  AND c.scv_FEmision < '{win_to_exclusive}'
ORDER BY c.scv_FEmision, c.scv_ID, d.sdv_NReng;
""".strip()


def item_from_remito_row(row: dict[str, Any], *, company_key: str) -> dict[str, Any] | None:
    """Traduce un renglon del SQL directo a la forma que guarda `bejerman_sale_items`."""
    partida = clean(row.get("partida"))
    normalized_serial = normalize_serial_for_lookup(partida)
    if not normalized_serial:
        return None
    issue_date = normalize_date(row.get("fecha"))
    if not issue_date:
        return None

    doc_type = clean_upper(row.get("tipo"))
    letter = clean_upper(row.get("letra"))
    point = clean(row.get("punto"))
    number = clean(row.get("numero"))
    customer_code = clean(row.get("cliente_codigo"))
    try:
        line_index = int(clean(row.get("renglon")) or 0)
    except ValueError:
        line_index = 0

    item = {
        "company_key": clean_upper(company_key) or "SEPID",
        "comprobante_id": f"{REMOTE_COMPROBANTE_PREFIX}{clean(row.get('comprobante_id'))}",
        "document_id": (
            build_document_id({"t": doc_type, "n": number, "l": letter, "p": point, "f": issue_date, "c": customer_code})
            if doc_type and number and point
            else None
        ),
        "document_type": doc_type,
        "document_letter": letter,
        "document_point_of_sale": point,
        "document_number": number,
        "document_label": document_number_from_parts(doc_type, letter, point, number),
        "issue_date": issue_date,
        "customer_code": customer_code,
        "customer_name": clean(row.get("cliente_nombre")),
        "customer_cuit": digits_only(row.get("cliente_cuit")),
        "article_code": clean(row.get("articulo")),
        "article_description": clean(row.get("descripcion")),
        "item_partida": partida,
        "normalized_serial": normalized_serial,
        "quantity": as_number(row.get("cantidad")),
        "unit_price": None,
        "total_amount": None,
        "currency": "",
        "line_index": line_index,
        # El remito no trae precios por esta via: se deja constancia del origen para que
        # un importe vacio no se confunda con un dato que el SDK no supo traer.
        "raw_document": {"source": "samtronic_sql", "scvId": clean(row.get("comprobante_id"))},
        "raw_item": dict(row),
    }
    item["sale_item_key"] = _sale_item_key(item)
    return item


def sync_bejerman_remito_items(
    *,
    company_key: str = "SEPID",
    date_from: str | None = None,
    date_to: str | None = None,
    types: list[str] | None = None,
    chunk_days: int = 366,
    dry_run: bool = False,
) -> dict[str, Any]:
    from .bejerman_delivery import _remote_bejerman_sql_csv

    company = clean_upper(company_key) or "SEPID"
    types = [item.upper() for item in (types or _document_types(
        "BEJERMAN_WARRANTY_REMITO_TYPES", WARRANTY_REMITO_TYPES_DEFAULT
    ))]
    date_to = clean(date_to) or date.today().isoformat()
    date_from = clean(date_from) or DEFAULT_DATE_FROM

    summary: dict[str, Any] = {
        "ok": True,
        "companyKey": company,
        "dateFrom": date_from,
        "dateTo": date_to,
        "types": types,
        "windows": 0,
        "rows": 0,
        "items": 0,
        "upserted": 0,
        "errors": [],
        "dryRun": bool(dry_run),
    }
    if not types:
        summary["ok"] = False
        summary["errors"].append({"error": "Sin tipos de remito configurados"})
        return summary
    if not dry_run and not _table_exists(SALE_ITEM_TABLE):
        summary["ok"] = False
        summary["errors"].append({"error": f"Falta la tabla {SALE_ITEM_TABLE}"})
        return summary

    # De la ventana mas nueva a la mas vieja: si el backfill se corta a la mitad, lo que
    # quedo cargado es lo reciente, que es lo que mas se consulta.
    for win_from, win_to in _date_windows(date_from, date_to, chunk_days):
        summary["windows"] += 1
        outcome: dict[str, Any] = {}
        rows = _remote_bejerman_sql_csv(
            _remito_rows_sql(types, win_from, win_to),
            company_key=company,
            outcome=outcome,
            timeout_seconds=180,
        )
        if outcome.get("status") != "success":
            summary["ok"] = False
            summary["errors"].append(
                {"window": f"{win_from}/{win_to}", "status": outcome.get("status"), "detail": outcome.get("detail")}
            )
            continue
        summary["rows"] += len(rows)
        items = [item for item in (item_from_remito_row(row, company_key=company) for row in rows) if item]
        summary["items"] += len(items)
        if not dry_run and items:
            # Un upsert por ventana: la transaccion no queda abierta sobre todo el backfill.
            summary["upserted"] += upsert_sale_items(items)
    return summary
