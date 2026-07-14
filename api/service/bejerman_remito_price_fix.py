from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable, Iterable

from .bejerman_documents import parse_bejerman_remito_number


PRICE_FIX_TYPES = {"RT"}
QUANTITY_TOLERANCE = Decimal("0.0001")


class BejermanRemitoPriceFixError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RemitoPriceLineTarget:
    company_key: str
    group_id: str
    remito_number: str
    tipo: str
    letra: str
    punto: str
    numero: str
    customer_code: str
    customer_name: str
    order_number: str
    item_id: str
    article_code: str
    partida: str
    quantity: Decimal
    unit_price: Decimal
    discount_percent: Decimal
    line_net: Decimal


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def clean_upper(value: Any) -> str:
    return clean_text(value).upper()


def timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def sql_literal(value: Any) -> str:
    return "'" + clean_text(value).replace("'", "''") + "'"


def decimal_literal(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP), "f")


def money_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise BejermanRemitoPriceFixError("INVALID_PRICE_TARGET", "Precio o cantidad inválidos") from exc


def _safe_sql_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", clean_text(value))[:120] or f"NEXORA_BKP_REMITOS_PRECIO_{timestamp_slug()}"


def _parse_group_document(order: dict[str, Any]) -> dict[str, str]:
    group = order.get("bejermanRemitoGroup") if isinstance(order.get("bejermanRemitoGroup"), dict) else {}
    return parse_bejerman_remito_number(
        group.get("remitoNumber") or order.get("remitoNumber"),
        default_type=clean_upper(group.get("comprobanteTipo")) or "RT",
        default_letter=clean_upper(group.get("comprobanteLetra")) or "R",
        default_point=clean_text(group.get("comprobantePtoVenta")),
        allowed_types=PRICE_FIX_TYPES,
        allow_number_only=bool(clean_text(group.get("comprobantePtoVenta"))),
        require_complete=True,
    )


def _line_defs_for_item(order: dict[str, Any], item: dict[str, Any]) -> list[tuple[Decimal, str]]:
    partidas = item.get("partidas") if isinstance(item.get("partidas"), list) else []
    rows: list[tuple[Decimal, str]] = []
    for partida in partidas:
        if not isinstance(partida, dict):
            continue
        partida_text = clean_text(partida.get("partida"))
        quantity = money_decimal(partida.get("assignedQuantity") or partida.get("quantity") or item.get("quantity") or "1")
        if quantity <= 0:
            continue
        rows.append((quantity, partida_text))
    if rows:
        return rows

    quantity = money_decimal(item.get("quantity") or "1")
    partida = clean_text(item.get("partida"))
    if not partida and clean_text(order.get("deliveryType")) != "sale":
        partida = clean_text(item.get("equipmentSerial") or order.get("equipmentSerial"))
    return [(quantity, partida)]


def targets_from_delivery_order(order: dict[str, Any], items: list[dict[str, Any]]) -> list[RemitoPriceLineTarget]:
    parsed = _parse_group_document(order)
    group = order.get("bejermanRemitoGroup") if isinstance(order.get("bejermanRemitoGroup"), dict) else {}
    group_id = clean_text(group.get("id") or order.get("bejermanRemitoGroupId"))
    company_key = clean_upper(group.get("companyKey") or order.get("companyKey")) or "SEPID"
    customer_code = clean_text(group.get("customerCode") or order.get("bejermanCustomerCode"))
    customer_name = clean_text(group.get("customerName") or order.get("customerName"))
    order_number = clean_text(order.get("orderNumber"))

    out: list[RemitoPriceLineTarget] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = clean_text(item.get("id"))
        article_code = clean_text(item.get("articleCode"))
        if not item_id or not article_code:
            raise BejermanRemitoPriceFixError(
                "BEJERMAN_PRICE_FIX_ARTICLE_REQUIRED",
                "Cada renglón necesita un artículo Bejerman para corregir el precio.",
            )
        unit_price = money_decimal(item.get("unitPrice"))
        discount_percent = money_decimal(item.get("discountPercent") or "0")
        if unit_price < 0 or discount_percent < 0 or discount_percent > 100:
            raise BejermanRemitoPriceFixError("INVALID_PRICE_TARGET", "Precio o descuento inválidos")
        for quantity, partida in _line_defs_for_item(order, item):
            line_net = (quantity * unit_price * (Decimal("100") - discount_percent) / Decimal("100")).quantize(
                Decimal("0.0001"),
                rounding=ROUND_HALF_UP,
            )
            out.append(
                RemitoPriceLineTarget(
                    company_key=company_key,
                    group_id=group_id,
                    remito_number=parsed["remitoNumber"],
                    tipo=parsed["type"],
                    letra=parsed["letter"],
                    punto=parsed["point"],
                    numero=parsed["number"],
                    customer_code=customer_code,
                    customer_name=customer_name,
                    order_number=order_number,
                    item_id=item_id,
                    article_code=article_code,
                    partida=partida,
                    quantity=quantity,
                    unit_price=unit_price,
                    discount_percent=discount_percent,
                    line_net=line_net,
                )
            )
    if not out:
        raise BejermanRemitoPriceFixError("BEJERMAN_PRICE_FIX_TARGETS_EMPTY", "No hay renglones para corregir en Bejerman.")
    return out


