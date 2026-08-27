"""Equipos que vuelven a entrar mientras su ingreso anterior sigue abierto.

El caso real: el equipo se entregó, pero nadie tildó la entrega en NEXORA, así que la OS
vieja quedó abierta. Meses después el cliente lo trae de nuevo y recepción no puede emitir
el remito de ingreso, con el cliente esperando en el mostrador.

La respuesta de acá es no frenar el mostrador: el ingreso se crea igual, el RIS se emite con
el equipo adentro pero **sin número de OS en ese renglón** (porque todavía no se sabe si la OS
buena es la nueva o la vieja), y el conflicto queda anotado para resolverlo después.

Para resolverlo hace falta saber si el equipo realmente salió. Eso lo sabe Bejerman: si hay un
RSS (entrega de equipo reparado) o un RTA (alquiler) con esa partida **posterior** a la fecha
del ingreso viejo, el equipo salió y la OS vieja se puede cerrar con ese comprobante como
respaldo. Esa búsqueda es la que habilita a recepción a resolverlo sola.

La serie no siempre está escrita igual de los dos lados ("MZJ5S 121220" acá,
"MZJ5S121220" en Bejerman), así que la comparación ignora espacios y guiones. Y cuando
ni así aparece pero la persona tiene el comprobante a la vista, se puede cargar el número
y verificarlo contra Bejerman (`find_document_by_number`): sin ese camino el conflicto
quedaba trabado para siempre, que es exactamente lo que ya pasó con los remitos de entrega.

Ojo con las consultas remotas: son una por conflicto y a demanda. Nunca en loop
(ver docs/modificaciones-directas-bejerman-sql.md).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from django.db import connection

logger = logging.getLogger(__name__)


# Los dos comprobantes que acreditan que el equipo salió del taller después de haber entrado.
# RSS: entrega del equipo reparado. RTA: sale en alquiler.
EVIDENCE_DOCUMENT_TYPES = ("RSS", "RTA")

# La orden anterior en cualquiera de estos estados ya no está abierta, así que el conflicto se
# terminó: lo haya cerrado esta pantalla o cualquier otro camino (corrección histórica, entrega,
# baja). Es la misma lista con la que `_open_ingreso_for_equipment` decide que una OS sigue viva.
CLOSED_INGRESO_STATES = (
    "entregado",
    "alquilado",
    "baja",
    "vendido_pendiente_entrega",
    "vendido_entregado",
)

TABLE_NAME = "ingreso_open_conflicts"


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _sql_literal(value: Any) -> str:
    return "N'" + _clean(value).replace("'", "''") + "'"


def _json_value(raw: Any, fallback: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    text = _clean(raw)
    if not text:
        return fallback
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return fallback


def conflicts_schema_ready() -> bool:
    try:
        with connection.cursor() as cur:
            if connection.vendor == "postgresql":
                cur.execute(
                    """
                    SELECT 1 FROM information_schema.tables
                     WHERE table_schema = ANY(current_schemas(true)) AND table_name = %s
                     LIMIT 1
                    """,
                    [TABLE_NAME],
                )
            else:
                cur.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=%s LIMIT 1",
                    [TABLE_NAME],
                )
            return bool(cur.fetchone())
    except Exception:
        return False


def _closed_state_filter() -> str:
    """Condición SQL: la orden anterior todavía está abierta."""

    estado = "prev.estado::text" if connection.vendor == "postgresql" else "prev.estado"
    valores = ", ".join(f"'{item}'" for item in CLOSED_INGRESO_STATES)
    return f"COALESCE({estado}, '') NOT IN ({valores})"


def _rows(sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    with connection.cursor() as cur:
        cur.execute(sql, params or [])
        columns = [column[0] for column in cur.description]
        return [dict(zip(columns, raw)) for raw in cur.fetchall()]


def _row(sql: str, params: list[Any] | None = None) -> dict[str, Any] | None:
    found = _rows(sql, params)
    return found[0] if found else None


# --------------------------------------------------------------------------------------
# Registro del conflicto
# --------------------------------------------------------------------------------------


def record_conflict(
    ingreso_id: int,
    conflicting_ingreso_id: int,
    *,
    numero_serie: str = "",
    tipo_equipo: str = "",
    user_id: int | None = None,
) -> int | None:
    """Anota (o reusa) el conflicto del ingreso recién creado. Idempotente por ingreso."""

    if not conflicts_schema_ready():
        logger.warning(
            "ingreso_open_conflict_schema_missing",
            extra={"ingreso_id": ingreso_id, "conflicting_ingreso_id": conflicting_ingreso_id},
        )
        return None
    existing = _row(f"SELECT id FROM {TABLE_NAME} WHERE ingreso_id = %s", [ingreso_id])
    if existing:
        return int(existing["id"])
    with connection.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {TABLE_NAME}
              (ingreso_id, conflicting_ingreso_id, status, numero_serie, tipo_equipo, created_by)
            VALUES (%s, %s, 'pending', %s, %s, %s)
            """,
            [ingreso_id, conflicting_ingreso_id, _clean(numero_serie), _clean(tipo_equipo), user_id],
        )
    created = _row(f"SELECT id FROM {TABLE_NAME} WHERE ingreso_id = %s", [ingreso_id])
    return int(created["id"]) if created else None


