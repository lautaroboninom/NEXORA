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


BILLABLE_DELIVERY_TYPES = {"sale"}
BILLABLE_REMITO_TYPES = {"RT", "RD"}
DEFAULT_TEST_DATABASES = {
    "SEPID": "SBDPSEP",
    "MGBIO": "SBDPMGBI",
}


@dataclass(frozen=True)
class RemitoCurrencyTarget:
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
    generated_at: str
    commercial_exchange_rate: str


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def clean_upper(value: Any) -> str:
    return clean_text(value).upper()


def timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def default_out_dir() -> str:
    return str(Path("informes") / f"bejerman_remitos_usd_moneda_{timestamp_slug()}")


def default_test_database(company_key: Any) -> str:
    return DEFAULT_TEST_DATABASES.get(clean_upper(company_key), "SBDPSEP")


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
        allowed_types=BILLABLE_REMITO_TYPES,
        default_point=default_point,
        allow_number_only=bool(default_point),
        require_complete=True,
    )


def target_from_row(row: dict[str, Any]) -> RemitoCurrencyTarget:
    parsed = _parse_remito(row)
    return RemitoCurrencyTarget(
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
        generated_at=clean_text(row.get("generated_at")),
        commercial_exchange_rate=clean_text(row.get("commercial_exchange_rate")),
    )


def collect_local_usd_remito_targets(
    *,
    company_key: str = "",
    from_date: str = "",
    to_date: str = "",
    limit: int = 0,
    include_all_delivery_types: bool = False,
) -> list[RemitoCurrencyTarget]:
    filters = [
        "UPPER(COALESCE(o.price_currency, 'ARS')) = 'USD'",
        "NULLIF(BTRIM(COALESCE(o.invoice_number, '')), '') IS NULL",
        "NULLIF(BTRIM(COALESCE(g.remito_number, o.remito_number, '')), '') IS NOT NULL",
        "COALESCE(g.status, '') = 'generated'",
        "o.status NOT IN ('facturado', 'cancelado')",
    ]
    params: list[Any] = []
    if not include_all_delivery_types:
        filters.append("o.delivery_type = ANY(%s)")
        params.append(sorted(BILLABLE_DELIVERY_TYPES))
    if clean_text(company_key):
        filters.append("UPPER(COALESCE(g.company_key, o.company_key, '')) = %s")
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
          g.comprobante_numero,
          COALESCE(g.remito_number, o.remito_number) AS remito_number,
          g.customer_code,
          g.customer_name,
          STRING_AGG(DISTINCT o.order_number, ', ' ORDER BY o.order_number) AS order_numbers,
          MAX(COALESCE(g.generated_at, o.updated_at, o.created_at)) AS generated_at,
          MAX(NULLIF(BTRIM(COALESCE(o.commercial_exchange_rate, '')), '')) AS commercial_exchange_rate
        FROM delivery_orders o
        JOIN bejerman_remito_groups g ON g.id = o.bejerman_remito_group_id
        WHERE {" AND ".join(filters)}
        GROUP BY
          g.id, COALESCE(g.company_key, o.company_key, 'SEPID'), g.comprobante_tipo,
          COALESCE(g.comprobante_letra, 'R'), g.comprobante_pto_venta,
          g.comprobante_numero, COALESCE(g.remito_number, o.remito_number),
          g.customer_code, g.customer_name
        ORDER BY MAX(COALESCE(g.generated_at, o.updated_at, o.created_at)) DESC
        {limit_sql}
    """
    with connection.cursor() as cur:
        cur.execute(sql, params)
        columns = [col[0] for col in cur.description]
        rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    targets: list[RemitoCurrencyTarget] = []
    for row in rows:
        target = target_from_row(row)
        if target.tipo in BILLABLE_REMITO_TYPES:
            targets.append(target)
    return targets


def build_bejerman_currency_fix_sql(
    targets: Iterable[RemitoCurrencyTarget],
    *,
    database: str,
    apply: bool = False,
    backup_name: str = "",
) -> str:
    target_list = list(targets)
    database_name = clean_text(database) or "SBDPSEP"
    safe_database = database_name.replace("]", "]]")
    backup = clean_text(backup_name) or f"NEXORA_BKP_REMITOS_USD_MONEDA_{timestamp_slug()}"
    backup = re.sub(r"[^A-Za-z0-9_]", "_", backup)[:120]
    apply_flag = "1" if apply else "0"

    header = [
        "SET NOCOUNT ON;",
        f"USE [{safe_database}];",
        "",
        f"DECLARE @Apply bit = {apply_flag};",
        "DECLARE @LocalCurrencyCode varchar(10) = '1';",
        "DECLARE @LocalExchangeType varchar(20);",
        "",
        "IF OBJECT_ID('tempdb..#NexoraRemitoCurrencyTargets') IS NOT NULL",
        "    DROP TABLE #NexoraRemitoCurrencyTargets;",
        "",
        "CREATE TABLE #NexoraRemitoCurrencyTargets (",
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
        "    generated_at nvarchar(40) NULL,",
        "    commercial_exchange_rate nvarchar(40) NULL",
        ");",
        "",
    ]
    if target_list:
        values = []
        for target in target_list:
            values.append(
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
                        sql_literal(target.generated_at),
                        sql_literal(target.commercial_exchange_rate),
                    ]
                )
                + ")"
            )
        header.extend(
            [
                "INSERT INTO #NexoraRemitoCurrencyTargets (",
                "    company_key, group_id, remito_number, tipo, letra, punto, numero,",
                "    customer_code, customer_name, order_numbers, generated_at, commercial_exchange_rate",
                ")",
                "VALUES",
                ",\n".join(values) + ";",
                "",
            ]
        )

    body = f"""
