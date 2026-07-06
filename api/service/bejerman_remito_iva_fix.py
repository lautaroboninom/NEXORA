from __future__ import annotations

import csv
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from django.db import connection

from .bejerman_documents import parse_bejerman_remito_number


REMITO_IVA_TYPES = {"RT", "RD", "RSS"}
DEFAULT_TEST_DATABASES = {
    "SEPID": "SBDPSEP",
    "MGBIO": "SBDPMGBI",
}
DEFAULT_PRODUCTION_DATABASES = {
    "SEPID": "SBDASEP",
    "MGBIO": "SBDAMGBI",
}
DELIVERY_TYPES = {"sale", "service_release"}
REPAIR_CORRECTION_MODE = "repair_21"
SALE_CORRECTION_MODE = "sale_article"


@dataclass(frozen=True)
class RemitoIvaTarget:
    company_key: str
    group_id: str
    remito_number: str
    tipo: str
    letra: str
    punto: str
    numero: str
    customer_code: str
    customer_name: str
    order_numbers: str
    delivery_types: str
    correction_mode: str
    generated_at: str


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def clean_upper(value: Any) -> str:
    return clean_text(value).upper()


def timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def default_out_dir() -> str:
    return str(Path("informes") / f"bejerman_remitos_iva_{timestamp_slug()}")


def default_test_database(company_key: Any) -> str:
    return DEFAULT_TEST_DATABASES.get(clean_upper(company_key), "SBDPSEP")


def default_production_database(company_key: Any) -> str:
    return DEFAULT_PRODUCTION_DATABASES.get(clean_upper(company_key), "SBDASEP")


def sql_literal(value: Any) -> str:
    return "'" + clean_text(value).replace("'", "''") + "'"


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _parse_remito(row: dict[str, Any]) -> dict[str, str]:
    default_type = clean_upper(row.get("comprobante_tipo")) or "RT"
    default_point = clean_text(row.get("comprobante_pto_venta"))
    return parse_bejerman_remito_number(
        row.get("remito_number"),
        default_type=default_type,
        default_letter=clean_upper(row.get("comprobante_letra")) or "R",
        allowed_types=REMITO_IVA_TYPES,
        default_point=default_point,
        allow_number_only=bool(default_point),
        require_complete=True,
    )


def correction_mode_from_delivery_types(value: Any) -> str:
    raw = {clean_text(item).lower() for item in re.split(r"[,;]", clean_text(value)) if clean_text(item)}
    if raw and raw.issubset({"service_release"}):
        return REPAIR_CORRECTION_MODE
    return SALE_CORRECTION_MODE


def target_from_row(row: dict[str, Any]) -> RemitoIvaTarget:
    parsed = _parse_remito(row)
    delivery_types = clean_text(row.get("delivery_types"))
    correction_mode = clean_text(row.get("correction_mode")) or correction_mode_from_delivery_types(delivery_types)
    return RemitoIvaTarget(
        company_key=clean_upper(row.get("company_key")) or "SEPID",
        group_id=clean_text(row.get("group_id")),
        remito_number=parsed["remitoNumber"],
        tipo=parsed["type"],
        letra=parsed["letter"],
        punto=parsed["point"],
        numero=parsed["number"],
        customer_code=clean_text(row.get("customer_code")),
        customer_name=clean_text(row.get("customer_name")),
        order_numbers=clean_text(row.get("order_numbers")),
        delivery_types=delivery_types,
        correction_mode=correction_mode,
        generated_at=clean_text(row.get("generated_at")),
    )