def pending_conflict_ingreso_ids(ingreso_ids: list[int]) -> set[int]:
    """Los ingresos del lote que todavía arrastran un conflicto sin resolver.

    Es lo que decide qué renglones del remito salen sin número de OS. Se deriva de la tabla
    en vez de guardarse en `ingresos` para que, al resolverse el conflicto, la OS vuelva a
    imprimirse sola en las reimpresiones.
    """

    clean_ids = [int(item) for item in ingreso_ids or [] if str(item).isdigit()]
    if not clean_ids or not conflicts_schema_ready():
        return set()
    placeholders = ", ".join(["%s"] * len(clean_ids))
    rows = _rows(
        f"""
        SELECT k.ingreso_id
          FROM {TABLE_NAME} k
          LEFT JOIN ingresos prev ON prev.id = k.conflicting_ingreso_id
         WHERE k.status = 'pending'
           AND {_closed_state_filter()}
           AND k.ingreso_id IN ({placeholders})
        """,
        clean_ids,
    )
    return {int(row["ingreso_id"]) for row in rows}


def attach_remito_number(ingreso_ids: list[int], remito_number: str) -> None:
    """Deja anotado en qué remito salió el equipo, para poder rastrearlo al resolver."""

    clean_ids = [int(item) for item in ingreso_ids or [] if str(item).isdigit()]
    number = _clean(remito_number)
    if not clean_ids or not number or not conflicts_schema_ready():
        return
    placeholders = ", ".join(["%s"] * len(clean_ids))
    with connection.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {TABLE_NAME}
               SET remito_number = %s,
                   updated_at = CURRENT_TIMESTAMP
             WHERE status = 'pending'
               AND COALESCE(remito_number, '') = ''
               AND ingreso_id IN ({placeholders})
            """,
            [number, *clean_ids],
        )


def get_conflict(conflict_id: int) -> dict[str, Any] | None:
    if not conflicts_schema_ready():
        return None
    row = _row(f"SELECT * FROM {TABLE_NAME} WHERE id = %s", [conflict_id])
    if row:
        row["evidence"] = _json_value(row.get("evidence"), {})
    return row


def list_pending_conflicts() -> list[dict[str, Any]]:
    if not conflicts_schema_ready():
        return []
    rows = _rows(
        f"""
        SELECT k.id,
               k.ingreso_id,
               k.conflicting_ingreso_id,
               k.status,
               k.remito_number,
               k.numero_serie,
               k.tipo_equipo,
               k.evidence,
               k.created_at,
               k.updated_at,
               COALESCE(prev.estado::text, '') AS conflicting_estado,
               COALESCE(prev.fecha_ingreso, prev.fecha_creacion) AS conflicting_fecha_ingreso,
               COALESCE(c.razon_social, '') AS cliente,
               COALESCE(b.nombre, '') AS marca,
               COALESCE(m.nombre, '') AS modelo,
               COALESCE(d.numero_interno, '') AS numero_interno,
               COALESCE(i.empresa_bejerman, '') AS empresa_bejerman
          FROM {TABLE_NAME} k
          JOIN ingresos i ON i.id = k.ingreso_id
          LEFT JOIN ingresos prev ON prev.id = k.conflicting_ingreso_id
          LEFT JOIN devices d ON d.id = i.device_id
          LEFT JOIN customers c ON c.id = d.customer_id
          LEFT JOIN marcas b ON b.id = d.marca_id
          LEFT JOIN models m ON m.id = d.model_id
         WHERE k.status = 'pending'
           AND {_closed_state_filter()}
         ORDER BY k.created_at DESC
        """
    )
    for row in rows:
        row["evidence"] = _json_value(row.get("evidence"), {})
    return rows


def mark_resolved(
    conflict_id: int,
    *,
    status: str = "resolved",
    note: str = "",
    evidence: dict[str, Any] | None = None,
    user_id: int | None = None,
) -> None:
    if not conflicts_schema_ready():
        return
    evidence_json = json.dumps(evidence or {}, ensure_ascii=False, default=str)
    cast = "::jsonb" if connection.vendor == "postgresql" else ""
    with connection.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {TABLE_NAME}
               SET status = %s,
                   resolution_note = %s,
                   evidence = %s{cast},
                   resolved_by = %s,
                   resolved_at = CURRENT_TIMESTAMP,
                   updated_at = CURRENT_TIMESTAMP
             WHERE id = %s
            """,
            [status, _clean(note), evidence_json, user_id, conflict_id],
        )