def _values_sql(targets: Iterable[RemitoPriceLineTarget]) -> str:
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
                    sql_literal(target.order_number),
                    sql_literal(target.item_id),
                    sql_literal(target.article_code),
                    sql_literal(target.partida),
                    decimal_literal(target.quantity),
                    decimal_literal(target.unit_price),
                    decimal_literal(target.discount_percent),
                    decimal_literal(target.line_net),
                ]
            )
            + ")"
        )
    return ",\n".join(rows)


def build_bejerman_price_fix_sql(
    targets: Iterable[RemitoPriceLineTarget],
    *,
    database: str,
    apply: bool = False,
    backup_name: str = "",
) -> str:
    target_list = list(targets)
    database_name = clean_text(database) or "SBDPSEP"
    safe_database = database_name.replace("]", "]]")
    backup = _safe_sql_name(backup_name or f"NEXORA_BKP_REMITOS_PRECIO_{timestamp_slug()}")
    apply_flag = "1" if apply else "0"

    header = [
        "SET NOCOUNT ON;",
        f"USE [{safe_database}];",
        "",
        f"DECLARE @Apply bit = {apply_flag};",
        f"DECLARE @BackupPrefix sysname = N'{backup}';",
        "",
        "IF OBJECT_ID('tempdb..#NexoraRemitoPriceTargets') IS NOT NULL DROP TABLE #NexoraRemitoPriceTargets;",
        "CREATE TABLE #NexoraRemitoPriceTargets (",
        "  target_row_id int IDENTITY(1,1) NOT NULL PRIMARY KEY,",
        "  company_key nvarchar(20) NOT NULL,",
        "  group_id nvarchar(80) NOT NULL,",
        "  remito_number nvarchar(40) NOT NULL,",
        "  tipo varchar(8) NOT NULL,",
        "  letra varchar(2) NOT NULL,",
        "  punto varchar(5) NOT NULL,",
        "  numero varchar(8) NOT NULL,",
        "  customer_code nvarchar(30) NOT NULL,",
        "  customer_name nvarchar(200) NOT NULL,",
        "  order_number nvarchar(80) NOT NULL,",
        "  item_id nvarchar(80) NOT NULL,",
        "  article_code nvarchar(80) NOT NULL,",
        "  partida nvarchar(120) NOT NULL,",
        "  quantity decimal(18,4) NOT NULL,",
        "  unit_price decimal(18,4) NOT NULL,",
        "  discount_percent decimal(9,4) NOT NULL,",
        "  line_net decimal(18,4) NOT NULL",
        ");",
        "",
    ]
    if target_list:
        header.extend(
            [
                "INSERT INTO #NexoraRemitoPriceTargets (",
                "  company_key, group_id, remito_number, tipo, letra, punto, numero, customer_code, customer_name,",
                "  order_number, item_id, article_code, partida, quantity, unit_price, discount_percent, line_net",
                ")",
                "VALUES",
                _values_sql(target_list) + ";",
                "",
            ]
        )

    body = f"""
IF NOT EXISTS (SELECT 1 FROM #NexoraRemitoPriceTargets)
BEGIN
    THROW 51100, N'La corrección no se aplica porque no hay renglones objetivo.', 1;
END;

IF OBJECT_ID('tempdb..#MatchedDocs') IS NOT NULL DROP TABLE #MatchedDocs;
SELECT
  tgt.group_id,
  MAX(tgt.remito_number) AS remito_number,
  COUNT(DISTINCT c.scv_ID) AS matches,
  MAX(c.scv_ID) AS scv_ID,
  MAX(c.scvemp_Codigo) AS scvemp_Codigo,
  MAX(c.scvsuc_Cod) AS scvsuc_Cod,
  MAX(c.scv_FContab) AS scv_FContab,
  MAX(c.scv_FDDJJ) AS scv_FDDJJ,
  MAX(c.scv_IncluDDJJ) AS scv_IncluDDJJ
INTO #MatchedDocs
FROM #NexoraRemitoPriceTargets tgt
LEFT JOIN SegTiposV t
  ON t.spvtco_Cod = tgt.tipo
 AND t.spv_Letra = tgt.letra
 AND t.spv_CodPvt = tgt.punto
 AND t.spv_Nro = tgt.numero
LEFT JOIN SegCabV c
  ON c.scv_ID = t.spvscv_ID
 AND LTRIM(RTRIM(c.scvcli_Cod)) = LTRIM(RTRIM(tgt.customer_code))
GROUP BY tgt.group_id;

IF EXISTS (SELECT 1 FROM #MatchedDocs WHERE matches <> 1)
BEGIN
    THROW 51101, N'La corrección no se aplica porque el remito no matchea exactamente una cabecera Bejerman.', 1;
END;

IF OBJECT_ID('tempdb..#LineMatches') IS NOT NULL DROP TABLE #LineMatches;
SELECT
  tgt.*,
  md.scv_ID,
  md.scvemp_Codigo,
  md.scvsuc_Cod,
  md.scv_FContab,
  md.scv_FDDJJ,
  md.scv_IncluDDJJ,
  d.sdv_ID,
  d.sdv_NReng,
  d.sdvtiv_Cod,
  COALESCE(tv.stv_Tasa1,
    CASE NULLIF(LTRIM(RTRIM(COALESCE(d.sdvtiv_Cod, ''))), '')
      WHEN '01' THEN 21
      WHEN '03' THEN 10.5
      WHEN '04' THEN 0
      ELSE 0
    END
  ) AS vat_rate,
  COALESCE(d.sdv_PrecioUn, 0) AS current_unit_price,
  ABS(COALESCE(NULLIF(d.sdv_ImpTot, 0), COALESCE(d.sdv_PrecioUn, 0) * ABS(COALESCE(d.sdv_CantUM1, 0)))) AS current_line_net,
  COUNT(d.sdv_ID) OVER (PARTITION BY tgt.target_row_id) AS line_matches
INTO #LineMatches
FROM #NexoraRemitoPriceTargets tgt
JOIN #MatchedDocs md ON md.group_id = tgt.group_id
LEFT JOIN SegDetV d
  ON d.sdvscv_ID = md.scv_ID
 AND d.sdv_TipoIt = 'A'
 AND LTRIM(RTRIM(COALESCE(d.sdvart_CodGen, '') + COALESCE(d.sdvart_CodEle1, '') + COALESCE(d.sdvart_CodEle2, '') + COALESCE(d.sdvart_CodEle3, ''))) = LTRIM(RTRIM(tgt.article_code))
 AND ABS(ABS(COALESCE(d.sdv_CantUM1, 0)) - tgt.quantity) <= {decimal_literal(QUANTITY_TOLERANCE)}
 AND (
      (NULLIF(LTRIM(RTRIM(tgt.partida)), '') IS NULL AND NULLIF(LTRIM(RTRIM(COALESCE(d.sdvstp_Partida, ''))), '') IS NULL)
      OR LTRIM(RTRIM(COALESCE(d.sdvstp_Partida, ''))) = LTRIM(RTRIM(tgt.partida))
 )
LEFT JOIN SegTotV tv
  ON tv.stvscv_ID = md.scv_ID
 AND LTRIM(RTRIM(COALESCE(tv.stvtiv_Cod, ''))) = LTRIM(RTRIM(COALESCE(d.sdvtiv_Cod, '')));

IF EXISTS (SELECT 1 FROM #LineMatches WHERE line_matches <> 1 OR sdv_ID IS NULL)
BEGIN
    THROW 51102, N'La corrección no se aplica porque uno o más renglones no tienen mapeo único en SegDetV.', 1;
END;

IF EXISTS (
    SELECT 1
    FROM #LineMatches
    GROUP BY sdv_ID
    HAVING COUNT(*) <> 1
)
BEGIN
    THROW 51103, N'La corrección requiere revisión manual porque más de un renglón NEXORA apunta al mismo renglón Bejerman.', 1;
END;

IF OBJECT_ID('tempdb..#TotalGroups') IS NOT NULL DROP TABLE #TotalGroups;
SELECT
  md.scv_ID,
  MAX(md.scvemp_Codigo) AS stvemp_Codigo,
  MAX(md.scvsuc_Cod) AS stvsuc_Cod,
  MAX(md.scv_FContab) AS stv_FContab,
  MAX(md.scv_FDDJJ) AS stv_FDDJJ,
  MAX(md.scv_IncluDDJJ) AS stv_IncluDDJJ,
  COALESCE(NULLIF(LTRIM(RTRIM(d.sdvtiv_Cod)), ''), '04') AS stvtiv_Cod,
  CAST('' AS varchar(20)) AS stvngr_Cod,
  MAX(COALESCE(tv.stv_Tasa1,
    CASE NULLIF(LTRIM(RTRIM(COALESCE(d.sdvtiv_Cod, ''))), '')
      WHEN '01' THEN 21
      WHEN '03' THEN 10.5
      WHEN '04' THEN 0
      ELSE 0
    END
  )) AS stv_Tasa1,
  CAST(0 AS decimal(18,4)) AS stv_Tasa2,
  ROUND(SUM(CASE WHEN lm.sdv_ID IS NOT NULL THEN lm.line_net ELSE ABS(COALESCE(NULLIF(d.sdv_ImpTot, 0), COALESCE(d.sdv_PrecioUn, 0) * ABS(COALESCE(d.sdv_CantUM1, 0)))) END), 2) AS stv_ImpNetoEmi,
  ROUND(SUM((CASE WHEN lm.sdv_ID IS NOT NULL THEN lm.line_net ELSE ABS(COALESCE(NULLIF(d.sdv_ImpTot, 0), COALESCE(d.sdv_PrecioUn, 0) * ABS(COALESCE(d.sdv_CantUM1, 0)))) END) *
    COALESCE(tv.stv_Tasa1,
      CASE NULLIF(LTRIM(RTRIM(COALESCE(d.sdvtiv_Cod, ''))), '')
        WHEN '01' THEN 21
        WHEN '03' THEN 10.5
        WHEN '04' THEN 0
        ELSE 0
      END
    ) / 100.0), 2) AS stv_Imp1Emi,
  CAST(0 AS decimal(18,4)) AS stv_Imp2Emi
INTO #TotalGroups
FROM #MatchedDocs md
JOIN SegDetV d ON d.sdvscv_ID = md.scv_ID AND d.sdv_TipoIt = 'A'
LEFT JOIN #LineMatches lm ON lm.sdv_ID = d.sdv_ID
LEFT JOIN SegTotV tv
  ON tv.stvscv_ID = md.scv_ID
 AND LTRIM(RTRIM(COALESCE(tv.stvtiv_Cod, ''))) = LTRIM(RTRIM(COALESCE(d.sdvtiv_Cod, '')))
GROUP BY md.scv_ID, COALESCE(NULLIF(LTRIM(RTRIM(d.sdvtiv_Cod)), ''), '04');

IF @Apply = 1
BEGIN
    DECLARE @sql nvarchar(max);

    SET @sql = N'SELECT SYSDATETIME() AS backup_at, c.* INTO dbo.' + QUOTENAME(@BackupPrefix + '_SegCabV') + N'
                 FROM SegCabV c JOIN #MatchedDocs md ON md.scv_ID = c.scv_ID';
    IF OBJECT_ID(N'dbo.' + QUOTENAME(@BackupPrefix + '_SegCabV'), 'U') IS NULL EXEC sp_executesql @sql;

    SET @sql = N'SELECT SYSDATETIME() AS backup_at, d.* INTO dbo.' + QUOTENAME(@BackupPrefix + '_SegDetV') + N'
                 FROM SegDetV d JOIN #MatchedDocs md ON md.scv_ID = d.sdvscv_ID';
    IF OBJECT_ID(N'dbo.' + QUOTENAME(@BackupPrefix + '_SegDetV'), 'U') IS NULL EXEC sp_executesql @sql;

    SET @sql = N'SELECT SYSDATETIME() AS backup_at, tv.* INTO dbo.' + QUOTENAME(@BackupPrefix + '_SegTotV') + N'
                 FROM SegTotV tv JOIN #MatchedDocs md ON md.scv_ID = tv.stvscv_ID';
    IF OBJECT_ID(N'dbo.' + QUOTENAME(@BackupPrefix + '_SegTotV'), 'U') IS NULL EXEC sp_executesql @sql;

    SET @sql = N'SELECT SYSDATETIME() AS backup_at, * INTO dbo.' + QUOTENAME(@BackupPrefix + '_LineMatches') + N'
                 FROM #LineMatches';
    IF OBJECT_ID(N'dbo.' + QUOTENAME(@BackupPrefix + '_LineMatches'), 'U') IS NULL EXEC sp_executesql @sql;

    BEGIN TRY
        BEGIN TRANSACTION;

        UPDATE d
           SET sdv_PrecioUn = lm.unit_price,
               sdv_ImpTot = lm.line_net
        FROM SegDetV d
        JOIN #LineMatches lm ON lm.sdv_ID = d.sdv_ID;

        DELETE tv
        FROM SegTotV tv
        JOIN #MatchedDocs md ON md.scv_ID = tv.stvscv_ID;

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
        FROM #TotalGroups;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0
            ROLLBACK TRANSACTION;
        THROW;
    END CATCH;
END;

SELECT
  CASE WHEN @Apply = 1 THEN 'APLICADO' ELSE 'DRY_RUN' END AS estado,
  @BackupPrefix AS backup_name,
  group_id,
  remito_number,
  order_number,
  item_id,
  article_code,
  partida,
  quantity,
  current_unit_price,
  unit_price AS target_unit_price,
  discount_percent,
  current_line_net,
  line_net AS target_line_net,
  sdv_ID,
  sdv_NReng
FROM #LineMatches
ORDER BY remito_number, sdv_NReng, item_id;
""".strip()
    return "\n".join(header) + body


