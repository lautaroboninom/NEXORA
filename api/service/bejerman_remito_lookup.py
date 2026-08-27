"""Busca en Bejerman el comprobante que ya salió, a partir del número de serie.

Hasta ahora, para registrar en NEXORA un remito emitido a mano había que saber de
antemano su número. El operador tiene el equipo (o su MG), no el comprobante: así se
registraron 19 alquileres con el número puesto y sin ningún equipo vinculado.

Acá se da vuelta la búsqueda: se pregunta por la serie y Bejerman contesta qué remitos
la citan. Tres reglas filtran lo que no puede ser esa entrega:

- **Tipo**: RSS para entregas de Servicio Técnico, RTA para altas de alquiler.
- **Fecha**: un remito anterior al ingreso del equipo no puede ser su salida.
- **No repetido para el mismo NS**: si ese número ya quedó asociado a esa misma serie,
  no se vuelve a ofrecer. El mismo remito **sí** puede aparecer para otra OS: un
  comprobante puede llevar varios equipos, y ahí el número se repite legítimamente.

De cada candidato se devuelve además a quién salió, traducido al cliente del maestro
local: el comprobante lo dice con el código de Bejerman, que al operador no le sirve para
completar el campo "¿A quién?" del alquiler.

Y se devuelve el comprobante entero, no sólo el renglón del equipo buscado: asociar un
remito es un movimiento que después hay que deshacer a mano, así que antes de confirmarlo
el operador tiene que poder ver qué más lleva ese número, a qué CUIT salió y de qué
depósito, y reconocerlo o descartarlo sin salir de la pantalla.

El canal es el SQL directo a SAMTRONIC (`_remote_bejerman_sql_csv`), no el SDK: el SDK
no sabe filtrar comprobantes por partida y bajaría tablas enteras para descartarlas acá.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from django.db import connection

from .bejerman_sdk import BEJERMAN_OCCASIONAL_CUSTOMER_CODE, OCCASIONAL_CUSTOMER_NAMES


REMITO_TYPES_BY_FLOW = {
    "service_release": ("RSS",),
    "rental": ("RTA",),
}
SEARCH_LIMIT = 50
# El detalle de cada comprobante multiplica las filas: se acota el total que viaja por SSH
# y, por candidato, cuantos renglones se muestran (un remito no pasa de 22 lineas).
DETAIL_ROW_LIMIT = 800
CANDIDATE_LINE_LIMIT = 40
# Las series reales traen almohadilla y barra (D9#1807, MA26/040): se aceptan, pero nada
# que pueda cerrar el literal SQL que se arma para SAMTRONIC.
_SAFE_SERIAL = re.compile(r"[^A-Za-z0-9#/._\- ]")
# Punto flexible (hay remitos viejos tipeados sin ceros) pero número de 4 dígitos para
# arriba: con menos, un "24-08" suelto se colaría como si fuera un comprobante.
_DOCUMENT_NUMBER = re.compile(r"(\d{1,5})\s*-\s*(\d{4,10})")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _sanitize_serial(value: Any) -> str:
    return _SAFE_SERIAL.sub("", _clean(value)).strip()


def _document_key(value: Any) -> tuple[str, str] | None:
    """(punto, número) de un remito escrito de cualquier forma.

    Sirve para comparar "RSS R 00004-00004833" con lo que devuelve Bejerman en columnas
    separadas, sin depender de cómo quedó escrito el texto guardado.
    """
    match = _DOCUMENT_NUMBER.search(_clean(value))
    if not match:
        return None
    return match.group(1).zfill(5), match.group(2).lstrip("0").zfill(8)


def _has_column(table: str, column: str) -> bool:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT 1
              FROM information_schema.columns
             WHERE table_name = %s AND column_name = %s
             LIMIT 1
            """,
            [table, column],
        )
        return cur.fetchone() is not None


