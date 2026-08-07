from __future__ import annotations

import json
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

from django.db import connection
from django.utils import timezone

from .bejerman_delivery import (
    _bejerman_sql_database_for_company,
    _remote_bejerman_sql_csv,
    _remote_bejerman_sql_execute,
    _sql_literal,
)

REFERENCE_COMPANY_KEY = "SEPID"
TARGET_COMPANY_KEY = "MGBIO"

# Umbral validado contra los 97 casos conocidos de discrepancias_SEPID_MGBIO.xlsx:
# SequenceMatcher.ratio() sobre texto normalizado separa limpio los 20 "sustantivos"
# (máximo 0.52) de los 77 "cosméticos" (mínimo 0.57), sin errores de clasificación.
SIMILARITY_THRESHOLD = 0.55

DESCRIPTION_COLUMNS = ("art_DescGen", "art_DescAdic", "art_DescRedu")

# Resto de columnas de dbo.Articulos (sin PK ni auditoría), introspectadas el
# 27/07/2026 contra SBDASEP vía sys.columns. Se usan para la réplica de fila
# completa de los casos "sustantivos".
OTHER_COLUMNS = (
    "art_Gen", "artatr_Cod1", "artele_Cod1IN", "artatr_Cod2", "artele_Cod2IN", "artatr_Cod3", "artele_Cod3IN",
    "art_CodBarras", "artart_CodGenAlter", "artart_CodEle1Alter", "artart_CodEle2Alter", "artart_CodEle3Alter",
    "art_CodRedu", "artcla_Cod", "artele_Desc1", "artele_Desc2", "artele_Desc3",
    "artpro_Cod", "artda1_Cod", "artda2_Cod", "art_TipoTasaCpr", "arttiv_CodCpr", "artngr_CodCpr",
    "art_ImpCredFisc", "art_TipoTasaVta", "arttiv_CodVta", "artngr_CodVta", "art_BienUso", "art_Tipo",
    "art_CtrlStock", "art_StockPart", "art_StockSerie", "art_PesoBruto", "art_FactorConv", "art_CantEnvase",
    "artmon_CodigoPrBase", "artmtca_CodigoPrBase", "art_PrBase", "artbda_Cod", "art_CircStock", "art_CircCpra",
    "art_CircProd", "art_CircVta", "art_InclEnLisP", "art_LoteMultiplo", "art_LoteMinimo", "art_LoteMaximo",
    "artdep_Cod", "artagr_Cod", "art_TipoCons", "artdep_CodCons", "art_Clasif", "art_Foto", "art_RegAlic",
    "artrcf_Cod", "art_ProducSimultaneidad", "art_MascaraPart", "art_Prorrateo", "art_AplLoteAut", "art_NotasIngVta",
)

ALL_COLUMNS = DESCRIPTION_COLUMNS + OTHER_COLUMNS

# FKs de una sola columna: se copian solo si el valor de referencia está en
# blanco o existe en la tabla referenciada del lado destino.
FK_SINGLE_COLUMNS: dict[str, tuple[str, str]] = {
    "artcla_Cod": ("ClasArt", "cla_Cod"),
    "artpro_Cod": ("Proveed", "pro_Cod"),
    "artda1_Cod": ("Defi1Art", "da1_Cod"),
    "artda2_Cod": ("Defi2Art", "da2_Cod"),
    "artbda_Cod": ("BonDtoXArt", "bda_Cod"),
    "artdep_Cod": ("Deposito", "dep_Cod"),
    "artdep_CodCons": ("Deposito", "dep_Cod"),
    "artagr_Cod": ("AgrupCpra", "agr_Cod"),
}

# FKs compuestas (Elementos, auto-referencia a Articulos): validar existencia
# compuesta genéricamente no vale la pena para este catálogo (son campos de
# variante/artículo-alternativo que en la práctica están en blanco). Se copian
# solo cuando el valor de referencia está en blanco en las tres/cuatro columnas
# del grupo; si no, se dejan como estaban en destino y se marca el grupo como
# omitido.
FK_COMPOSITE_GROUPS: tuple[tuple[str, ...], ...] = (
    ("artatr_Cod1", "artele_Cod1IN"),
    ("artatr_Cod2", "artele_Cod2IN"),
    ("artatr_Cod3", "artele_Cod3IN"),
    ("artart_CodGenAlter", "artart_CodEle1Alter", "artart_CodEle2Alter", "artart_CodEle3Alter"),
)

SAFE_COLUMNS = tuple(
    col
    for col in OTHER_COLUMNS
    if col not in FK_SINGLE_COLUMNS and col not in {c for group in FK_COMPOSITE_GROUPS for c in group}
)

SYNC_USER_MARKER = "NEXORASYNC"