def perform_bejerman_remito_price_fix(
    targets: Iterable[RemitoPriceLineTarget],
    *,
    company_key: Any,
    apply: bool = True,
    database: str = "",
    backup_name: str = "",
    executor: Callable[..., list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    target_list = list(targets)
    if not target_list:
        raise BejermanRemitoPriceFixError("BEJERMAN_PRICE_FIX_TARGETS_EMPTY", "No hay renglones para corregir en Bejerman.")

    if not database:
        from .bejerman_delivery import _bejerman_sql_database_for_company

        database = _bejerman_sql_database_for_company(company_key)
    effective_backup_name = _safe_sql_name(backup_name or f"NEXORA_BKP_REMITOS_PRECIO_{timestamp_slug()}")
    sql = build_bejerman_price_fix_sql(
        target_list,
        database=database,
        apply=apply,
        backup_name=effective_backup_name,
    )

    if executor is None:
        from .bejerman_delivery import _remote_bejerman_sql_csv

        executor = _remote_bejerman_sql_csv

    rows = executor(sql, company_key=company_key)
    if not rows:
        raise BejermanRemitoPriceFixError(
            "BEJERMAN_PRICE_FIX_NOT_APPLIED",
            "No se pudo confirmar la corrección de precio en Bejerman.",
        )
    return {
        "backupName": effective_backup_name,
        "lineCount": len(target_list),
        "rows": rows,
        "sql": sql,
        "targets": [asdict(target) for target in target_list],
    }