def collect_local_remito_iva_targets(
    *,
    company_key: str = "",
    from_date: str = "",
    to_date: str = "",
    limit: int = 0,
    include_existing: bool = False,
    delivery_types: Iterable[str] | None = None,
) -> list[RemitoIvaTarget]:
    selected_types = {clean_text(item).lower() for item in (delivery_types or DELIVERY_TYPES) if clean_text(item)}
    selected_types = selected_types or set(DELIVERY_TYPES)
    filters = [
        "COALESCE(g.status, '') IN ('generated','reconciled')",
        "NULLIF(BTRIM(COALESCE(g.remito_number, o.remito_number, '')), '') IS NOT NULL",
        "o.delivery_type = ANY(%s)",
    ]
    params: list[Any] = [sorted(selected_types)]
    if not include_existing:
        filters.append(
            """
            LOWER(COALESCE(
              g.response_summary->>'documentMode',
              g.response_summary->>'document_mode',
              g.response_summary->>'mode',
              ''
            )) NOT IN ('existing','register','registered','registrado')
            """
        )
    if clean_text(company_key):
        filters.append("UPPER(COALESCE(g.company_key, o.company_key, 'SEPID')) = %s")
        params.append(clean_upper(company_key))
    if clean_text(from_date):
        filters.append("COALESCE(g.generated_at, o.updated_at, o.created_at)::date >= %s::date")
        params.append(clean_text(from_date))
    if clean_text(to_date):
        filters.append("COALESCE(g.generated_at, o.updated_at, o.created_at)::date <= %s::date")
        params.append(clean_text(to_date))

    limit_sql = "LIMIT %s" if limit and limit > 0 else ""
    if limit_sql:
        params.append(int(limit))

    sql = f"""
        SELECT
          g.id AS group_id,
          COALESCE(g.company_key, o.company_key, 'SEPID') AS company_key,
          g.comprobante_tipo,
          COALESCE(g.comprobante_letra, 'R') AS comprobante_letra,
          g.comprobante_pto_venta,
          COALESCE(g.remito_number, o.remito_number) AS remito_number,
          g.customer_code,
          g.customer_name,
          STRING_AGG(DISTINCT o.order_number, ', ' ORDER BY o.order_number) AS order_numbers,
          STRING_AGG(DISTINCT o.delivery_type, ', ' ORDER BY o.delivery_type) AS delivery_types,
          MAX(COALESCE(g.generated_at, o.updated_at, o.created_at)) AS generated_at
        FROM bejerman_remito_groups g
        JOIN delivery_orders o ON o.bejerman_remito_group_id = g.id
        WHERE {" AND ".join(filters)}
        GROUP BY
          g.id, COALESCE(g.company_key, o.company_key, 'SEPID'), g.comprobante_tipo,
          COALESCE(g.comprobante_letra, 'R'), g.comprobante_pto_venta,
          COALESCE(g.remito_number, o.remito_number), g.customer_code, g.customer_name
        ORDER BY MAX(COALESCE(g.generated_at, o.updated_at, o.created_at))
        {limit_sql}
    """
    with connection.cursor() as cur:
        cur.execute(sql, params)
        columns = [col[0] for col in cur.description]
        rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    targets: list[RemitoIvaTarget] = []
    for row in rows:
        target = target_from_row(row)
        if target.tipo in REMITO_IVA_TYPES:
            targets.append(target)
    return targets


def _safe_sql_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", clean_text(value))[:96] or f"NEXORA_BKP_REMITOS_IVA_{timestamp_slug()}"


def _values_sql(targets: list[RemitoIvaTarget]) -> str:
    rows = []
    for target in targets:
        rows.append(
            "("
            + ", ".join(
                [
                    sql_literal(target.company_key),
                    sql_literal(target.group_id),
                    sql_literal(target.remito_number),
                    sql_literal(target.tipo),
                    sql_literal(target.letra),
                    sql_literal(target.punto),
                    sql_literal(target.numero),
                    sql_literal(target.customer_code),
                    sql_literal(target.customer_name),
                    sql_literal(target.order_numbers),
                    sql_literal(target.delivery_types),
                    sql_literal(target.correction_mode),
                    sql_literal(target.generated_at),
                ]
            )
            + ")"
        )
    return ",\n".join(rows)