def resolve_equipment_by_identifier(identifier: Any) -> dict[str, Any] | None:
    """Ubica el equipo por número de serie o por MG: el operador usa cualquiera de los dos."""
    text = _clean(identifier)
    if not text:
        return None
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT d.id AS device_id,
                   COALESCE(d.numero_serie, '') AS numero_serie,
                   COALESCE(d.numero_interno, '') AS numero_interno,
                   COALESCE(b.nombre, '') AS marca,
                   COALESCE(m.nombre, '') AS modelo
              FROM devices d
              LEFT JOIN marcas b ON b.id = d.marca_id
              LEFT JOIN models m ON m.id = d.model_id
             WHERE LOWER(TRIM(COALESCE(d.numero_serie, ''))) = LOWER(TRIM(%s))
                OR UPPER(REGEXP_REPLACE(COALESCE(d.numero_interno, ''), '\\s+', '', 'g'))
                   = UPPER(REGEXP_REPLACE(%s, '\\s+', '', 'g'))
             ORDER BY (LOWER(TRIM(COALESCE(d.numero_serie, ''))) = LOWER(TRIM(%s))) DESC, d.id DESC
             LIMIT 1
            """,
            [text, text, text],
        )
        cols = [col[0] for col in cur.description]
        row = cur.fetchone()
    return dict(zip(cols, row)) if row else None


def ingreso_reference_date(ingreso_id: Any) -> str:
    """Fecha desde la que un remito puede ser la salida de esa OS."""
    if not ingreso_id:
        return ""
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(fecha_ingreso, fecha_creacion)
              FROM ingresos
             WHERE id = %s
            """,
            [ingreso_id],
        )
        row = cur.fetchone()
    if not row or not row[0]:
        return ""
    value = row[0]
    return value.date().isoformat() if hasattr(value, "date") else str(value)[:10]


def used_document_keys_for_serial(serial: str) -> set[tuple[str, str]]:
    """Remitos ya asociados a ESA serie.

    Se mira por serie y no por OS a propósito: el mismo equipo puede tener varias OS a lo
    largo del tiempo y no queremos ofrecer dos veces el remito con el que ya salió.
    """
    serial_text = _clean(serial)
    if not serial_text:
        return set()
    selects = []
    params: list[Any] = []
    serial_match = "LOWER(TRIM(COALESCE(d.numero_serie, ''))) = LOWER(TRIM(%s))"
    for column in ("remito_salida", "alquiler_remito"):
        if _has_column("ingresos", column):
            selects.append(
                f"""
                SELECT COALESCE(i.{column}, '') AS numero
                  FROM ingresos i
                  JOIN devices d ON d.id = i.device_id
                 WHERE {serial_match}
                """
            )
            params.append(serial_text)
    selects.append(
        f"""
        SELECT COALESCE(o.remito_number, '') AS numero
          FROM delivery_order_item_partidas doip
          JOIN delivery_order_items doi ON doi.id = doip.order_item_id
          JOIN delivery_orders o ON o.id = doi.order_id
          JOIN devices d ON d.id = doip.device_id
         WHERE {serial_match}
        """
    )
    params.append(serial_text)
    sql = " UNION ".join(selects)
    with connection.cursor() as cur:
        cur.execute(f"SELECT DISTINCT numero FROM ({sql}) usados WHERE COALESCE(numero, '') <> ''", params)
        rows = cur.fetchall()
    keys = set()
    for row in rows:
        key = _document_key(row[0])
        if key:
            keys.add(key)
    return keys


def _customer_code_key(value: Any) -> str:
    return _clean(value).upper()


def local_customers_by_code(codes: Any) -> dict[str, dict[str, Any]]:
    """Traduce el código de cliente de Bejerman al cliente del maestro local.

    El remito sabe a quién salió, pero lo dice con el código de Bejerman; el campo
    "¿A quién?" del alquiler se escribe con la razón social. Sin esta traducción el
    operador encuentra el comprobante y aun así tiene que salir a buscar el cliente
    a mano, que es justo donde se perdían los alquileres sin cliente cargado.
    """
    wanted = {key for key in (_customer_code_key(item) for item in codes or ()) if key}
    if not wanted:
        return {}
    real_codes = sorted(wanted - {BEJERMAN_OCCASIONAL_CUSTOMER_CODE})
    alias_select = "COALESCE(alias_interno, '')" if _has_column("customers", "alias_interno") else "''"
    resolved: dict[str, dict[str, Any]] = {}
    with connection.cursor() as cur:
        if real_codes:
            cur.execute(
                f"""
                SELECT UPPER(TRIM(COALESCE(cod_empresa, ''))) AS code,
                       id,
                       COALESCE(razon_social, '') AS razon_social,
                       {alias_select} AS alias_interno
                  FROM customers
                 WHERE UPPER(TRIM(COALESCE(cod_empresa, ''))) = ANY(%s)
                 ORDER BY id
                """,
                [real_codes],
            )
            for code, customer_id, razon_social, alias in cur.fetchall():
                resolved.setdefault(
                    code,
                    {"id": customer_id, "code": code, "name": razon_social, "alias": alias, "matched": True},
                )
        if BEJERMAN_OCCASIONAL_CUSTOMER_CODE in wanted:
            # El ocasional no tiene código propio en el maestro: el remito de un particular
            # apunta al placeholder local, y el nombre de la persona ya vive en el ingreso.
            cur.execute(
                """
                SELECT id, COALESCE(razon_social, '') AS razon_social
                  FROM customers
                 WHERE LOWER(REGEXP_REPLACE(COALESCE(razon_social, ''), '[^A-Za-z0-9]', '', 'g')) = ANY(%s)
                 ORDER BY id
                 LIMIT 1
                """,
                [sorted(OCCASIONAL_CUSTOMER_NAMES)],
            )
            row = cur.fetchone()
            if row:
                resolved[BEJERMAN_OCCASIONAL_CUSTOMER_CODE] = {
                    "id": row[0],
                    "code": BEJERMAN_OCCASIONAL_CUSTOMER_CODE,
                    "name": row[1],
                    "alias": "",
                    "matched": True,
                    "occasional": True,
                }
    return resolved


