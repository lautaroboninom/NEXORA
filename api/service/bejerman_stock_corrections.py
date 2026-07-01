from __future__ import annotations

import csv
import hashlib
import json
import re
import unicodedata
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import connection
from django.utils import timezone

from .bejerman_companies import DEFAULT_INGRESS_COMPANY_KEY
from .bejerman_sdk import as_number, as_string, build_stock_rows, first_value
from .bejerman_sync import BejermanSDKClient, validate_bejerman_config


REPORT_FIELDS = [
    "status",
    "problem",
    "action",
    "company_key",
    "device_id",
    "ingreso_id",
    "numero_interno",
    "numero_serie",
    "articulo",
    "depositos",
    "total_disponible",
    "deposito_a_reducir",
    "cantidad",
    "motivo",
    "aprobado_por",
    "stock_hash",
    "post_status",
    "usuario_bejerman",
    "fecha_stock",
    "run_id",
    "error",
    "respuesta",
]

USER_FIELDS = (
    "stp_UsuAct",
    "Usuario",
    "UsuarioAlta",
    "UsuarioModificacion",
    "Comprobante_Usuario",
    "mst_Usuario",
)
DATE_FIELDS = (
    "stp_FecAct",
    "Fecha",
    "FechaAlta",
    "FechaModificacion",
    "Comprobante_FechaEmision",
    "mst_Fecha",
)


@dataclass(frozen=True)
class ApprovedStockCorrection:
    numero_serie: str
    articulo: str
    deposito_a_reducir: str
    cantidad: float
    motivo: str
    aprobado_por: str
    expected_stock_hash: str = ""


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _norm(value: Any) -> str:
    return _clean(value).upper()