# --------------------------------------------------------------------------------------
# Evidencia en Bejerman
# --------------------------------------------------------------------------------------


def _normalize_serial(value: Any) -> str:
    """La misma serie escrita de dos formas tiene que encontrarse igual.

    Recepción tipea lo que dice la etiqueta y a veces la separa ("MZJ5S 121220"); en Bejerman
    la partida va pegada. Con la comparación literal el equipo "nunca salió" y el conflicto
    quedaba sin salida posible aunque el remito estuviera ahí. Se ignoran espacios y guiones,
    igual que cuando NEXORA decide si dos series son el mismo equipo.
    """

    return re.sub(r"[\s-]+", "", _clean(value)).upper()


def _serial_candidates(value: Any) -> list[str]:
    """Las formas conocidas de esa serie: la que tipeó recepción y la que tiene el equipo."""

    items = value if isinstance(value, (list, tuple, set)) else [value]
    out: list[str] = []
    for item in items:
        text = _clean(item)
        if text and text not in out:
            out.append(text)
    return out


def _document_key(value: Any) -> tuple[str, str] | None:
    """(punto, número) de un remito escrito de cualquier forma."""

    match = re.search(r"(\d{1,5})\s*-\s*(\d{4,10})", _clean(value))
    if not match:
        return None
    return match.group(1).zfill(5), match.group(2).lstrip("0").zfill(8)


def _document_rows(sql: str, company_key: Any) -> tuple[list[dict[str, Any]], str]:
    """Corre la consulta contra Bejerman. Devuelve (filas, motivo_de_falla)."""

    try:
        from .bejerman_delivery import _remote_bejerman_sql_csv

        outcome: dict[str, Any] = {}
        rows = _remote_bejerman_sql_csv(sql, company_key=_clean(company_key) or "SEPID", outcome=outcome)
    except Exception as exc:
        logger.warning("ingreso_conflict_evidence_unavailable", extra={"error": str(exc)[:500]})
        return [], str(exc)[:300]
    if outcome.get("status") != "success":
        return [], "No se pudo consultar los comprobantes en Bejerman."
    return rows or [], ""