def _remote_rows(serial: str, types: tuple[str, ...], since: str, company_key: Any, outcome: dict[str, Any]) -> list[dict[str, Any]]:
    from .bejerman_delivery import _remote_bejerman_sql_csv

    safe_serial = _sanitize_serial(serial).replace("'", "''")
    type_list = ", ".join(f"'{item}'" for item in types)
    date_filter = ""
    if since:
        # La fecha ya viene validada como ISO por `_normalized_since`.
        date_filter = f"     AND c2.scv_FEmision >= '{since}'\n"
    # Primero se eligen los comprobantes que citan la partida y despues se traen TODOS sus
    # renglones: antes se devolvia solo la linea del equipo, y con eso el operador no podia
    # saber que mas llevaba el remito que estaba por asociar.
    sql = f"""
SET NOCOUNT ON;

SELECT TOP {DETAIL_ROW_LIMIT}
  CONVERT(varchar(20), c.scv_ID) AS comprobante_id,
  LTRIM(RTRIM(t.spvtco_Cod)) AS tipo,
  LTRIM(RTRIM(t.spv_Letra)) AS letra,
  LTRIM(RTRIM(t.spv_CodPvt)) AS punto,
  LTRIM(RTRIM(t.spv_Nro)) AS numero,
  CONVERT(varchar(10), c.scv_FEmision, 23) AS fecha,
  LTRIM(RTRIM(COALESCE(c.scvcli_Cod, ''))) AS cliente_codigo,
  LTRIM(RTRIM(COALESCE(c.scvcli_RazSoc, ''))) AS cliente_nombre,
  LTRIM(RTRIM(COALESCE(c.scvcli_CUIT, ''))) AS cliente_cuit,
  LTRIM(RTRIM(COALESCE(c.scv_Mens, ''))) AS observaciones,
  CAST(COALESCE(d.sdv_NReng, 0) AS varchar(20)) AS renglon,
  LTRIM(RTRIM(COALESCE(d.sdvstp_Partida, ''))) AS partida,
  LTRIM(RTRIM(COALESCE(d.sdvart_CodGen, '') + COALESCE(d.sdvart_CodEle1, '')
       + COALESCE(d.sdvart_CodEle2, '') + COALESCE(d.sdvart_CodEle3, ''))) AS articulo,
  LTRIM(RTRIM(COALESCE(d.sdv_Desc, ''))) AS descripcion,
  CAST(COALESCE(d.sdv_CantUM1, 0) AS varchar(40)) AS cantidad,
  LTRIM(RTRIM(COALESCE(d.sdvdep_Cod, ''))) AS deposito
FROM dbo.SegCabV c
JOIN dbo.SegTiposV t ON t.spvscv_ID = c.scv_ID
JOIN dbo.SegDetV d ON d.sdvscv_ID = c.scv_ID
WHERE c.scv_ID IN (
  SELECT TOP {SEARCH_LIMIT} c2.scv_ID
    FROM dbo.SegCabV c2
    JOIN dbo.SegTiposV t2 ON t2.spvscv_ID = c2.scv_ID
    JOIN dbo.SegDetV d2 ON d2.sdvscv_ID = c2.scv_ID
   WHERE UPPER(LTRIM(RTRIM(COALESCE(d2.sdvstp_Partida, '')))) = UPPER('{safe_serial}')
     AND LTRIM(RTRIM(t2.spvtco_Cod)) IN ({type_list})
{date_filter}   GROUP BY c2.scv_ID, c2.scv_FEmision
   ORDER BY c2.scv_FEmision DESC, c2.scv_ID DESC
)
ORDER BY c.scv_FEmision DESC, t.spv_Nro DESC, d.sdv_NReng;
""".strip()
    return _remote_bejerman_sql_csv(sql, company_key=company_key, outcome=outcome)


def _quantity(value: Any) -> float | None:
    """La cantidad se muestra siempre positiva: el signo es dirección de stock, no cantidad."""
    text = _clean(value).replace(",", ".")
    if not text:
        return None
    try:
        return abs(float(text))
    except ValueError:
        return None


def _serial_key(value: Any) -> str:
    return re.sub(r"\s+", "", _clean(value)).upper()