def _normalize_header(value: Any) -> str:
    text = unicodedata.normalize("NFKD", _clean(value).lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", text)


def _pick(row: dict[str, Any], *names: str) -> str:
    normalized = {_normalize_header(key): value for key, value in row.items()}
    for name in names:
        value = normalized.get(_normalize_header(name))
        if value not in (None, ""):
            return _clean(value)
    return ""


def _approved(value: Any) -> bool:
    if value in (None, ""):
        return True
    text = unicodedata.normalize("NFKD", _norm(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text in {"SI", "S", "YES", "Y", "TRUE", "1", "APROBADO", "APPROVED"}


def _positive_quantity(value: Any) -> float:
    qty = as_number(value)
    if qty is None or qty <= 0:
        raise ValueError("La cantidad debe ser mayor a cero.")
    return float(qty)


def _format_qty(value: Any) -> str:
    qty = as_number(value)
    if qty is None:
        return ""
    if abs(qty - int(qty)) < 0.000001:
        return str(int(qty))
    return f"{qty:.6f}".rstrip("0").rstrip(".")


def _payload_qty(value: float) -> int | float:
    return int(value) if abs(value - int(value)) < 0.000001 else value


def _stock_quantity(row: dict[str, Any]) -> float:
    available = as_number(row.get("availableQuantity"))
    if available is not None:
        return float(available)
    real = as_number(row.get("realQuantity"))
    committed = as_number(row.get("committedQuantity"))
    if real is None:
        return 0.0
    return float(real) - float(committed or 0)


def _stock_rows_for_serial(response: dict[str, Any], serial: str) -> list[dict[str, Any]]:
    serial_norm = _norm(serial)
    rows: list[dict[str, Any]] = []
    for row in build_stock_rows(response):
        partida = as_string(row.get("partida"))
        if partida and _norm(partida) != serial_norm:
            continue
        quantity = _stock_quantity(row)
        if quantity <= 0:
            continue
        rows.append({**row, "effectiveQuantity": quantity})
    return rows


def _group_stock(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        article = as_string(row.get("articleCode")).strip()
        deposit = as_string(row.get("depositCode")).strip().upper()
        if not article or not deposit:
            continue
        key = (article, deposit)
        current = grouped.setdefault(
            key,
            {
                "articulo": article,
                "deposito": deposit,
                "cantidad": 0.0,
                "rows": [],
            },
        )
        current["cantidad"] = float(current["cantidad"]) + _stock_quantity(row)
        current["rows"].append(row)
    return sorted(grouped.values(), key=lambda item: (item["articulo"], item["deposito"]))


def _stock_hash(groups: list[dict[str, Any]]) -> str:
    signature = "|".join(
        f"{item['articulo']}:{item['deposito']}:{_format_qty(item['cantidad'])}"
        for item in groups
        if float(item.get("cantidad") or 0) > 0
    )
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]


def _stock_label(groups: list[dict[str, Any]]) -> str:
    return "; ".join(
        f"{item['deposito']}={_format_qty(item['cantidad'])} (art {item['articulo']})"
        for item in groups
    )


def _raw_summary(groups: list[dict[str, Any]], fields: tuple[str, ...]) -> str:
    values: list[str] = []
    for group in groups:
        for row in group.get("rows") or []:
            raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
            value = as_string(first_value(raw, fields))
            if value and value not in values:
                values.append(value)
    return ", ".join(values[:5])


def _table_has_column(table: str, column: str) -> bool:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT 1
              FROM information_schema.columns
             WHERE table_schema = ANY(current_schemas(true))
               AND table_name = %s
               AND column_name = %s
             LIMIT 1
            """,
            [table, column],
        )
        return bool(cur.fetchone())


def _device_contexts(
    *,
    serials: list[str] | None = None,
    limit: int = 250,
    default_company_key: str = DEFAULT_INGRESS_COMPANY_KEY,
) -> list[dict[str, Any]]:
    serial_filter = ""
    params: list[Any] = []
    normalized_serials = sorted({_norm(serial) for serial in (serials or []) if _clean(serial)})
    if normalized_serials:
        serial_filter = "AND UPPER(TRIM(d.numero_serie)) = ANY(%s)"
        params.append(normalized_serials)

    latest_company_expr = (
        "COALESCE(NULLIF(i.empresa_bejerman, ''), '') AS empresa_bejerman"
        if _table_has_column("ingresos", "empresa_bejerman")
        else "'' AS empresa_bejerman"
    )
    latest_date_expr = (
        "COALESCE(i.fecha_ingreso, i.fecha_creacion)"
        if _table_has_column("ingresos", "fecha_ingreso")
        else "i.fecha_creacion"
    )
    params.append(max(1, int(limit or 1)))
    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT d.id AS device_id,
                   COALESCE(d.numero_serie, '') AS numero_serie,
                   COALESCE(d.numero_interno, '') AS numero_interno,
                   COALESCE(ma.nombre, '') AS marca,
                   COALESCE(m.nombre, '') AS modelo,
                   latest.id AS ingreso_id,
                   COALESCE(latest.estado, '') AS ingreso_estado,
                   COALESCE(NULLIF(latest.empresa_bejerman, ''), %s) AS empresa_bejerman
              FROM devices d
              LEFT JOIN marcas ma ON ma.id = d.marca_id
              LEFT JOIN models m ON m.id = d.model_id
              LEFT JOIN LATERAL (
                    SELECT i.id,
                           i.estado,
                           {latest_company_expr},
                           {latest_date_expr} AS sort_date
                      FROM ingresos i
                     WHERE i.device_id = d.id
                     ORDER BY {latest_date_expr} DESC NULLS LAST, i.id DESC
                     LIMIT 1
              ) latest ON TRUE
             WHERE NULLIF(TRIM(d.numero_serie), '') IS NOT NULL
               {serial_filter}
             ORDER BY d.id
             LIMIT %s
            """,
            [default_company_key, *params],
        )
        cols = [col[0] for col in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _context_by_serial(serials: list[str], *, default_company_key: str) -> dict[str, dict[str, Any]]:
    contexts = _device_contexts(serials=serials, limit=max(1, len(serials)), default_company_key=default_company_key)
    return {_norm(row.get("numero_serie")): row for row in contexts}


def _base_report_row(
    context: dict[str, Any],
    *,
    company_key: str,
    groups: list[dict[str, Any]],
    run_id: str = "",
) -> dict[str, Any]:
    return {
        "company_key": company_key,
        "device_id": context.get("device_id") or "",
        "ingreso_id": context.get("ingreso_id") or "",
        "numero_interno": context.get("numero_interno") or "",
        "numero_serie": context.get("numero_serie") or "",
        "articulo": ", ".join(sorted({item["articulo"] for item in groups if item.get("articulo")})),
        "depositos": _stock_label(groups),
        "total_disponible": _format_qty(sum(float(item.get("cantidad") or 0) for item in groups)),
        "stock_hash": _stock_hash(groups),
        "usuario_bejerman": _raw_summary(groups, USER_FIELDS),
        "fecha_stock": _raw_summary(groups, DATE_FIELDS),
        "run_id": run_id,
    }


def _issue_rows_for_context(
    context: dict[str, Any],
    *,
    company_key: str,
    stock_response: dict[str, Any],
    run_id: str = "",
) -> list[dict[str, Any]]:
    serial = as_string(context.get("numero_serie"))
    groups = _group_stock(_stock_rows_for_serial(stock_response, serial))
    if not groups:
        return []
    total = sum(float(item.get("cantidad") or 0) for item in groups)
    if total <= 1:
        return []

    base = _base_report_row(context, company_key=company_key, groups=groups, run_id=run_id)
    articles = sorted({item["articulo"] for item in groups if item.get("articulo")})
    deposits = sorted({item["deposito"] for item in groups if item.get("deposito")})
    if len(articles) != 1:
        return [
            {
                **base,
                "status": "blocked",
                "problem": "multiple_articles",
                "action": "manual_review",
                "error": "La misma partida aparece con más de un artículo.",
            }
        ]

    rows: list[dict[str, Any]] = []
    same_deposit_groups = [item for item in groups if float(item.get("cantidad") or 0) > 1]
    for group in same_deposit_groups:
        quantity = float(group["cantidad"]) - 1
        rows.append(
            {
                **base,
                "status": "auto_candidate",
                "problem": "same_deposit_quantity",
                "action": "sal_err",
                "articulo": group["articulo"],
                "deposito_a_reducir": group["deposito"],
                "cantidad": _format_qty(quantity),
                "motivo": "Duplicado en el mismo depósito",
                "post_status": "multi_deposit_needs_approval" if len(deposits) > 1 else "resolved_after_apply",
            }
        )

    if len(deposits) > 1:
        rows.append(
            {
                **base,
                "status": "needs_approval",
                "problem": "multi_deposit_positive",
                "action": "approved_csv_required",
                "articulo": articles[0],
                "motivo": "Requiere indicar qué depósito se reduce.",
            }
        )
    return rows


def timestamp_run_id() -> str:
    return timezone.localtime().strftime("%Y%m%d_%H%M%S")


def build_stock_correction_sal_payload(
    *,
    numero_serie: str,
    articulo: str,
    deposito: str,
    cantidad: float,
    run_id: str,
    sequence: int = 1,
    motivo: str = "",
) -> list[dict[str, Any]]:
    qty = _positive_quantity(cantidad)
    comprobante = _clean(getattr(settings, "BEJERMAN_STOCK_CORRECTION_COMPROBANTE", "SAL")) or "SAL"
    causa = _clean(getattr(settings, "BEJERMAN_STOCK_CORRECTION_CAUSA_EMISION", "ERR")) or "ERR"
    origin = f"NEXORA-STOCKFIX-{run_id}-{numero_serie}-{deposito}-{sequence}"
    crc = zlib.crc32(origin.encode("utf-8")) % 10000000
    document_number = f"{90000000 + crc:08d}"
    payload_qty = _payload_qty(qty)
    return [
        {
            "Comprobante_FechaEmision": timezone.localdate().isoformat(),
            "Comprobante_Tipo": comprobante,
            "Comprobante_Letra": "",
            "Comprobante_PtoVenta": "",
            "Comprobante_Numero": document_number,
            "Comprobante_Art_CodGen": _clean(articulo),
            "Comprobante_ArtPartida": _clean(numero_serie),
            "Comprobante_DescArticulo": f"NEXORA {_clean(numero_serie)}"[:50],
            "Comprobante_ArtCodTipo": "1",
            "Comprobante_ArtDeposito": _clean(deposito).upper(),
            "Comprobante_CantidadUM1": -payload_qty,
            "Comprobante_CantidadUM2": -payload_qty,
            "Comprobante_PrecioTotalMonLocal": 0,
            "Comprobante_CodigoCausaEmision": causa,
            "Comprobante_IdOrigen": origin[:80],
            "Partida_Observaciones": (motivo or f"STOCKFIX {run_id}")[:20],
        }
    ]


def audit_duplicate_stock(
    *,
    client: BejermanSDKClient | None = None,
    company_key: str = DEFAULT_INGRESS_COMPANY_KEY,
    serials: list[str] | None = None,
    limit: int = 250,
    run_id: str | None = None,
) -> dict[str, Any]:
    validate_bejerman_config()
    run_id = run_id or timestamp_run_id()
    client = client or BejermanSDKClient(company_key=company_key, allow_system_credentials=True)
    contexts = _device_contexts(serials=serials, limit=limit, default_company_key=company_key)
    items: list[dict[str, Any]] = []
    errors = 0
    for context in contexts:
        serial = as_string(context.get("numero_serie"))
        try:
            response = client.stock_by_deposit_partida("", serial)
            items.extend(
                _issue_rows_for_context(context, company_key=company_key, stock_response=response, run_id=run_id)
            )
        except Exception as exc:
            errors += 1
            items.append(
                {
                    **_base_report_row(context, company_key=company_key, groups=[], run_id=run_id),
                    "status": "error",
                    "problem": "stock_lookup_failed",
                    "action": "retry",
                    "error": str(exc),
                }
            )
    return _stats_from_items(items, checked=len(contexts), errors=errors)


def _stats_from_items(items: list[dict[str, Any]], *, checked: int, errors: int = 0) -> dict[str, Any]:
    stats = {
        "checked": checked,
        "issues": len([item for item in items if item.get("problem")]),
        "auto_candidates": len([item for item in items if item.get("status") == "auto_candidate"]),
        "needs_approval": len([item for item in items if item.get("status") == "needs_approval"]),
        "applied": len([item for item in items if item.get("status") == "applied"]),
        "blocked": len([item for item in items if item.get("status") == "blocked"]),
        "skipped": len([item for item in items if item.get("status") == "skipped"]),
        "errors": errors + len([item for item in items if item.get("status") == "error"]),
        "items": items,
    }
    return stats


def _json_summary(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)[:4000]


def apply_single_stock_correction(
    *,
    client: BejermanSDKClient,
    company_key: str,
    context: dict[str, Any],
    numero_serie: str,
    articulo: str,
    deposito_a_reducir: str,
    cantidad: float,
    motivo: str,
    aprobado_por: str = "",
    expected_stock_hash: str = "",
    run_id: str,
    sequence: int,
    same_deposit_only: bool = False,
) -> dict[str, Any]:
    response = client.stock_by_deposit_partida("", numero_serie)
    groups = _group_stock(_stock_rows_for_serial(response, numero_serie))
    base = _base_report_row(
        {**context, "numero_serie": numero_serie},
        company_key=company_key,
        groups=groups,
        run_id=run_id,
    )
    current_hash = _stock_hash(groups)
    if expected_stock_hash and expected_stock_hash != current_hash:
        return {
            **base,
            "status": "blocked",
            "problem": "stock_changed",
            "action": "skip",
            "articulo": articulo,
            "deposito_a_reducir": deposito_a_reducir,
            "cantidad": _format_qty(cantidad),
            "motivo": motivo,
            "aprobado_por": aprobado_por,
            "error": "El stock cambió desde el dry-run; no se emitió SAL.",
        }

    target_group = None
    for group in groups:
        if _norm(group.get("articulo")) == _norm(articulo) and _norm(group.get("deposito")) == _norm(deposito_a_reducir):
            target_group = group
            break
    if not target_group:
        return {
            **base,
            "status": "blocked",
            "problem": "stock_not_found",
            "action": "skip",
            "articulo": articulo,
            "deposito_a_reducir": deposito_a_reducir,
            "cantidad": _format_qty(cantidad),
            "motivo": motivo,
            "aprobado_por": aprobado_por,
            "error": "No hay stock positivo suficiente en el depósito aprobado.",
        }

    qty = _positive_quantity(cantidad)
    deposit_quantity = float(target_group.get("cantidad") or 0)
    if deposit_quantity < qty:
        return {
            **base,
            "status": "blocked",
            "problem": "insufficient_stock",
            "action": "skip",
            "articulo": articulo,
            "deposito_a_reducir": deposito_a_reducir,
            "cantidad": _format_qty(qty),
            "motivo": motivo,
            "aprobado_por": aprobado_por,
            "error": "La cantidad aprobada supera el stock disponible actual.",
        }
    if same_deposit_only and abs((deposit_quantity - qty) - 1) > 0.000001:
        return {
            **base,
            "status": "blocked",
            "problem": "auto_rule_no_longer_matches",
            "action": "skip",
            "articulo": articulo,
            "deposito_a_reducir": deposito_a_reducir,
            "cantidad": _format_qty(qty),
            "motivo": motivo,
            "aprobado_por": aprobado_por,
            "error": "El duplicado del depósito ya no coincide con la regla automática.",
        }

    article_total = sum(
        float(group.get("cantidad") or 0)
        for group in groups
        if _norm(group.get("articulo")) == _norm(articulo)
    )
    if article_total - qty < 1:
        return {
            **base,
            "status": "blocked",
            "problem": "would_leave_zero_stock",
            "action": "skip",
            "articulo": articulo,
            "deposito_a_reducir": deposito_a_reducir,
            "cantidad": _format_qty(qty),
            "motivo": motivo,
            "aprobado_por": aprobado_por,
            "error": "La corrección dejaría la partida sin stock positivo.",
        }

    payload = build_stock_correction_sal_payload(
        numero_serie=numero_serie,
        articulo=articulo,
        deposito=deposito_a_reducir,
        cantidad=qty,
        run_id=run_id,
        sequence=sequence,
        motivo=motivo,
    )
    response = client.ingresar_lista_comprobantes_json(payload)
    return {
        **base,
        "status": "applied",
        "problem": "same_deposit_quantity" if same_deposit_only else "approved_correction",
        "action": "sal_err",
        "articulo": articulo,
        "deposito_a_reducir": deposito_a_reducir,
        "cantidad": _format_qty(qty),
        "motivo": motivo,
        "aprobado_por": aprobado_por,
        "respuesta": _json_summary(response),
    }


def apply_duplicate_stock_corrections(
    *,
    client: BejermanSDKClient | None = None,
    company_key: str = DEFAULT_INGRESS_COMPANY_KEY,
    serials: list[str] | None = None,
    limit: int = 250,
    run_id: str | None = None,
) -> dict[str, Any]:
    validate_bejerman_config()
    validate_bejerman_config(comprobante="BEJERMAN_STOCK_CORRECTION_COMPROBANTE")
    run_id = run_id or timestamp_run_id()
    client = client or BejermanSDKClient(company_key=company_key, allow_system_credentials=True)
    audit = audit_duplicate_stock(
        client=client,
        company_key=company_key,
        serials=serials,
        limit=limit,
        run_id=run_id,
    )
    contexts = _context_by_serial(
        [item.get("numero_serie") for item in audit["items"] if item.get("numero_serie")],
        default_company_key=company_key,
    )
    results: list[dict[str, Any]] = []
    for sequence, item in enumerate([row for row in audit["items"] if row.get("status") == "auto_candidate"], 1):
        try:
            serial = item["numero_serie"]
            results.append(
                apply_single_stock_correction(
                    client=client,
                    company_key=company_key,
                    context=contexts.get(_norm(serial), {}),
                    numero_serie=serial,
                    articulo=item["articulo"],
                    deposito_a_reducir=item["deposito_a_reducir"],
                    cantidad=_positive_quantity(item["cantidad"]),
                    motivo=item.get("motivo") or "Duplicado en el mismo depósito",
                    expected_stock_hash=item.get("stock_hash") or "",
                    run_id=run_id,
                    sequence=sequence,
                    same_deposit_only=True,
                )
            )
        except Exception as exc:
            results.append({**item, "status": "error", "action": "skip", "error": str(exc)})

    stats = _stats_from_items(results, checked=audit["checked"])
    stats["issues"] = audit["issues"]
    stats["auto_candidates"] = audit["auto_candidates"]
    stats["needs_approval"] = audit["needs_approval"]
    return stats


def load_approved_corrections(path: str | Path) -> list[ApprovedStockCorrection]:
    in_path = Path(path)
    if not in_path.exists():
        raise ValueError(f"No existe el archivo aprobado: {in_path}")
    corrections: list[ApprovedStockCorrection] = []
    with in_path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if not _approved(_pick(row, "approved", "aprobado")):
                continue
            serial = _pick(row, "numero_serie", "serial", "partida")
            article = _pick(row, "articulo", "article_code", "articulo_codigo")
            deposit = _pick(row, "deposito_a_reducir", "deposit_to_reduce", "deposito")
            quantity = _pick(row, "cantidad", "qty", "qty_to_reduce")
            reason = _pick(row, "motivo", "reason")
            approved_by = _pick(row, "aprobado_por", "approved_by")
            if not serial or not article or not deposit or not quantity:
                raise ValueError("El CSV aprobado requiere numero_serie, articulo, deposito_a_reducir y cantidad.")
            if not reason or not approved_by:
                raise ValueError("El CSV aprobado requiere motivo y aprobado_por.")
            corrections.append(
                ApprovedStockCorrection(
                    numero_serie=serial,
                    articulo=article,
                    deposito_a_reducir=deposit.upper(),
                    cantidad=_positive_quantity(quantity),
                    motivo=reason,
                    aprobado_por=approved_by,
                    expected_stock_hash=_pick(row, "stock_hash", "expected_stock_hash"),
                )
            )
    return corrections


def apply_approved_stock_corrections(
    corrections: list[ApprovedStockCorrection],
    *,
    client: BejermanSDKClient | None = None,
    company_key: str = DEFAULT_INGRESS_COMPANY_KEY,
    limit: int = 250,
    run_id: str | None = None,
) -> dict[str, Any]:
    validate_bejerman_config()
    validate_bejerman_config(comprobante="BEJERMAN_STOCK_CORRECTION_COMPROBANTE")
    run_id = run_id or timestamp_run_id()
    client = client or BejermanSDKClient(company_key=company_key, allow_system_credentials=True)
    selected = corrections[: max(1, int(limit or 1))]
    contexts = _context_by_serial([item.numero_serie for item in selected], default_company_key=company_key)
    results: list[dict[str, Any]] = []
    for sequence, correction in enumerate(selected, 1):
        context = contexts.get(_norm(correction.numero_serie))
        if not context:
            results.append(
                {
                    "status": "blocked",
                    "problem": "unknown_serial",
                    "action": "skip",
                    "company_key": company_key,
                    "numero_serie": correction.numero_serie,
                    "articulo": correction.articulo,
                    "deposito_a_reducir": correction.deposito_a_reducir,
                    "cantidad": _format_qty(correction.cantidad),
                    "motivo": correction.motivo,
                    "aprobado_por": correction.aprobado_por,
                    "run_id": run_id,
                    "error": "El número de serie no existe en NEXORA.",
                }
            )
            continue
        try:
            results.append(
                apply_single_stock_correction(
                    client=client,
                    company_key=company_key,
                    context=context,
                    numero_serie=correction.numero_serie,
                    articulo=correction.articulo,
                    deposito_a_reducir=correction.deposito_a_reducir,
                    cantidad=correction.cantidad,
                    motivo=correction.motivo,
                    aprobado_por=correction.aprobado_por,
                    expected_stock_hash=correction.expected_stock_hash,
                    run_id=run_id,
                    sequence=sequence,
                )
            )
        except Exception as exc:
            results.append(
                {
                    **context,
                    "status": "error",
                    "problem": "apply_failed",
                    "action": "skip",
                    "company_key": company_key,
                    "numero_serie": correction.numero_serie,
                    "articulo": correction.articulo,
                    "deposito_a_reducir": correction.deposito_a_reducir,
                    "cantidad": _format_qty(correction.cantidad),
                    "motivo": correction.motivo,
                    "aprobado_por": correction.aprobado_por,
                    "run_id": run_id,
                    "error": str(exc),
                }
            )
    return _stats_from_items(results, checked=len(selected))


def default_report_path(run_id: str | None = None) -> Path:
    return Path("informes") / f"bejerman_stock_fix_duplicates_{run_id or timestamp_run_id()}.csv"


def write_report(path: str | Path, rows: list[dict[str, Any]]) -> Path:
    out_path = Path(path)
    if out_path.suffix.lower() != ".csv":
        out_path = out_path / f"bejerman_stock_fix_duplicates_{timestamp_run_id()}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(REPORT_FIELDS)
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})
    return out_path