SELECT TOP 1 @LocalExchangeType = NULLIF(LTRIM(RTRIM(c.scvmtca_codigo)), '')
  FROM SegCabV c
 WHERE c.scvmon_codigo = @LocalCurrencyCode
   AND NULLIF(LTRIM(RTRIM(c.scvmtca_codigo)), '') IS NOT NULL
 ORDER BY c.scv_FEmision DESC, c.scv_ID DESC;

IF @LocalExchangeType IS NULL
BEGIN
    SELECT TOP 1 @LocalExchangeType = NULLIF(LTRIM(RTRIM(mtca_codigo)), '')
      FROM mon_tca
     WHERE mon_codigo = @LocalCurrencyCode
     ORDER BY mtca_codigo;
END;

IF @LocalExchangeType IS NULL
    SET @LocalExchangeType = '';

;WITH match_counts AS (
    SELECT
      tgt.group_id,
      tgt.remito_number,
      COUNT(c.scv_ID) AS matches
    FROM #NexoraRemitoCurrencyTargets tgt
    LEFT JOIN SegTiposV t
      ON t.spvtco_Cod = tgt.tipo
     AND t.spv_Letra = tgt.letra
     AND t.spv_CodPvt = tgt.punto
     AND t.spv_Nro = tgt.numero
    LEFT JOIN SegCabV c
      ON c.scv_ID = t.spvscv_ID
     AND LTRIM(RTRIM(c.scvcli_Cod)) = LTRIM(RTRIM(tgt.customer_code))
    GROUP BY tgt.group_id, tgt.remito_number
)
SELECT *
  FROM match_counts
 WHERE matches <> 1;

IF @Apply = 1 AND EXISTS (
    SELECT 1
      FROM (
        SELECT tgt.group_id, COUNT(c.scv_ID) AS matches
          FROM #NexoraRemitoCurrencyTargets tgt
          LEFT JOIN SegTiposV t
            ON t.spvtco_Cod = tgt.tipo
           AND t.spv_Letra = tgt.letra
           AND t.spv_CodPvt = tgt.punto
           AND t.spv_Nro = tgt.numero
          LEFT JOIN SegCabV c
            ON c.scv_ID = t.spvscv_ID
           AND LTRIM(RTRIM(c.scvcli_Cod)) = LTRIM(RTRIM(tgt.customer_code))
         GROUP BY tgt.group_id
      ) q
     WHERE q.matches <> 1
)
BEGIN
    THROW 51000, 'La correccion no se aplica porque algun remito no matchea exactamente una cabecera Bejerman.', 1;
END;