# Columnas float de Articulos: los datos de Bejerman usan coma decimal
# ("1628,16"), hay que normalizarlas a punto antes de mandarlas como literal
# numérico sin comillas (si no, SQL Server tira "Incorrect syntax").
FLOAT_COLUMNS = {
    "art_PesoBruto", "art_FactorConv", "art_CantEnvase", "art_PrBase",
    "art_LoteMultiplo", "art_LoteMinimo", "art_LoteMaximo",
}
# Columnas bit: se mandan como 0/1 sin comillas.
BIT_COLUMNS = {
    "art_Gen", "art_BienUso", "art_CtrlStock", "art_StockPart", "art_StockSerie",
    "art_CircStock", "art_CircCpra", "art_CircProd", "art_CircVta", "art_InclEnLisP",
    "art_RegAlic", "art_ProducSimultaneidad",
}


def _clean(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _sql_value_for_column(column: str, raw_value: Any) -> str:
    value = _clean(raw_value)
    if not value:
        # NULL, no ''. Un '' en una columna con FK (p.ej. artdep_Cod) viola la
        # constraint si '' no es un código real de la tabla referenciada.
        return "NULL"
    if column in FLOAT_COLUMNS:
        return value.replace(",", ".")
    if column in BIT_COLUMNS:
        return "1" if value.lower() in {"true", "1"} else "0"
    return _sql_literal(raw_value)


def _normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", _clean(value))
    text = text.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def classify_discrepancy(desc_reference: Any, desc_target: Any) -> str:
    ratio = SequenceMatcher(None, _normalize_text(desc_reference), _normalize_text(desc_target)).ratio()
    return "cosmetic" if ratio >= SIMILARITY_THRESHOLD else "sustantive"


def _fk_guard_expr(column: str, target_database: str) -> str:
    table, ref_column = FK_SINGLE_COLUMNS[column]
    return (
        f"CASE WHEN NULLIF(RTRIM(s.{column}), '') IS NULL "
        f"OR EXISTS (SELECT 1 FROM {target_database}.dbo.{table} t2 WHERE RTRIM(t2.{ref_column}) = RTRIM(s.{column})) "
        f"THEN 1 ELSE 0 END"
    )


def _diff_query(reference_database: str, target_database: str, codes: list[str] | None) -> str:
    select_parts = [
        "s.art_CodGen AS codigo",
        "s.art_CodEle1 AS cod_ele1",
        "s.art_CodEle2 AS cod_ele2",
        "s.art_CodEle3 AS cod_ele3",
    ]
    for col in ALL_COLUMNS:
        select_parts.append(f"s.{col} AS ref__{col}")
        select_parts.append(f"m.{col} AS target__{col}")
    for col in FK_SINGLE_COLUMNS:
        select_parts.append(f"{_fk_guard_expr(col, target_database)} AS fkok__{col}")

    where_parts = ["RTRIM(s.art_DescGen) <> RTRIM(m.art_DescGen)"]
    if codes:
        codes_sql = ", ".join(_sql_literal(code) for code in codes)
        where_parts.append(f"RTRIM(s.art_CodGen) IN ({codes_sql})")

    return f"""
SELECT {', '.join(select_parts)}
FROM {reference_database}.dbo.Articulos s
JOIN {target_database}.dbo.Articulos m
  ON RTRIM(s.art_CodGen) = RTRIM(m.art_CodGen)
 AND s.art_CodEle1 = m.art_CodEle1 AND s.art_CodEle2 = m.art_CodEle2 AND s.art_CodEle3 = m.art_CodEle3
WHERE {' AND '.join(where_parts)}
ORDER BY s.art_CodGen
""".strip()


def fetch_article_catalog_diff(
    *,
    reference: str = REFERENCE_COMPANY_KEY,
    target: str = TARGET_COMPANY_KEY,
    codes: list[str] | None = None,
) -> list[dict[str, Any]]:
    reference_database = _bejerman_sql_database_for_company(reference)
    target_database = _bejerman_sql_database_for_company(target)
    sql = _diff_query(reference_database, target_database, codes)
    return _remote_bejerman_sql_csv(sql, company_key=reference)


def _build_row_plan(row: dict[str, Any], *, include_sustantive: bool) -> dict[str, Any]:
    codigo = _clean(row.get("codigo"))
    desc_reference = row.get("ref__art_DescGen")
    desc_target = row.get("target__art_DescGen")
    classification = classify_discrepancy(desc_reference, desc_target)

    fields_changed: dict[str, dict[str, str]] = {}
    fk_skipped: list[str] = []

    if classification == "cosmetic":
        columns = DESCRIPTION_COLUMNS
        eligible = True
    else:
        eligible = include_sustantive
        columns = ALL_COLUMNS if eligible else ()

    for col in columns:
        ref_value = row.get(f"ref__{col}")
        target_value = row.get(f"target__{col}")
        if _clean(ref_value) == _clean(target_value):
            continue
        if col in FK_SINGLE_COLUMNS:
            if row.get(f"fkok__{col}") != "1":
                fk_skipped.append(col)
                continue
        fields_changed[col] = {"before": _clean(target_value), "after": _clean(ref_value)}

    if eligible and classification == "sustantive":
        for group in FK_COMPOSITE_GROUPS:
            ref_values = [_clean(row.get(f"ref__{col}")) for col in group]
            if any(ref_values):
                # Alguna columna del grupo tiene valor en SEPID: no se puede
                # garantizar que la combinación exista en destino sin validar
                # la FK compuesta, así que se deja como estaba y se marca.
                fk_skipped.extend(col for col in group if _clean(row.get(f"target__{col}")) != _clean(row.get(f"ref__{col}")))
                continue
            for col in group:
                target_value = row.get(f"target__{col}")
                if _clean(target_value):
                    fields_changed[col] = {"before": _clean(target_value), "after": ""}

    return {
        "code": codigo,
        "classification": classification,
        "similarity": round(
            SequenceMatcher(None, _normalize_text(desc_reference), _normalize_text(desc_target)).ratio(), 3
        ),
        "descriptionReference": _clean(desc_reference),
        "descriptionTarget": _clean(desc_target),
        "fieldsChanged": fields_changed,
        "fkColumnsSkipped": sorted(set(fk_skipped)),
        "eligible": eligible,
        "codElements": (row.get("cod_ele1"), row.get("cod_ele2"), row.get("cod_ele3")),
    }


def _build_update_sql(target_database: str, plan: dict[str, Any]) -> str:
    sets = [f"{col} = {_sql_value_for_column(col, values['after'])}" for col, values in plan["fieldsChanged"].items()]
    sets.append("art_FecMod = GETDATE()")
    sets.append(f"artusu_Codigo = {_sql_literal(SYNC_USER_MARKER)}")
    ele1, ele2, ele3 = plan["codElements"]
    return (
        f"UPDATE {target_database}.dbo.Articulos SET {', '.join(sets)} "
        f"WHERE RTRIM(art_CodGen) = {_sql_literal(plan['code'])} "
        f"AND art_CodEle1 = {_sql_literal(ele1)} AND art_CodEle2 = {_sql_literal(ele2)} AND art_CodEle3 = {_sql_literal(ele3)}"
    )


def _log_sync_result(plan: dict[str, Any], *, target: str, applied: bool, skip_reason: str | None) -> None:
    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO bejerman_article_sync_log
              (article_code, company_key, classification, fields_changed, applied, skip_reason, created_at)
            VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)
            """,
            [
                plan["code"],
                target,
                plan["classification"],
                json.dumps(plan["fieldsChanged"], ensure_ascii=False),
                applied,
                skip_reason,
                timezone.now(),
            ],
        )


def sync_article_catalog(
    *,
    reference: str = REFERENCE_COMPANY_KEY,
    target: str = TARGET_COMPANY_KEY,
    apply: bool = False,
    include_sustantive: bool = False,
    codes: list[str] | None = None,
) -> dict[str, Any]:
    target_database = _bejerman_sql_database_for_company(target)
    rows = fetch_article_catalog_diff(reference=reference, target=target, codes=codes)

    summary: dict[str, Any] = {
        "ok": True,
        "reference": reference,
        "target": target,
        "totalDiscrepancies": len(rows),
        "cosmetic": {"total": 0, "applied": 0},
        "sustantive": {"total": 0, "appliedFull": 0, "needsReview": 0},
        "errors": [],
        "rows": [],
    }

    for row in rows:
        plan = _build_row_plan(row, include_sustantive=include_sustantive)
        summary["rows"].append(plan)

        if plan["classification"] == "cosmetic":
            summary["cosmetic"]["total"] += 1
        else:
            summary["sustantive"]["total"] += 1

        if not plan["eligible"]:
            _log_sync_result(plan, target=target, applied=False, skip_reason="needs_review")
            summary["sustantive"]["needsReview"] += 1
            continue

        if not plan["fieldsChanged"]:
            _log_sync_result(plan, target=target, applied=False, skip_reason="no_changes")
            continue

        if not apply:
            _log_sync_result(plan, target=target, applied=False, skip_reason="dry_run")
            continue

        sql = _build_update_sql(target_database, plan)
        result = _remote_bejerman_sql_execute(sql, company_key=target)
        applied = result.get("status") == "success" and result.get("rowsAffected", 0) > 0
        if applied:
            if plan["classification"] == "cosmetic":
                summary["cosmetic"]["applied"] += 1
            else:
                summary["sustantive"]["appliedFull"] += 1
            _log_sync_result(plan, target=target, applied=True, skip_reason=None)
        else:
            summary["errors"].append({"code": plan["code"], "detail": result})
            _log_sync_result(plan, target=target, applied=False, skip_reason=f"sql_error:{result.get('status')}")

    return summary