def _document_payload(row: dict[str, Any]) -> dict[str, Any]:
    tipo = _clean(row.get("Comprobante_Tipo"))
    letra = _clean(row.get("Comprobante_Letra"))
    punto = _clean(row.get("Comprobante_PtoVenta"))
    numero = _clean(row.get("Comprobante_Numero"))
    label = " ".join(part for part in (tipo, letra, f"{punto}-{numero}" if punto or numero else "") if part)
    fecha = _clean(row.get("Comprobante_FechaEmision"))
    return {
        "type": tipo,
        "letter": letra,
        "pointOfSale": punto,
        "number": numero,
        "remitoNumber": label,
        "issueDate": fecha,
        "partida": _clean(row.get("Item_Partida")),
        "description": _clean(row.get("Item_DescripArticulo")),
        "code": label,
        "name": f"{label} · {fecha}",
    }


def find_exit_evidence(numero_serie: Any, since_date: Any, company_key: str = "SEPID") -> dict[str, Any]:
    """Busca en Bejerman un RSS/RTA de esa partida posterior a `since_date`.

    `numero_serie` puede ser una serie o varias (la que quedó anotada en el conflicto y la que
    tiene el equipo en NEXORA): se comparan todas sin espacios y en mayúsculas, porque la
    diferencia entre "MZJ5S 121220" y "MZJ5S121220" no es una diferencia de equipo.

    Devuelve siempre un dict con `status`: `found`, `not_found` o `unavailable`. Nunca levanta:
    que Bejerman no conteste no puede tumbar la pantalla de recepción, sólo significa que el
    conflicto se resuelve a mano.
    """

    serials = _serial_candidates(numero_serie)
    normalized: list[str] = []
    for serial in serials:
        key = _normalize_serial(serial)
        if key and key not in normalized:
            normalized.append(key)
    since = _clean(since_date)[:10]
    if not normalized or not since:
        return {"status": "not_found", "documents": [], "reason": "Falta número de serie o fecha del ingreso anterior."}

    type_values = ", ".join(_sql_literal(item) for item in EVIDENCE_DOCUMENT_TYPES)
    serial_values = ", ".join(_sql_literal(item) for item in normalized)
    sql = f"""
SET NOCOUNT ON;
SELECT TOP 5
  LTRIM(RTRIM(t.spvtco_Cod)) AS Comprobante_Tipo,
  LTRIM(RTRIM(t.spv_Letra)) AS Comprobante_Letra,
  LTRIM(RTRIM(t.spv_CodPvt)) AS Comprobante_PtoVenta,
  LTRIM(RTRIM(t.spv_Nro)) AS Comprobante_Numero,
  CONVERT(varchar(10), c.scv_FEmision, 23) AS Comprobante_FechaEmision,
  LTRIM(RTRIM(COALESCE(d.sdvstp_Partida, ''))) AS Item_Partida,
  LTRIM(RTRIM(COALESCE(d.sdv_Desc, ''))) AS Item_DescripArticulo
FROM dbo.SegCabV c
JOIN dbo.SegTiposV t ON t.spvscv_ID = c.scv_ID
LEFT JOIN dbo.SegDetV d ON d.sdvscv_ID = c.scv_ID
WHERE c.scv_FEmision > CONVERT(datetime, {_sql_literal(since)}, 120)
  AND LTRIM(RTRIM(t.spvtco_Cod)) IN ({type_values})
  AND UPPER(REPLACE(REPLACE(COALESCE(d.sdvstp_Partida, ''), ' ', ''), '-', '')) IN ({serial_values})
ORDER BY c.scv_FEmision DESC, c.scv_ID DESC;
""".strip()

    rows, failure = _document_rows(sql, company_key)
    if failure:
        return {"status": "unavailable", "documents": [], "reason": failure, "serials": serials}

    documents = [_document_payload(row) for row in rows]
    if not documents:
        return {
            "status": "not_found",
            "documents": [],
            "serial": serials[0],
            "serials": serials,
            "since": since,
            "reason": (
                f"Bejerman no tiene ningún {' ni '.join(EVIDENCE_DOCUMENT_TYPES)} de "
                f"{' / '.join(serials)} posterior al {since}."
            ),
        }
    return {"status": "found", "documents": documents, "since": since, "serial": serials[0], "serials": serials}