def build_bejerman_iva_fix_sql(
    targets: Iterable[RemitoIvaTarget],
    *,
    database: str,
    apply: bool = False,
    backup_name: str = "",
) -> str:
    target_list = list(targets)
    database_name = clean_text(database) or "SBDPSEP"
    safe_database = database_name.replace("]", "]]")
    backup = _safe_sql_name(backup_name or f"NEXORA_BKP_REMITOS_IVA_{timestamp_slug()}")
    apply_flag = "1" if apply else "0"

    header = [
        "SET NOCOUNT ON;",
        f"USE [{safe_database}];",
        "",
        f"DECLARE @Apply bit = {apply_flag};",
        f"DECLARE @BackupPrefix sysname = N'{backup}';",
        "",
        "IF OBJECT_ID('tempdb..#NexoraRemitoIvaTargets') IS NOT NULL",
        "    DROP TABLE #NexoraRemitoIvaTargets;",
        "",
        "CREATE TABLE #NexoraRemitoIvaTargets (",
        "    company_key nvarchar(20) NOT NULL,",
        "    group_id nvarchar(80) NOT NULL,",
        "    remito_number nvarchar(40) NOT NULL,",
        "    tipo varchar(8) NOT NULL,",
        "    letra varchar(2) NOT NULL,",
        "    punto varchar(5) NOT NULL,",
        "    numero varchar(8) NOT NULL,",
        "    customer_code nvarchar(30) NOT NULL,",
        "    customer_name nvarchar(200) NOT NULL,",
        "    order_numbers nvarchar(4000) NOT NULL,",
        "    delivery_types nvarchar(200) NOT NULL,",
        "    correction_mode varchar(20) NOT NULL,",
        "    generated_at nvarchar(40) NULL",
        ");",
        "",
    ]
    if target_list:
        header.extend(
            [
                "INSERT INTO #NexoraRemitoIvaTargets (",
                "    company_key, group_id, remito_number, tipo, letra, punto, numero,",
                "    customer_code, customer_name, order_numbers, delivery_types, correction_mode, generated_at",
                ")",
                "VALUES",
                _values_sql(target_list) + ";",
                "",
            ]
        )
    else:
        header.append("-- No hay remitos candidatos en la entrada generada.")

    body = f"""
IF OBJECT_ID('tempdb..#NexoraIvaRateMap') IS NOT NULL DROP TABLE #NexoraIvaRateMap;
CREATE TABLE #NexoraIvaRateMap(tiv_cod varchar(8) NOT NULL PRIMARY KEY, tasa decimal(9,4) NOT NULL);
INSERT INTO #NexoraIvaRateMap(tiv_cod, tasa) VALUES ('01', 21), ('03', 10.5), ('04', 0);

IF OBJECT_ID('tempdb..#MatchCounts') IS NOT NULL DROP TABLE #MatchCounts;
SELECT
  tgt.group_id,
  tgt.remito_number,
  COUNT(c.scv_ID) AS matches
INTO #MatchCounts
FROM #NexoraRemitoIvaTargets tgt
LEFT JOIN SegTiposV t
  ON t.spvtco_Cod = tgt.tipo
 AND t.spv_Letra = tgt.letra
 AND t.spv_CodPvt = tgt.punto
 AND t.spv_Nro = tgt.numero
LEFT JOIN SegCabV c
  ON c.scv_ID = t.spvscv_ID
 AND (
      NULLIF(LTRIM(RTRIM(tgt.customer_code)), '') IS NULL
      OR LTRIM(RTRIM(c.scvcli_Cod)) = LTRIM(RTRIM(tgt.customer_code))
 )
GROUP BY tgt.group_id, tgt.remito_number;

SELECT *
  FROM #MatchCounts
 WHERE matches <> 1
 ORDER BY remito_number;

IF OBJECT_ID('tempdb..#MatchedDocs') IS NOT NULL DROP TABLE #MatchedDocs;
SELECT
  tgt.*,
  c.scvemp_Codigo,
  c.scvsuc_Cod,
  c.scv_ID,
  c.scv_FContab,
  c.scv_FDDJJ,
  c.scv_IncluDDJJ,
  c.scv_FEmision,
  c.scvcli_Cod,
  c.scvcli_RazSoc
INTO #MatchedDocs
FROM #NexoraRemitoIvaTargets tgt
JOIN SegTiposV t
  ON t.spvtco_Cod = tgt.tipo
 AND t.spv_Letra = tgt.letra
 AND t.spv_CodPvt = tgt.punto
 AND t.spv_Nro = tgt.numero
JOIN SegCabV c
  ON c.scv_ID = t.spvscv_ID
 AND (
      NULLIF(LTRIM(RTRIM(tgt.customer_code)), '') IS NULL
      OR LTRIM(RTRIM(c.scvcli_Cod)) = LTRIM(RTRIM(tgt.customer_code))
 );

IF OBJECT_ID('tempdb..#CurrentTotals') IS NOT NULL DROP TABLE #CurrentTotals;
SELECT
  stvscv_ID AS scv_ID,
  SUM(COALESCE(stv_ImpNetoEmi, 0)) AS current_net,
  SUM(COALESCE(stv_Imp1Emi, 0)) AS current_iva
INTO #CurrentTotals
FROM SegTotV
WHERE stvscv_ID IN (SELECT scv_ID FROM #MatchedDocs)
GROUP BY stvscv_ID;

IF OBJECT_ID('tempdb..#LineTargets') IS NOT NULL DROP TABLE #LineTargets;
SELECT
  m.company_key,
  m.group_id,
  m.remito_number,
  m.order_numbers,
  m.delivery_types,
  m.correction_mode,
  m.scv_ID,
  m.scvemp_Codigo,
  m.scvsuc_Cod,
  m.scv_FContab,
  m.scv_FDDJJ,
  m.scv_IncluDDJJ,
  m.scvcli_Cod,
  m.scvcli_RazSoc,
  d.sdv_ID,
  d.sdv_NReng,
  d.sdvart_CodGen,
  d.sdvart_CodEle1,
  d.sdvart_CodEle2,
  d.sdvart_CodEle3,
  d.sdv_Desc,
  d.sdvart_TipoTasaVta AS current_tipo_tasa,
  d.sdvtiv_Cod AS current_tiv_cod,
  d.sdvngr_Cod AS current_ngr_cod,
  ABS(COALESCE(NULLIF(d.sdv_ImpTot, 0), COALESCE(d.sdv_PrecioUn, 0) * ABS(COALESCE(d.sdv_CantUM1, 0)))) AS line_net,
  NULLIF(LTRIM(RTRIM(a.art_TipoTasaVta)), '') AS article_tipo_tasa,
  NULLIF(LTRIM(RTRIM(a.arttiv_CodVta)), '') AS article_tiv_cod,
  CASE
    WHEN m.correction_mode = '{REPAIR_CORRECTION_MODE}' THEN '1'
    ELSE COALESCE(NULLIF(LTRIM(RTRIM(a.art_TipoTasaVta)), ''), NULLIF(LTRIM(RTRIM(d.sdvart_TipoTasaVta)), ''), '1')
  END AS target_tipo_tasa,
  CASE
    WHEN m.correction_mode = '{REPAIR_CORRECTION_MODE}' THEN '01'
    ELSE COALESCE(NULLIF(LTRIM(RTRIM(a.arttiv_CodVta)), ''), '')
  END AS target_tiv_cod
INTO #LineTargets
FROM #MatchedDocs m
JOIN SegDetV d
  ON d.sdvscv_ID = m.scv_ID
 AND d.sdv_TipoIt = 'A'
LEFT JOIN Articulos a
  ON LTRIM(RTRIM(a.art_CodGen)) = LTRIM(RTRIM(d.sdvart_CodGen))
 AND LTRIM(RTRIM(COALESCE(a.art_CodEle1, ''))) = LTRIM(RTRIM(COALESCE(d.sdvart_CodEle1, '')))
 AND LTRIM(RTRIM(COALESCE(a.art_CodEle2, ''))) = LTRIM(RTRIM(COALESCE(d.sdvart_CodEle2, '')))
 AND LTRIM(RTRIM(COALESCE(a.art_CodEle3, ''))) = LTRIM(RTRIM(COALESCE(d.sdvart_CodEle3, '')));

ALTER TABLE #LineTargets ADD target_rate decimal(9,4) NULL;
UPDATE lt
   SET target_rate = rm.tasa
FROM #LineTargets lt
LEFT JOIN #NexoraIvaRateMap rm
  ON rm.tiv_cod = lt.target_tiv_cod;

IF OBJECT_ID('tempdb..#ReviewLines') IS NOT NULL DROP TABLE #ReviewLines;
SELECT
  lt.*,
  CASE
    WHEN lt.line_net <= 0 THEN 'SIN_BASE'
    WHEN lt.correction_mode = '{SALE_CORRECTION_MODE}' AND NULLIF(lt.article_tiv_cod, '') IS NULL THEN 'ARTICULO_SIN_IVA_VENTA'
    WHEN lt.target_rate IS NULL THEN 'CODIGO_IVA_NO_MAPEADO'
    ELSE ''
  END AS review_reason
INTO #ReviewLines
FROM #LineTargets lt
WHERE lt.line_net > 0
  AND (
    (lt.correction_mode = '{SALE_CORRECTION_MODE}' AND NULLIF(lt.article_tiv_cod, '') IS NULL)
    OR lt.target_rate IS NULL
  );

SELECT
  'REVISAR' AS estado,
  review_reason,
  remito_number,
  order_numbers,
  sdv_ID,
  sdv_NReng,
  sdvart_CodGen,
  sdvart_CodEle1,
  sdv_Desc,
  current_tiv_cod,
  article_tiv_cod,
  target_tiv_cod,
  line_net
FROM #ReviewLines
ORDER BY remito_number, sdv_NReng;

IF OBJECT_ID('tempdb..#CandidateDocs') IS NOT NULL DROP TABLE #CandidateDocs;
SELECT
  lt.scv_ID,
  MAX(lt.remito_number) AS remito_number,
  MAX(lt.order_numbers) AS order_numbers,
  MAX(lt.delivery_types) AS delivery_types,
  MAX(lt.correction_mode) AS correction_mode,
  SUM(CASE WHEN lt.target_rate > 0 THEN lt.line_net ELSE 0 END) AS taxable_net,
  SUM(CASE WHEN lt.target_rate > 0 THEN ROUND(lt.line_net * lt.target_rate / 100.0, 2) ELSE 0 END) AS expected_iva,
  COALESCE(MAX(ct.current_iva), 0) AS current_iva
INTO #CandidateDocs
FROM #LineTargets lt
LEFT JOIN #CurrentTotals ct ON ct.scv_ID = lt.scv_ID
WHERE lt.line_net > 0
GROUP BY lt.scv_ID
HAVING
  SUM(CASE WHEN lt.target_rate > 0 THEN lt.line_net ELSE 0 END) > 0
  AND ABS(COALESCE(MAX(ct.current_iva), 0)) < 0.01;

SELECT
  'CANDIDATO' AS estado,
  remito_number,
  order_numbers,
  delivery_types,
  correction_mode,
  taxable_net,
  current_iva,
  expected_iva
FROM #CandidateDocs
ORDER BY remito_number;

SELECT
  'LINEA' AS estado,
  lt.remito_number,
  lt.order_numbers,
  lt.sdv_ID,
  lt.sdv_NReng,
  lt.sdvart_CodGen,
  lt.sdvart_CodEle1,
  lt.sdv_Desc,
  lt.line_net,
  lt.current_tipo_tasa,
  lt.current_tiv_cod,
  lt.article_tipo_tasa,
  lt.article_tiv_cod,
  lt.target_tipo_tasa,
  lt.target_tiv_cod,
  lt.target_rate
FROM #LineTargets lt
JOIN #CandidateDocs cd ON cd.scv_ID = lt.scv_ID
ORDER BY lt.remito_number, lt.sdv_NReng;

IF @Apply = 1 AND EXISTS (SELECT 1 FROM #MatchCounts WHERE matches <> 1)
BEGIN
    THROW 51000, N'La corrección no se aplica porque algún remito no matchea exactamente una cabecera Bejerman.', 1;
END;

IF @Apply = 1 AND EXISTS (
    SELECT 1
    FROM #ReviewLines rl
    JOIN #CandidateDocs cd ON cd.scv_ID = rl.scv_ID
)
BEGIN
    THROW 51001, N'La corrección no se aplica porque hay líneas con IVA de artículo no determinado.', 1;
END;

IF OBJECT_ID('tempdb..#IvaGroups') IS NOT NULL DROP TABLE #IvaGroups;
SELECT
  lt.scv_ID,
  MAX(lt.scvemp_Codigo) AS stvemp_Codigo,
  MAX(lt.scvsuc_Cod) AS stvsuc_Cod,
  MAX(lt.scv_FContab) AS stv_FContab,
  MAX(lt.scv_FDDJJ) AS stv_FDDJJ,
  MAX(lt.scv_IncluDDJJ) AS stv_IncluDDJJ,
  lt.target_tiv_cod AS stvtiv_Cod,
  CAST('' AS varchar(20)) AS stvngr_Cod,
  MAX(lt.target_rate) AS stv_Tasa1,
  CAST(0 AS decimal(18,4)) AS stv_Tasa2,
  ROUND(SUM(lt.line_net), 2) AS stv_ImpNetoEmi,
  ROUND(SUM(lt.line_net * lt.target_rate / 100.0), 2) AS stv_Imp1Emi,
  CAST(0 AS decimal(18,4)) AS stv_Imp2Emi
INTO #IvaGroups
FROM #LineTargets lt
JOIN #CandidateDocs cd ON cd.scv_ID = lt.scv_ID
WHERE lt.line_net > 0
  AND NULLIF(lt.target_tiv_cod, '') IS NOT NULL
  AND lt.target_rate IS NOT NULL
GROUP BY lt.scv_ID, lt.target_tiv_cod;

SELECT
  'TOTAL_NUEVO' AS estado,
  cd.remito_number,
  ig.stvtiv_Cod,
  ig.stv_Tasa1,
  ig.stv_ImpNetoEmi,
  ig.stv_Imp1Emi
FROM #IvaGroups ig
JOIN #CandidateDocs cd ON cd.scv_ID = ig.scv_ID
ORDER BY cd.remito_number, ig.stvtiv_Cod;

IF @Apply = 1
BEGIN
    DECLARE @sql nvarchar(max);

    SET @sql = N'SELECT SYSDATETIME() AS backup_at, c.* INTO dbo.' + QUOTENAME(@BackupPrefix + '_SegCabV') + N'
                 FROM SegCabV c JOIN #CandidateDocs cd ON cd.scv_ID = c.scv_ID';
    IF OBJECT_ID(N'dbo.' + QUOTENAME(@BackupPrefix + '_SegCabV'), 'U') IS NULL EXEC sp_executesql @sql;

    SET @sql = N'SELECT SYSDATETIME() AS backup_at, d.* INTO dbo.' + QUOTENAME(@BackupPrefix + '_SegDetV') + N'
                 FROM SegDetV d JOIN #CandidateDocs cd ON cd.scv_ID = d.sdvscv_ID';
    IF OBJECT_ID(N'dbo.' + QUOTENAME(@BackupPrefix + '_SegDetV'), 'U') IS NULL EXEC sp_executesql @sql;

    SET @sql = N'SELECT SYSDATETIME() AS backup_at, tv.* INTO dbo.' + QUOTENAME(@BackupPrefix + '_SegTotV') + N'
                 FROM SegTotV tv JOIN #CandidateDocs cd ON cd.scv_ID = tv.stvscv_ID';
    IF OBJECT_ID(N'dbo.' + QUOTENAME(@BackupPrefix + '_SegTotV'), 'U') IS NULL EXEC sp_executesql @sql;

    SET @sql = N'SELECT SYSDATETIME() AS backup_at, * INTO dbo.' + QUOTENAME(@BackupPrefix + '_LineTargets') + N'
                 FROM #LineTargets WHERE scv_ID IN (SELECT scv_ID FROM #CandidateDocs)';
    IF OBJECT_ID(N'dbo.' + QUOTENAME(@BackupPrefix + '_LineTargets'), 'U') IS NULL EXEC sp_executesql @sql;

    BEGIN TRY
        BEGIN TRANSACTION;

        UPDATE d
           SET sdvart_TipoTasaVta = lt.target_tipo_tasa,
               sdvtiv_Cod = lt.target_tiv_cod,
               sdvngr_Cod = '',
               sdv_ImpNG = 0
        FROM SegDetV d
        JOIN #LineTargets lt ON lt.sdv_ID = d.sdv_ID
        JOIN #CandidateDocs cd ON cd.scv_ID = lt.scv_ID
        WHERE lt.line_net > 0
          AND NULLIF(lt.target_tiv_cod, '') IS NOT NULL
          AND lt.target_rate IS NOT NULL;

        DELETE tv
        FROM SegTotV tv
        JOIN #CandidateDocs cd ON cd.scv_ID = tv.stvscv_ID;

        INSERT INTO SegTotV (
          stvemp_Codigo, stvsuc_Cod, stvscv_ID, stv_FContab, stv_FDDJJ, stv_IncluDDJJ,
          stv_Marca2, stv_TipoPropDesc, stvtiv_Cod, stvngr_Cod, stvres_Cod, stvres_Art,
          stv_Tasa1, stv_Tasa2, stv_ImpNetoEmi, stv_Imp1Emi, stv_Imp2Emi,
          stv_AplicaDesc, stv_REModMan, stv_BUso, stv_Base, stv_PorcentajeRes
        )
        SELECT
          stvemp_Codigo, stvsuc_Cod, scv_ID, stv_FContab, stv_FDDJJ, stv_IncluDDJJ,
          '1', '1', stvtiv_Cod, stvngr_Cod, '', ' ',
          stv_Tasa1, stv_Tasa2, stv_ImpNetoEmi, stv_Imp1Emi, stv_Imp2Emi,
          'S', 0, 'N', 0, 0
        FROM #IvaGroups;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0
            ROLLBACK TRANSACTION;
        THROW;
    END CATCH;

    DECLARE @CorrectedCount int;
    SELECT @CorrectedCount = COUNT(*) FROM #CandidateDocs;
    PRINT CONCAT(N'Remitos corregidos: ', @CorrectedCount);
END
ELSE
BEGIN
    PRINT N'Dry-run: @Apply=0. No se modifica Bejerman.';
END;

SELECT
  'DESPUES' AS estado,
  cd.remito_number,
  tv.stvtiv_Cod,
  tv.stv_Tasa1,
  tv.stv_ImpNetoEmi,
  tv.stv_Imp1Emi
FROM #CandidateDocs cd
JOIN SegTotV tv ON tv.stvscv_ID = cd.scv_ID
ORDER BY cd.remito_number, tv.stvtiv_Cod;
""".strip()
    return "\n".join(header) + "\n" + body + "\n"


def write_reports(
    out_dir: str | Path,
    *,
    targets: list[RemitoIvaTarget],
    sql_script: str,
    metadata: dict[str, Any],
) -> Path:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    rows = [asdict(target) for target in targets]
    (out_path / "candidatos.json").write_text(_json_dumps(rows), encoding="utf-8")
    (out_path / "summary.json").write_text(
        _json_dumps({**metadata, "candidate_count": len(targets)}),
        encoding="utf-8",
    )
    (out_path / "bejerman_remitos_iva_fix.sql").write_text(sql_script, encoding="utf-8")
    csv_path = out_path / "candidatos.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(RemitoIvaTarget.__dataclass_fields__.keys()))
        writer.writeheader()
        writer.writerows(rows)
    return out_path