def _line_payload(row: dict[str, Any], serial: str) -> dict[str, Any]:
    partida = _clean(row.get("partida"))
    return {
        "row": _clean(row.get("renglon")),
        "partida": partida,
        "articleCode": _clean(row.get("articulo")),
        "description": _clean(row.get("descripcion")),
        "quantity": _quantity(row.get("cantidad")),
        "warehouse": _clean(row.get("deposito")),
        "isSearchedItem": bool(partida) and _serial_key(partida) == _serial_key(serial),
    }


def _normalized_since(value: Any) -> str:
    text = _clean(value)[:10]
    if not text:
        return ""
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def search_remitos_by_serial(
    identifier: Any,
    *,
    flow: str,
    company_key: Any = None,
    ingreso_id: Any = None,
    since: Any = None,
) -> dict[str, Any]:
    types = REMITO_TYPES_BY_FLOW.get(_clean(flow) or "service_release")
    if not types:
        return {"status": "invalid_flow", "candidates": [], "equipment": None}

    equipment = resolve_equipment_by_identifier(identifier)
    serial = _clean((equipment or {}).get("numero_serie")) or _sanitize_serial(identifier)
    if not serial:
        return {
            "status": "equipment_not_found",
            "candidates": [],
            "equipment": None,
            "detail": "No se encontró un equipo con ese número de serie ni con ese MG.",
        }

    reference_date = _normalized_since(since) or _normalized_since(ingreso_reference_date(ingreso_id))
    outcome: dict[str, Any] = {}
    rows = _remote_rows(serial, types, reference_date, company_key, outcome)
    status = _clean(outcome.get("status"))
    if status and status not in {"ok", "success"} and not rows:
        return {
            "status": status,
            "candidates": [],
            "equipment": equipment,
            "serial": serial,
            "referenceDate": reference_date,
            "detail": "No se pudo consultar Bejerman para buscar el remito.",
        }

    used = used_document_keys_for_serial(serial)
    candidates: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        punto = _clean(row.get("punto")).zfill(5)
        numero = _clean(row.get("numero")).lstrip("0").zfill(8)
        key = (punto, numero)
        if not numero.strip("0"):
            continue
        candidate = by_key.get(key)
        if candidate is None:
            tipo = _clean(row.get("tipo")) or types[0]
            letra = _clean(row.get("letra")) or "R"
            candidate = {
                "remitoNumber": f"{tipo} {letra} {punto}-{numero}",
                "type": tipo,
                "letter": letra,
                "pointOfSale": punto,
                "number": numero,
                "documentId": _clean(row.get("comprobante_id")),
                "issueDate": _clean(row.get("fecha")),
                "customerCode": _clean(row.get("cliente_codigo")),
                "customerName": _clean(row.get("cliente_nombre")),
                "customerCuit": _clean(row.get("cliente_cuit")),
                "notes": _clean(row.get("observaciones")),
                # Los tres campos de abajo describen el renglón del equipo buscado; se
                # mantienen sueltos porque son los que consume la carga manual del remito.
                "articleCode": "",
                "description": "",
                "partida": "",
                "lines": [],
                "lineCount": 0,
                "linesTruncated": False,
                "alreadyLinked": key in used,
            }
            by_key[key] = candidate
            candidates.append(candidate)
        line = _line_payload(row, serial)
        candidate["lineCount"] += 1
        if len(candidate["lines"]) < CANDIDATE_LINE_LIMIT:
            candidate["lines"].append(line)
        else:
            candidate["linesTruncated"] = True
        if line["isSearchedItem"] and not candidate["partida"]:
            candidate["articleCode"] = line["articleCode"]
            candidate["description"] = line["description"]
            candidate["partida"] = line["partida"]
    for candidate in candidates:
        if not candidate["partida"]:
            primero = candidate["lines"][0] if candidate["lines"] else {}
            candidate["articleCode"] = candidate["articleCode"] or _clean(primero.get("articleCode"))
            candidate["description"] = candidate["description"] or _clean(primero.get("description"))
            candidate["partida"] = serial
        # Cuántos equipos más viajan en el mismo comprobante: es la pregunta que se hace el
        # operador cuando ve un número que no reconoce.
        candidate["otherItemCount"] = max(0, candidate["lineCount"] - 1)

    customers = local_customers_by_code({item["customerCode"] for item in candidates})
    for item in candidates:
        item["customer"] = customers.get(_customer_code_key(item["customerCode"]))

    available = [item for item in candidates if not item["alreadyLinked"]]
    return {
        "status": "ok",
        "equipment": equipment,
        "serial": serial,
        "referenceDate": reference_date,
        "candidates": available,
        "alreadyLinked": [item for item in candidates if item["alreadyLinked"]],
    }