def find_document_by_number(remito_number: Any, *, company_key: str = "SEPID", since: Any = None) -> dict[str, Any]:
    """Verifica contra Bejerman un remito que alguien cargó a mano.

    Es el camino de vuelta del anterior: cuando la búsqueda por serie no encuentra nada pero la
    persona tiene el comprobante a la vista, se comprueba ese número en Bejerman en vez de
    creerle al formulario. `status`: `found`, `mismatch`, `not_found`, `invalid` o `unavailable`.
    """

    key = _document_key(remito_number)
    if not key:
        return {
            "status": "invalid",
            "documents": [],
            "reason": "El número de remito tiene que venir como 00004-00004893.",
        }
    punto, numero = key
    sql = f"""
SET NOCOUNT ON;
SELECT TOP 20
  LTRIM(RTRIM(t.spvtco_Cod)) AS Comprobante_Tipo,
  LTRIM(RTRIM(t.spv_Letra)) AS Comprobante_Letra,
  LTRIM(RTRIM(t.spv_CodPvt)) AS Comprobante_PtoVenta,
  LTRIM(RTRIM(t.spv_Nro)) AS Comprobante_Numero,
  CONVERT(varchar(10), c.scv_FEmision, 23) AS Comprobante_FechaEmision,
  LTRIM(RTRIM(COALESCE(d.sdvstp_Partida, ''))) AS Item_Partida,
  LTRIM(RTRIM(COALESCE(d.sdv_Desc, ''))) AS Item_DescripArticulo
FROM dbo.SegCabV c
JOIN dbo.SegTiposV t ON t.spvscv_ID = c.scv_ID
LEFT JOIN dbo.SegDetV d ON d.sdvscv_ID = c.scv_ID
WHERE RIGHT('00000' + LTRIM(RTRIM(t.spv_CodPvt)), 5) = {_sql_literal(punto)}
  AND RIGHT('00000000' + LTRIM(RTRIM(t.spv_Nro)), 8) = {_sql_literal(numero)}
ORDER BY c.scv_FEmision DESC, c.scv_ID DESC;
""".strip()

    rows, failure = _document_rows(sql, company_key)
    if failure:
        return {"status": "unavailable", "documents": [], "reason": failure}

    documents = [_document_payload(row) for row in rows]
    if not documents:
        return {
            "status": "not_found",
            "documents": [],
            "reason": f"Bejerman no tiene ningún comprobante {punto}-{numero} en {_clean(company_key) or 'SEPID'}.",
        }

    comprobante = documents[0]
    label = comprobante.get("remitoNumber") or f"{punto}-{numero}"
    if comprobante.get("type") not in EVIDENCE_DOCUMENT_TYPES:
        return {
            "status": "mismatch",
            "documents": documents,
            "reason": (
                f"{label} es un {comprobante.get('type') or 'comprobante'}, no un remito de salida "
                f"({' ni '.join(EVIDENCE_DOCUMENT_TYPES)})."
            ),
        }
    limit = _clean(since)[:10]
    fecha = comprobante.get("issueDate") or ""
    if limit and fecha and fecha < limit:
        return {
            "status": "mismatch",
            "documents": documents,
            "reason": f"{label} es del {fecha}, anterior al ingreso del {limit}: no puede ser esa salida.",
        }
    return {"status": "found", "documents": documents, "since": limit, "number": f"{punto}-{numero}"}