IF OBJECT_ID('dbo.{backup}', 'U') IS NULL
BEGIN
    SELECT
      SYSDATETIME() AS backup_at,
      tgt.company_key,
      tgt.group_id,
      tgt.remito_number,
      tgt.order_numbers,
      tgt.commercial_exchange_rate,
      c.scvemp_Codigo,
      c.scvsuc_Cod,
      c.scv_ID,
      c.scv_FEmision,
      c.scvcli_Cod,
      c.scvmon_codigo,
      c.scvmtca_codigo,
      c.scvmcot_cotiza
    INTO dbo.{backup}
    FROM #NexoraRemitoCurrencyTargets tgt
    JOIN SegTiposV t
      ON t.spvtco_Cod = tgt.tipo
     AND t.spv_Letra = tgt.letra
     AND t.spv_CodPvt = tgt.punto
     AND t.spv_Nro = tgt.numero
    JOIN SegCabV c
      ON c.scv_ID = t.spvscv_ID
     AND LTRIM(RTRIM(c.scvcli_Cod)) = LTRIM(RTRIM(tgt.customer_code));
END;

SELECT
  'ANTES' AS estado,
  tgt.group_id,
  tgt.remito_number,
  tgt.order_numbers,
  c.scv_ID,
  c.scvcli_Cod,
  c.scvmon_codigo,
  c.scvmtca_codigo,
  c.scvmcot_cotiza,
  @LocalCurrencyCode AS moneda_destino,
  @LocalExchangeType AS tipo_cambio_destino
FROM #NexoraRemitoCurrencyTargets tgt
JOIN SegTiposV t
  ON t.spvtco_Cod = tgt.tipo
 AND t.spv_Letra = tgt.letra
 AND t.spv_CodPvt = tgt.punto
 AND t.spv_Nro = tgt.numero
JOIN SegCabV c
  ON c.scv_ID = t.spvscv_ID
 AND LTRIM(RTRIM(c.scvcli_Cod)) = LTRIM(RTRIM(tgt.customer_code))
ORDER BY tgt.remito_number;

IF @Apply = 1
BEGIN
    UPDATE c
       SET scvmon_codigo = @LocalCurrencyCode,
           scvmtca_codigo = @LocalExchangeType,
           scvmcot_cotiza = 1
    FROM #NexoraRemitoCurrencyTargets tgt
    JOIN SegTiposV t
      ON t.spvtco_Cod = tgt.tipo
     AND t.spv_Letra = tgt.letra
     AND t.spv_CodPvt = tgt.punto
     AND t.spv_Nro = tgt.numero
    JOIN SegCabV c
      ON c.scv_ID = t.spvscv_ID
     AND LTRIM(RTRIM(c.scvcli_Cod)) = LTRIM(RTRIM(tgt.customer_code))
    WHERE
      NULLIF(LTRIM(RTRIM(COALESCE(c.scvmon_codigo, ''))), '') <> @LocalCurrencyCode
      OR COALESCE(NULLIF(LTRIM(RTRIM(COALESCE(c.scvmtca_codigo, ''))), ''), '') <>
         COALESCE(NULLIF(LTRIM(RTRIM(@LocalExchangeType)), ''), '')
      OR COALESCE(c.scvmcot_cotiza, 0) <> 1;

    PRINT CONCAT('Remitos corregidos: ', @@ROWCOUNT);
END
ELSE
BEGIN
    PRINT 'Dry-run: cambie @Apply a 1 solo despues de revisar el SELECT ANTES y la tabla de backup.';
END;

SELECT
  'DESPUES' AS estado,
  tgt.group_id,
  tgt.remito_number,
  tgt.order_numbers,
  c.scv_ID,
  c.scvcli_Cod,
  c.scvmon_codigo,
  c.scvmtca_codigo,
  c.scvmcot_cotiza
FROM #NexoraRemitoCurrencyTargets tgt
JOIN SegTiposV t
  ON t.spvtco_Cod = tgt.tipo
 AND t.spv_Letra = tgt.letra
 AND t.spv_CodPvt = tgt.punto
 AND t.spv_Nro = tgt.numero
JOIN SegCabV c
  ON c.scv_ID = t.spvscv_ID
 AND LTRIM(RTRIM(c.scvcli_Cod)) = LTRIM(RTRIM(tgt.customer_code))
ORDER BY tgt.remito_number;
""".strip()
    if not target_list:
        header.append("-- No hay remitos candidatos en la entrada generada.")
    return "\n".join(header) + "\n" + body + "\n"


def write_reports(
    out_dir: str | Path,
    *,
    targets: list[RemitoCurrencyTarget],
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
    (out_path / "bejerman_remitos_usd_moneda_fix.sql").write_text(sql_script, encoding="utf-8")
    csv_path = out_path / "candidatos.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(RemitoCurrencyTarget.__dataclass_fields__.keys()))
        writer.writeheader()
        writer.writerows(rows)
    return out_path
