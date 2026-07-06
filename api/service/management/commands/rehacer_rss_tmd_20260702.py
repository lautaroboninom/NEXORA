from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from service.bejerman_delivery import (
    _bejerman_sql_database_for_company,
    _remote_bejerman_sql_csv,
    generate_bejerman_remito,
)
from service.bejerman_documents import parse_bejerman_remito_number
from service.delivery_orders import ensure_service_release_order_for_ingreso, service_release_max_items_per_order


DEFAULT_DATE = "2026-07-02"
DEFAULT_CUSTOMER_CODE = "TMD"
DEFAULT_COMPANY_KEY = "SEPID"
DEFAULT_PENDING_OS = [27935, 29103]
DEFAULT_OLD_REMITOS = [
    "RSS R 00004-00004747",
    "RSS R 00004-00004749",
    "RSS R 00004-00004750",
    "RSS R 00004-00004751",
    "RSS R 00004-00004752",
    "RSS R 00004-00004753",
]
DEFAULT_BACKUP_PREFIX = "NEXORA_BKP_20260703_TMD_RSS"
EXPECTED_OLD_OS_COUNT = 46
EXPECTED_TARGET_OS_COUNT = 48


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _upper(value: Any) -> str:
    return _clean(value).upper()


def _rows(cur) -> list[dict[str, Any]]:
    columns = [column[0] for column in cur.description or []]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(_json_dump(value) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _parse_int_list(value: Any) -> list[int]:
    text = _clean(value)
    if not text:
        return []
    out: list[int] = []
    for raw in re.split(r"[,;\s]+", text):
        if not raw:
            continue
        try:
            parsed = int(raw)
        except (TypeError, ValueError) as exc:
            raise CommandError(f"OS inválida en lista: {raw}") from exc
        if parsed > 0 and parsed not in out:
            out.append(parsed)
    return out


def _timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _sql_identifier(value: Any) -> str:
    text = _clean(value)
    if not text:
        raise CommandError("Identificador SQL vacío")
    return "[" + text.replace("]", "]]") + "]"


def _sql_string(value: Any) -> str:
    return "N'" + _clean(value).replace("'", "''") + "'"


def _parse_remito(remito_number: str) -> dict[str, str]:
    return parse_bejerman_remito_number(
        remito_number,
        default_type="RSS",
        default_letter="R",
        default_point="00004",
        allowed_types={"RSS"},
        allow_number_only=True,
        require_complete=True,
    )


def _target_rows_sql_values(targets: list[dict[str, Any]]) -> str:
    if not targets:
        return ""
    values = []
    for target in targets:
        values.append(
            "("
            + ", ".join(
                [
                    _sql_string(target["group_id"]),
                    _sql_string(target["remito_number"]),
                    _sql_string(target["type"]),
                    _sql_string(target["letter"]),
                    _sql_string(target["point"]),
                    _sql_string(target["number"]),
                    _sql_string(target["customer_code"]),
                    str(int(target.get("expected_items") or 0)),
                    _sql_string(",".join(str(item) for item in target.get("ingreso_ids") or [])),
                ]
            )
            + ")"
        )
    return ",\n".join(values)


def _build_target_temp_table_sql(targets: list[dict[str, Any]]) -> str:
    values = _target_rows_sql_values(targets)
    insert_sql = ""
    if values:
        insert_sql = (
            "INSERT INTO #NexoraTmdRssTargets "
            "(group_id, remito_number, tipo, letra, punto, numero, customer_code, expected_items, os_ids) VALUES\n"
            + values
            + ";"
        )
    return f"""
IF OBJECT_ID('tempdb..#NexoraTmdRssTargets') IS NOT NULL DROP TABLE #NexoraTmdRssTargets;
CREATE TABLE #NexoraTmdRssTargets (
  group_id NVARCHAR(80) NOT NULL,
  remito_number NVARCHAR(80) NOT NULL,
  tipo NVARCHAR(10) NOT NULL,
  letra NVARCHAR(4) NOT NULL,
  punto NVARCHAR(10) NOT NULL,
  numero NVARCHAR(20) NOT NULL,
  customer_code NVARCHAR(40) NOT NULL,
  expected_items INT NOT NULL,
  os_ids NVARCHAR(MAX) NOT NULL
);
{insert_sql}
""".strip()


def build_bejerman_validation_sql(targets: list[dict[str, Any]], *, database: str) -> str:
    return f"""
SET NOCOUNT ON;
USE {_sql_identifier(database)};
{_build_target_temp_table_sql(targets)}

SELECT
  tgt.group_id,
  tgt.remito_number,
  tgt.customer_code,
  tgt.expected_items,
  COUNT(DISTINCT c.scv_ID) AS matches,
  COALESCE(CONVERT(varchar(20), MIN(c.scv_ID)), '') AS scv_id,
  COUNT(CASE WHEN d.sdv_TipoIt = 'A' THEN 1 END) AS article_items,
  COUNT(CASE WHEN d.sdv_TipoIt = 'L' THEN 1 END) AS legend_items,
  COUNT(d.sdv_ID) AS total_lines,
  MAX(CONVERT(varchar(10), c.scv_FEmision, 23)) AS issue_date,
  MAX(LTRIM(RTRIM(COALESCE(c.scvcli_Cod, '')))) AS matched_customer_code
FROM #NexoraTmdRssTargets tgt
LEFT JOIN dbo.SegTiposV t
  ON LTRIM(RTRIM(t.spvtco_Cod)) = tgt.tipo
 AND LTRIM(RTRIM(t.spv_Letra)) = tgt.letra
 AND LTRIM(RTRIM(t.spv_CodPvt)) = tgt.punto
 AND LTRIM(RTRIM(t.spv_Nro)) = tgt.numero
LEFT JOIN dbo.SegCabV c
  ON c.scv_ID = t.spvscv_ID
 AND LTRIM(RTRIM(COALESCE(c.scvcli_Cod, ''))) = tgt.customer_code
LEFT JOIN dbo.SegDetV d
  ON d.sdvscv_ID = c.scv_ID
GROUP BY tgt.group_id, tgt.remito_number, tgt.customer_code, tgt.expected_items
ORDER BY tgt.remito_number;
""".strip()


def build_bejerman_delete_sql(
    targets: list[dict[str, Any]],
    *,
    database: str,
    backup_prefix: str,
    apply: bool,
) -> str:
    apply_value = "1" if apply else "0"
    validation_select = "SELECT 'VALIDACION' AS estado, * FROM #Validation ORDER BY remito_number;" if not apply else ""
    stock_select = "SELECT 'STOCK' AS estado, COUNT(*) AS stock_movements FROM #StockMoves;" if not apply else ""
    after_select = (
        """
SELECT
  'DESPUES' AS estado,
  tgt.group_id,
  tgt.remito_number,
  COUNT(DISTINCT c.scv_ID) AS active_matches
FROM #NexoraTmdRssTargets tgt
LEFT JOIN dbo.SegTiposV t
  ON LTRIM(RTRIM(t.spvtco_Cod)) = tgt.tipo
 AND LTRIM(RTRIM(t.spv_Letra)) = tgt.letra
 AND LTRIM(RTRIM(t.spv_CodPvt)) = tgt.punto
 AND LTRIM(RTRIM(t.spv_Nro)) = tgt.numero
LEFT JOIN dbo.SegCabV c
  ON c.scv_ID = t.spvscv_ID
 AND LTRIM(RTRIM(COALESCE(c.scvcli_Cod, ''))) = tgt.customer_code
GROUP BY tgt.group_id, tgt.remito_number
ORDER BY tgt.remito_number;
""".strip()
        if not apply
        else ""
    )
    return f"""
SET XACT_ABORT ON;
SET NOCOUNT ON;
USE {_sql_identifier(database)};

DECLARE @Apply bit = {apply_value};
DECLARE @BackupPrefix sysname = {_sql_string(backup_prefix)};

{_build_target_temp_table_sql(targets)}

IF OBJECT_ID('tempdb..#MatchedDocs') IS NOT NULL DROP TABLE #MatchedDocs;
SELECT
  tgt.group_id,
  tgt.remito_number,
  tgt.expected_items,
  tgt.os_ids,
  c.scv_ID
INTO #MatchedDocs
FROM #NexoraTmdRssTargets tgt
JOIN dbo.SegTiposV t
  ON LTRIM(RTRIM(t.spvtco_Cod)) = tgt.tipo
 AND LTRIM(RTRIM(t.spv_Letra)) = tgt.letra
 AND LTRIM(RTRIM(t.spv_CodPvt)) = tgt.punto
 AND LTRIM(RTRIM(t.spv_Nro)) = tgt.numero
JOIN dbo.SegCabV c
  ON c.scv_ID = t.spvscv_ID
 AND LTRIM(RTRIM(COALESCE(c.scvcli_Cod, ''))) = tgt.customer_code;

IF OBJECT_ID('tempdb..#MatchedDetails') IS NOT NULL DROP TABLE #MatchedDetails;
SELECT DISTINCT d.sdv_ID
INTO #MatchedDetails
FROM dbo.SegDetV d
JOIN #MatchedDocs md ON md.scv_ID = d.sdvscv_ID
WHERE d.sdv_ID IS NOT NULL;

IF OBJECT_ID('tempdb..#Validation') IS NOT NULL DROP TABLE #Validation;
SELECT
  tgt.group_id,
  tgt.remito_number,
  tgt.expected_items,
  COUNT(DISTINCT c.scv_ID) AS matches,
  COUNT(CASE WHEN d.sdv_TipoIt = 'A' THEN 1 END) AS article_items,
  COUNT(d.sdv_ID) AS total_lines
INTO #Validation
FROM #NexoraTmdRssTargets tgt
LEFT JOIN dbo.SegTiposV t
  ON LTRIM(RTRIM(t.spvtco_Cod)) = tgt.tipo
 AND LTRIM(RTRIM(t.spv_Letra)) = tgt.letra
 AND LTRIM(RTRIM(t.spv_CodPvt)) = tgt.punto
 AND LTRIM(RTRIM(t.spv_Nro)) = tgt.numero
LEFT JOIN dbo.SegCabV c
  ON c.scv_ID = t.spvscv_ID
 AND LTRIM(RTRIM(COALESCE(c.scvcli_Cod, ''))) = tgt.customer_code
LEFT JOIN dbo.SegDetV d
  ON d.sdvscv_ID = c.scv_ID
GROUP BY tgt.group_id, tgt.remito_number, tgt.expected_items;

{validation_select}

IF @Apply = 1 AND EXISTS (
    SELECT 1
      FROM #Validation
     WHERE matches <> 1
        OR article_items <> expected_items
)
BEGIN
    THROW 51000, 'No se borra Bejerman: algun RSS no matchea exactamente una cabecera o no coincide la cantidad de equipos.', 1;
END;

IF OBJECT_ID('tempdb..#ScvChildTables') IS NOT NULL DROP TABLE #ScvChildTables;
CREATE TABLE #ScvChildTables (
  table_name sysname NOT NULL,
  column_name sysname NOT NULL,
  delete_order int NOT NULL
);

INSERT INTO #ScvChildTables(table_name, column_name, delete_order)
SELECT 'SegCabVAux', COLUMN_NAME, 10
  FROM INFORMATION_SCHEMA.COLUMNS
 WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'SegCabVAux' AND COLUMN_NAME LIKE '%scv_ID'
UNION ALL
SELECT 'SegCuoV', COLUMN_NAME, 20
  FROM INFORMATION_SCHEMA.COLUMNS
 WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'SegCuoV' AND COLUMN_NAME LIKE '%scv_ID'
UNION ALL
SELECT 'SegTotV', COLUMN_NAME, 30
  FROM INFORMATION_SCHEMA.COLUMNS
 WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'SegTotV' AND COLUMN_NAME = 'stvscv_ID'
UNION ALL
SELECT 'SegRelDetV', COLUMN_NAME, 35
  FROM INFORMATION_SCHEMA.COLUMNS
 WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'SegRelDetV' AND COLUMN_NAME LIKE '%scv_ID%'
UNION ALL
SELECT 'SegDetV', COLUMN_NAME, 40
  FROM INFORMATION_SCHEMA.COLUMNS
 WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'SegDetV' AND COLUMN_NAME = 'sdvscv_ID'
UNION ALL
SELECT 'SegTiposV', COLUMN_NAME, 50
  FROM INFORMATION_SCHEMA.COLUMNS
 WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'SegTiposV' AND COLUMN_NAME = 'spvscv_ID';

IF OBJECT_ID('tempdb..#SdvChildTables') IS NOT NULL DROP TABLE #SdvChildTables;
CREATE TABLE #SdvChildTables (
  table_name sysname NOT NULL,
  column_name sysname NOT NULL,
  delete_order int NOT NULL
);

INSERT INTO #SdvChildTables(table_name, column_name, delete_order)
SELECT 'DtsSegDetV', COLUMN_NAME, 10
  FROM INFORMATION_SCHEMA.COLUMNS
 WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'DtsSegDetV' AND COLUMN_NAME = 'sdv_ID'
UNION ALL
SELECT 'eSalesSynchroItem', COLUMN_NAME, 20
  FROM INFORMATION_SCHEMA.COLUMNS
 WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'eSalesSynchroItem' AND COLUMN_NAME = 'sdv_ID'
UNION ALL
SELECT 'ItemDistCenCostV', COLUMN_NAME, 30
  FROM INFORMATION_SCHEMA.COLUMNS
 WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'ItemDistCenCostV' AND COLUMN_NAME = 'idvsdv_ID'
UNION ALL
SELECT 'RelRecorridoItemsVentas', COLUMN_NAME, 40
  FROM INFORMATION_SCHEMA.COLUMNS
 WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'RelRecorridoItemsVentas' AND COLUMN_NAME = 'rrvsdv_ID'
UNION ALL
SELECT 'SegRelDetV', COLUMN_NAME, 50
  FROM INFORMATION_SCHEMA.COLUMNS
 WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'SegRelDetV' AND COLUMN_NAME IN ('srvsdv_ID', 'srvsdv_IDOrig', 'srvsdv_IDPri');

IF OBJECT_ID('tempdb..#StockMoves') IS NOT NULL DROP TABLE #StockMoves;
CREATE TABLE #StockMoves (cms_ID int NOT NULL PRIMARY KEY);

IF COL_LENGTH('dbo.SegCabV', 'scvcms_ID') IS NOT NULL
BEGIN
    DECLARE @stockSql nvarchar(max);
    SET @stockSql = N'
      INSERT INTO #StockMoves(cms_ID)
      SELECT DISTINCT TRY_CONVERT(int, c.scvcms_ID)
        FROM dbo.SegCabV c
        JOIN #MatchedDocs md ON md.scv_ID = c.scv_ID
       WHERE TRY_CONVERT(int, c.scvcms_ID) IS NOT NULL
         AND NOT EXISTS (
              SELECT 1 FROM #StockMoves sm WHERE sm.cms_ID = TRY_CONVERT(int, c.scvcms_ID)
         );';
    EXEC sp_executesql @stockSql;
END;

IF OBJECT_ID('tempdb..#PartidaDeltas') IS NOT NULL DROP TABLE #PartidaDeltas;
CREATE TABLE #PartidaDeltas (
  art_codgen varchar(40) NOT NULL,
  art_codele1 varchar(40) NOT NULL,
  art_codele2 varchar(40) NOT NULL,
  art_codele3 varchar(40) NOT NULL,
  dep_cod varchar(40) NOT NULL,
  partida varchar(80) NOT NULL,
  delta_um1 float NOT NULL,
  delta_um2 float NOT NULL
);

IF OBJECT_ID('tempdb..#StockDeltas') IS NOT NULL DROP TABLE #StockDeltas;
CREATE TABLE #StockDeltas (
  art_codgen varchar(40) NOT NULL,
  art_codele1 varchar(40) NOT NULL,
  art_codele2 varchar(40) NOT NULL,
  art_codele3 varchar(40) NOT NULL,
  dep_cod varchar(40) NOT NULL,
  delta_um1 float NOT NULL,
  delta_um2 float NOT NULL
);

DECLARE @deltaMovStockColumn sysname;
SELECT TOP 1 @deltaMovStockColumn = COLUMN_NAME
  FROM INFORMATION_SCHEMA.COLUMNS
 WHERE TABLE_SCHEMA = 'dbo'
   AND TABLE_NAME = 'MovStock'
   AND COLUMN_NAME IN ('mstcms_ID', 'cms_ID')
 ORDER BY CASE WHEN COLUMN_NAME = 'mstcms_ID' THEN 0 ELSE 1 END;
IF @deltaMovStockColumn IS NULL
BEGIN
    SELECT TOP 1 @deltaMovStockColumn = COLUMN_NAME
      FROM INFORMATION_SCHEMA.COLUMNS
     WHERE TABLE_SCHEMA = 'dbo'
       AND TABLE_NAME = 'MovStock'
       AND COLUMN_NAME LIKE '%cms_ID'
     ORDER BY COLUMN_NAME;
END;

IF @deltaMovStockColumn IS NOT NULL AND EXISTS (SELECT 1 FROM #StockMoves)
BEGIN
    DECLARE @deltaSql nvarchar(max);
    SET @deltaSql = N'
      INSERT INTO #PartidaDeltas(
        art_codgen, art_codele1, art_codele2, art_codele3, dep_cod, partida, delta_um1, delta_um2
      )
      SELECT
        LTRIM(RTRIM(COALESCE(m.mstart_CodGen, ''''))),
        LTRIM(RTRIM(COALESCE(m.mstart_CodEle1, ''''))),
        LTRIM(RTRIM(COALESCE(m.mstart_CodEle2, ''''))),
        LTRIM(RTRIM(COALESCE(m.mstart_CodEle3, ''''))),
        LTRIM(RTRIM(COALESCE(m.mstdep_Cod, ''''))),
        LTRIM(RTRIM(COALESCE(m.mststp_Partida, ''''))),
        -SUM(COALESCE(NULLIF(m.mst_CantStockUM1, 0), m.mst_CantUM1, 0)),
        -SUM(COALESCE(NULLIF(m.mst_CantStockUM2, 0), m.mst_CantUM2, 0))
      FROM dbo.MovStock m
      JOIN #StockMoves sm ON sm.cms_ID = m.' + QUOTENAME(@deltaMovStockColumn) + N'
      WHERE LTRIM(RTRIM(COALESCE(m.mststp_Partida, ''''))) <> ''''
      GROUP BY
        LTRIM(RTRIM(COALESCE(m.mstart_CodGen, ''''))),
        LTRIM(RTRIM(COALESCE(m.mstart_CodEle1, ''''))),
        LTRIM(RTRIM(COALESCE(m.mstart_CodEle2, ''''))),
        LTRIM(RTRIM(COALESCE(m.mstart_CodEle3, ''''))),
        LTRIM(RTRIM(COALESCE(m.mstdep_Cod, ''''))),
        LTRIM(RTRIM(COALESCE(m.mststp_Partida, '''')));

      INSERT INTO #StockDeltas(art_codgen, art_codele1, art_codele2, art_codele3, dep_cod, delta_um1, delta_um2)
      SELECT
        LTRIM(RTRIM(COALESCE(m.mstart_CodGen, ''''))),
        LTRIM(RTRIM(COALESCE(m.mstart_CodEle1, ''''))),
        LTRIM(RTRIM(COALESCE(m.mstart_CodEle2, ''''))),
        LTRIM(RTRIM(COALESCE(m.mstart_CodEle3, ''''))),
        LTRIM(RTRIM(COALESCE(m.mstdep_Cod, ''''))),
        -SUM(COALESCE(NULLIF(m.mst_CantStockUM1, 0), m.mst_CantUM1, 0)),
        -SUM(COALESCE(NULLIF(m.mst_CantStockUM2, 0), m.mst_CantUM2, 0))
      FROM dbo.MovStock m
      JOIN #StockMoves sm ON sm.cms_ID = m.' + QUOTENAME(@deltaMovStockColumn) + N'
      GROUP BY
        LTRIM(RTRIM(COALESCE(m.mstart_CodGen, ''''))),
        LTRIM(RTRIM(COALESCE(m.mstart_CodEle1, ''''))),
        LTRIM(RTRIM(COALESCE(m.mstart_CodEle2, ''''))),
        LTRIM(RTRIM(COALESCE(m.mstart_CodEle3, ''''))),
        LTRIM(RTRIM(COALESCE(m.mstdep_Cod, '''')));';
    EXEC sp_executesql @deltaSql;
END;

{stock_select}

IF @Apply = 1
BEGIN
    DECLARE @table sysname, @column sysname, @order int, @sql nvarchar(max), @backupTable sysname;

    SET @backupTable = @BackupPrefix + '_SegCabV';
    IF OBJECT_ID(N'dbo.' + QUOTENAME(@backupTable), 'U') IS NULL
    BEGIN
        SET @sql = N'SELECT SYSDATETIME() AS backup_at, c.* INTO dbo.' + QUOTENAME(@backupTable)
            + N' FROM dbo.SegCabV c JOIN #MatchedDocs md ON md.scv_ID = c.scv_ID;';
        EXEC sp_executesql @sql;
    END;

    DECLARE child_cursor CURSOR LOCAL FAST_FORWARD FOR
        SELECT table_name, column_name, delete_order
          FROM #ScvChildTables
         ORDER BY delete_order;

    OPEN child_cursor;
    FETCH NEXT FROM child_cursor INTO @table, @column, @order;
    WHILE @@FETCH_STATUS = 0
    BEGIN
        SET @backupTable = @BackupPrefix + '_' + @table;
        IF OBJECT_ID(N'dbo.' + QUOTENAME(@backupTable), 'U') IS NULL
        BEGIN
            SET @sql = N'SELECT SYSDATETIME() AS backup_at, x.* INTO dbo.' + QUOTENAME(@backupTable)
                + N' FROM dbo.' + QUOTENAME(@table)
                + N' x JOIN #MatchedDocs md ON md.scv_ID = x.' + QUOTENAME(@column) + N';';
            EXEC sp_executesql @sql;
        END;
        FETCH NEXT FROM child_cursor INTO @table, @column, @order;
    END
    CLOSE child_cursor;
    DEALLOCATE child_cursor;

    DECLARE detail_child_cursor CURSOR LOCAL FAST_FORWARD FOR
        SELECT table_name, column_name, delete_order
          FROM #SdvChildTables
         ORDER BY delete_order;

    OPEN detail_child_cursor;
    FETCH NEXT FROM detail_child_cursor INTO @table, @column, @order;
    WHILE @@FETCH_STATUS = 0
    BEGIN
        SET @backupTable = @BackupPrefix + '_' + @table;
        IF OBJECT_ID(N'dbo.' + QUOTENAME(@backupTable), 'U') IS NULL
        BEGIN
            SET @sql = N'SELECT SYSDATETIME() AS backup_at, x.* INTO dbo.' + QUOTENAME(@backupTable)
                + N' FROM dbo.' + QUOTENAME(@table)
                + N' x JOIN #MatchedDetails md ON md.sdv_ID = x.' + QUOTENAME(@column) + N';';
            EXEC sp_executesql @sql;
        END;
        FETCH NEXT FROM detail_child_cursor INTO @table, @column, @order;
    END
    CLOSE detail_child_cursor;
    DEALLOCATE detail_child_cursor;

    IF EXISTS (SELECT 1 FROM #StockMoves)
    BEGIN
        IF OBJECT_ID('dbo.CabMovS', 'U') IS NOT NULL AND COL_LENGTH('dbo.CabMovS', 'cms_ID') IS NOT NULL
        BEGIN
            SET @backupTable = @BackupPrefix + '_CabMovS';
            IF OBJECT_ID(N'dbo.' + QUOTENAME(@backupTable), 'U') IS NULL
            BEGIN
                SET @sql = N'SELECT SYSDATETIME() AS backup_at, c.* INTO dbo.' + QUOTENAME(@backupTable)
                    + N' FROM dbo.CabMovS c JOIN #StockMoves sm ON sm.cms_ID = c.cms_ID;';
                EXEC sp_executesql @sql;
            END;
        END;

        DECLARE @movStockColumn sysname;
        SELECT TOP 1 @movStockColumn = COLUMN_NAME
          FROM INFORMATION_SCHEMA.COLUMNS
         WHERE TABLE_SCHEMA = 'dbo'
           AND TABLE_NAME = 'MovStock'
           AND COLUMN_NAME IN ('mstcms_ID', 'cms_ID')
         ORDER BY CASE WHEN COLUMN_NAME = 'mstcms_ID' THEN 0 ELSE 1 END;
        IF @movStockColumn IS NULL
        BEGIN
            SELECT TOP 1 @movStockColumn = COLUMN_NAME
              FROM INFORMATION_SCHEMA.COLUMNS
             WHERE TABLE_SCHEMA = 'dbo'
               AND TABLE_NAME = 'MovStock'
               AND COLUMN_NAME LIKE '%cms_ID'
             ORDER BY COLUMN_NAME;
        END;
        IF @movStockColumn IS NOT NULL
        BEGIN
            SET @backupTable = @BackupPrefix + '_MovStock';
            IF OBJECT_ID(N'dbo.' + QUOTENAME(@backupTable), 'U') IS NULL
            BEGIN
                SET @sql = N'SELECT SYSDATETIME() AS backup_at, m.* INTO dbo.' + QUOTENAME(@backupTable)
                    + N' FROM dbo.MovStock m JOIN #StockMoves sm ON sm.cms_ID = m.' + QUOTENAME(@movStockColumn) + N';';
                EXEC sp_executesql @sql;
            END;
        END;
    END;

    IF EXISTS (SELECT 1 FROM #PartidaDeltas)
    BEGIN
        SET @backupTable = @BackupPrefix + '_StockParPreDelete';
        IF OBJECT_ID(N'dbo.' + QUOTENAME(@backupTable), 'U') IS NULL
        BEGIN
            SET @sql = N'SELECT SYSDATETIME() AS backup_at, sp.* INTO dbo.' + QUOTENAME(@backupTable)
                + N' FROM dbo.StockPar sp JOIN #PartidaDeltas pd'
                + N' ON LTRIM(RTRIM(sp.stpart_CodGen)) = pd.art_codgen'
                + N' AND LTRIM(RTRIM(sp.stpart_CodEle1)) = pd.art_codele1'
                + N' AND LTRIM(RTRIM(sp.stpart_CodEle2)) = pd.art_codele2'
                + N' AND LTRIM(RTRIM(sp.stpart_CodEle3)) = pd.art_codele3'
                + N' AND LTRIM(RTRIM(sp.stpdep_Cod)) = pd.dep_cod'
                + N' AND LTRIM(RTRIM(sp.stp_Partida)) = pd.partida;';
            EXEC sp_executesql @sql;
        END;

        SET @backupTable = @BackupPrefix + '_DtsStockParPreDelete';
        IF OBJECT_ID(N'dbo.' + QUOTENAME(@backupTable), 'U') IS NULL
        BEGIN
            SET @sql = N'SELECT SYSDATETIME() AS backup_at, ds.* INTO dbo.' + QUOTENAME(@backupTable)
                + N' FROM dbo.DtsStockPar ds JOIN #PartidaDeltas pd'
                + N' ON LTRIM(RTRIM(ds.stpart_CodGen)) = pd.art_codgen'
                + N' AND LTRIM(RTRIM(ds.stpart_CodEle1)) = pd.art_codele1'
                + N' AND LTRIM(RTRIM(ds.stpart_CodEle2)) = pd.art_codele2'
                + N' AND LTRIM(RTRIM(ds.stpart_CodEle3)) = pd.art_codele3'
                + N' AND LTRIM(RTRIM(ds.stpdep_Cod)) = pd.dep_cod'
                + N' AND LTRIM(RTRIM(ds.stp_Partida)) = pd.partida;';
            EXEC sp_executesql @sql;
        END;
    END;

    IF EXISTS (SELECT 1 FROM #StockDeltas)
    BEGIN
        SET @backupTable = @BackupPrefix + '_StockPreDelete';
        IF OBJECT_ID(N'dbo.' + QUOTENAME(@backupTable), 'U') IS NULL
        BEGIN
            SET @sql = N'SELECT SYSDATETIME() AS backup_at, s.* INTO dbo.' + QUOTENAME(@backupTable)
                + N' FROM dbo.Stock s JOIN #StockDeltas sd'
                + N' ON LTRIM(RTRIM(s.stkart_CodGen)) = sd.art_codgen'
                + N' AND LTRIM(RTRIM(s.stkart_CodEle1)) = sd.art_codele1'
                + N' AND LTRIM(RTRIM(s.stkart_CodEle2)) = sd.art_codele2'
                + N' AND LTRIM(RTRIM(s.stkart_CodEle3)) = sd.art_codele3'
                + N' AND LTRIM(RTRIM(s.stkdep_Cod)) = sd.dep_cod;';
            EXEC sp_executesql @sql;
        END;
    END;

    BEGIN TRY
        BEGIN TRANSACTION;

        IF EXISTS (SELECT 1 FROM #StockMoves)
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM #PartidaDeltas pd
                  LEFT JOIN dbo.StockPar sp
                    ON LTRIM(RTRIM(sp.stpart_CodGen)) = pd.art_codgen
                   AND LTRIM(RTRIM(sp.stpart_CodEle1)) = pd.art_codele1
                   AND LTRIM(RTRIM(sp.stpart_CodEle2)) = pd.art_codele2
                   AND LTRIM(RTRIM(sp.stpart_CodEle3)) = pd.art_codele3
                   AND LTRIM(RTRIM(sp.stpdep_Cod)) = pd.dep_cod
                   AND LTRIM(RTRIM(sp.stp_Partida)) = pd.partida
                 WHERE sp.stp_Partida IS NULL
            )
            BEGIN
                THROW 51001, 'No se borra Bejerman: falta una partida en StockPar para restaurar stock antes del borrado.', 1;
            END;

            UPDATE sp
               SET stp_CantUM1 = COALESCE(sp.stp_CantUM1, 0) + pd.delta_um1,
                   stp_CantUM2 = COALESCE(sp.stp_CantUM2, 0) + pd.delta_um2,
                   stp_FecMod = GETDATE()
              FROM dbo.StockPar sp
              JOIN #PartidaDeltas pd
                ON LTRIM(RTRIM(sp.stpart_CodGen)) = pd.art_codgen
               AND LTRIM(RTRIM(sp.stpart_CodEle1)) = pd.art_codele1
               AND LTRIM(RTRIM(sp.stpart_CodEle2)) = pd.art_codele2
               AND LTRIM(RTRIM(sp.stpart_CodEle3)) = pd.art_codele3
               AND LTRIM(RTRIM(sp.stpdep_Cod)) = pd.dep_cod
               AND LTRIM(RTRIM(sp.stp_Partida)) = pd.partida;

            UPDATE s
               SET stk_CantUM1 = COALESCE(s.stk_CantUM1, 0) + sd.delta_um1,
                   stk_CantUM2 = COALESCE(s.stk_CantUM2, 0) + sd.delta_um2,
                   stk_FecMod = GETDATE()
              FROM dbo.Stock s
              JOIN #StockDeltas sd
                ON LTRIM(RTRIM(s.stkart_CodGen)) = sd.art_codgen
               AND LTRIM(RTRIM(s.stkart_CodEle1)) = sd.art_codele1
               AND LTRIM(RTRIM(s.stkart_CodEle2)) = sd.art_codele2
               AND LTRIM(RTRIM(s.stkart_CodEle3)) = sd.art_codele3
               AND LTRIM(RTRIM(s.stkdep_Cod)) = sd.dep_cod;

            INSERT INTO dbo.DtsStockPar(
                Dstp_CodInterno,
                stpart_CodGen,
                stpart_CodEle1,
                stpart_CodEle2,
                stpart_CodEle3,
                stpdep_Cod,
                stp_Partida
            )
            SELECT
                '',
                pd.art_codgen,
                pd.art_codele1,
                pd.art_codele2,
                pd.art_codele3,
                pd.dep_cod,
                pd.partida
              FROM #PartidaDeltas pd
             WHERE NOT EXISTS (
                    SELECT 1
                      FROM dbo.DtsStockPar ds
                     WHERE LTRIM(RTRIM(ds.stpart_CodGen)) = pd.art_codgen
                       AND LTRIM(RTRIM(ds.stpart_CodEle1)) = pd.art_codele1
                       AND LTRIM(RTRIM(ds.stpart_CodEle2)) = pd.art_codele2
                       AND LTRIM(RTRIM(ds.stpart_CodEle3)) = pd.art_codele3
                       AND LTRIM(RTRIM(ds.stpdep_Cod)) = pd.dep_cod
                       AND LTRIM(RTRIM(ds.stp_Partida)) = pd.partida
                );

            DECLARE @deleteMovStockColumn sysname;
            SELECT TOP 1 @deleteMovStockColumn = COLUMN_NAME
              FROM INFORMATION_SCHEMA.COLUMNS
             WHERE TABLE_SCHEMA = 'dbo'
               AND TABLE_NAME = 'MovStock'
               AND COLUMN_NAME IN ('mstcms_ID', 'cms_ID')
             ORDER BY CASE WHEN COLUMN_NAME = 'mstcms_ID' THEN 0 ELSE 1 END;
            IF @deleteMovStockColumn IS NULL
            BEGIN
                SELECT TOP 1 @deleteMovStockColumn = COLUMN_NAME
                  FROM INFORMATION_SCHEMA.COLUMNS
                 WHERE TABLE_SCHEMA = 'dbo'
                   AND TABLE_NAME = 'MovStock'
                   AND COLUMN_NAME LIKE '%cms_ID'
                 ORDER BY COLUMN_NAME;
            END;
            IF @deleteMovStockColumn IS NOT NULL
            BEGIN
                SET @sql = N'DELETE m FROM dbo.MovStock m JOIN #StockMoves sm ON sm.cms_ID = m.'
                    + QUOTENAME(@deleteMovStockColumn) + N';';
                EXEC sp_executesql @sql;
            END;

            IF OBJECT_ID('dbo.CabMovS', 'U') IS NOT NULL AND COL_LENGTH('dbo.CabMovS', 'cms_ID') IS NOT NULL
            BEGIN
                DELETE c
                  FROM dbo.CabMovS c
                  JOIN #StockMoves sm ON sm.cms_ID = c.cms_ID;
            END;
        END;

        DECLARE delete_detail_child_cursor CURSOR LOCAL FAST_FORWARD FOR
            SELECT table_name, column_name, delete_order
              FROM #SdvChildTables
             ORDER BY delete_order;

        OPEN delete_detail_child_cursor;
        FETCH NEXT FROM delete_detail_child_cursor INTO @table, @column, @order;
        WHILE @@FETCH_STATUS = 0
        BEGIN
            SET @sql = N'DELETE x FROM dbo.' + QUOTENAME(@table)
                + N' x JOIN #MatchedDetails md ON md.sdv_ID = x.' + QUOTENAME(@column) + N';';
            EXEC sp_executesql @sql;
            FETCH NEXT FROM delete_detail_child_cursor INTO @table, @column, @order;
        END
        CLOSE delete_detail_child_cursor;
        DEALLOCATE delete_detail_child_cursor;

        DECLARE delete_child_cursor CURSOR LOCAL FAST_FORWARD FOR
            SELECT table_name, column_name, delete_order
              FROM #ScvChildTables
             ORDER BY delete_order;

        OPEN delete_child_cursor;
        FETCH NEXT FROM delete_child_cursor INTO @table, @column, @order;
        WHILE @@FETCH_STATUS = 0
        BEGIN
            SET @sql = N'DELETE x FROM dbo.' + QUOTENAME(@table)
                + N' x JOIN #MatchedDocs md ON md.scv_ID = x.' + QUOTENAME(@column) + N';';
            EXEC sp_executesql @sql;
            FETCH NEXT FROM delete_child_cursor INTO @table, @column, @order;
        END
        CLOSE delete_child_cursor;
        DEALLOCATE delete_child_cursor;

        DELETE c
          FROM dbo.SegCabV c
          JOIN #MatchedDocs md ON md.scv_ID = c.scv_ID;

        COMMIT TRANSACTION;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0
            ROLLBACK TRANSACTION;
        THROW;
    END CATCH;
END
ELSE
BEGIN
    PRINT N'Dry-run: @Apply=0. No se modifica Bejerman.';
END;

{after_select}
""".strip()


def _remote_bejerman_sql_execute(sql: str, *, timeout_seconds: int = 120, company_key: Any = None) -> str:
    try:
        import paramiko  # type: ignore
    except Exception as exc:
        raise CommandError("No se pudo ejecutar SQL remoto: falta paramiko.") from exc

    host = _clean(os.getenv("BEJERMAN_SSH_HOST") or getattr(settings, "BEJERMAN_SSH_HOST", "")) or "45.173.2.155"
    user = _clean(os.getenv("BEJERMAN_SSH_USER") or getattr(settings, "BEJERMAN_SSH_USER", "")) or "administrator"
    password = _clean(os.getenv("BEJERMAN_SSH_PASSWORD") or getattr(settings, "BEJERMAN_SSH_PASSWORD", ""))
    sql_server = _clean(os.getenv("BEJERMAN_SQL_SERVER") or getattr(settings, "BEJERMAN_SQL_SERVER", "")) or "10.0.200.155,1433"
    database = _bejerman_sql_database_for_company(company_key)
    if not password:
        raise CommandError("No se pudo ejecutar SQL remoto: falta BEJERMAN_SSH_PASSWORD.")

    ps_script = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$sql = @'
{sql}
'@
$cs = 'Server={sql_server};Database={database};Integrated Security=SSPI;TrustServerCertificate=True;Connection Timeout=15'
$conn = New-Object System.Data.SqlClient.SqlConnection $cs
$conn.Open()
try {{
  $cmd = $conn.CreateCommand()
  $cmd.CommandTimeout = 180
  $cmd.CommandText = $sql
  [void]$cmd.ExecuteNonQuery()
  [Console]::Out.WriteLine('OK')
}} finally {{
  $conn.Close()
}}
""".strip()
    remote_path = f"C:/Windows/Temp/nexora_tmd_rss_apply_{uuid4().hex}.ps1"
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(
            hostname=host,
            username=user,
            password=password,
            timeout=timeout_seconds,
            banner_timeout=timeout_seconds,
            auth_timeout=timeout_seconds,
        )
        sftp = ssh.open_sftp()
        try:
            with sftp.file(remote_path, "wb") as handle:
                handle.write(ps_script.encode("utf-8-sig"))
        finally:
            sftp.close()
        _, stdout, stderr = ssh.exec_command(
            f'powershell -NoProfile -ExecutionPolicy Bypass -File "{remote_path}"',
            timeout=timeout_seconds + 60,
        )
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        if code != 0 or err.strip():
            raise CommandError(f"Falló el SQL remoto Bejerman: {err or out}")
        return out
    finally:
        try:
            ssh.exec_command(
                f'powershell -NoProfile -Command "Remove-Item -LiteralPath \'{remote_path}\' -Force -ErrorAction SilentlyContinue"'
            )
        except Exception:
            pass
        ssh.close()


class Command(BaseCommand):
    help = "Audita y rehace los RSS TMD del 2026-07-02 con dry-run por defecto."

    def add_arguments(self, parser):
        parser.add_argument("--date", default=DEFAULT_DATE, help="Fecha operativa auditada. Default: 2026-07-02.")
        parser.add_argument("--issue-date", default=DEFAULT_DATE, help="Fecha de emisión para los nuevos RSS.")
        parser.add_argument("--customer-code", default=DEFAULT_CUSTOMER_CODE, help="Código Bejerman del cliente.")
        parser.add_argument("--company-key", default=DEFAULT_COMPANY_KEY, help="Empresa NEXORA/Bejerman.")
        parser.add_argument(
            "--pending-os",
            default=",".join(str(item) for item in DEFAULT_PENDING_OS),
            help="OS liberadas sin RSS para sumar al lote. Usar vacío para omitirlas.",
        )
        parser.add_argument(
            "--old-remito",
            action="append",
            default=[],
            help="RSS viejo a reemplazar. Si se omite, usa los seis RSS TMD auditados.",
        )
        parser.add_argument("--actor-user-id", type=int, default=None, help="Usuario operador para eventos y emisión real.")
        parser.add_argument(
            "--out-dir",
            default="",
            help="Directorio de artefactos. Default: informes/tmd_rss_20260702_reemision/<timestamp>.",
        )
        parser.add_argument("--backup-prefix", default=DEFAULT_BACKUP_PREFIX, help="Prefijo de backups remotos Bejerman.")
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Aplica la corrección real. Requiere --confirm-production-data-fix.",
        )
        parser.add_argument(
            "--confirm-production-data-fix",
            action="store_true",
            help="Confirmación explícita para borrar/reemitir datos productivos.",
        )
        parser.add_argument(
            "--skip-remote-validation",
            action="store_true",
            help="No consulta Bejerman remoto. En apply solo usar si ya se validó por otro medio.",
        )
        parser.add_argument(
            "--skip-remote-delete",
            action="store_true",
            help="No ejecuta el borrado remoto en apply. Solo para continuar una remediación ya borrada.",
        )
        parser.add_argument(
            "--allow-count-mismatch",
            action="store_true",
            help="Permite aplicar aunque no sean 46 OS viejas y 48 totales. No recomendado.",
        )

    def handle(self, *args, **options):
        apply = bool(options.get("apply"))
        company_key = _upper(options.get("company_key")) or DEFAULT_COMPANY_KEY
        customer_code = _upper(options.get("customer_code")) or DEFAULT_CUSTOMER_CODE
        issue_date = _clean(options.get("issue_date")) or DEFAULT_DATE
        operation_date = _clean(options.get("date")) or DEFAULT_DATE
        old_remitos = options.get("old_remito") or DEFAULT_OLD_REMITOS
        old_remitos = [_parse_remito(item)["remitoNumber"] for item in old_remitos]
        pending_os = _parse_int_list(options.get("pending_os"))
        actor_user_id = options.get("actor_user_id")
        backup_prefix = _clean(options.get("backup_prefix")) or DEFAULT_BACKUP_PREFIX
        database = _bejerman_sql_database_for_company(company_key)
        out_dir = Path(
            options.get("out_dir")
            or Path("informes") / "tmd_rss_20260702_reemision" / _timestamp_slug()
        )
        out_dir.mkdir(parents=True, exist_ok=True)

        if apply:
            if not options.get("confirm_production_data_fix"):
                raise CommandError("--apply requiere --confirm-production-data-fix")
            if actor_user_id is None:
                raise CommandError("--apply requiere --actor-user-id")

        local = self._collect_local_state(
            old_remitos=old_remitos,
            pending_os=pending_os,
            customer_code=customer_code,
            company_key=company_key,
        )
        old_groups = local["old_groups"]
        old_os = local["old_os"]
        target_os = [*old_os]
        for ingreso_id in pending_os:
            if ingreso_id not in target_os:
                target_os.append(ingreso_id)

        ingreso_context = self._collect_ingreso_context(target_os)
        batches = self._build_batches(target_os)
        old_targets = self._build_old_targets(old_groups, customer_code)

        validation_sql = build_bejerman_validation_sql(old_targets, database=database)
        delete_dry_sql = build_bejerman_delete_sql(
            old_targets,
            database=database,
            backup_prefix=backup_prefix,
            apply=False,
        )
        delete_apply_sql = build_bejerman_delete_sql(
            old_targets,
            database=database,
            backup_prefix=backup_prefix,
            apply=True,
        )

        remote_before: list[dict[str, Any]] = []
        remote_warning = ""
        if not options.get("skip_remote_validation"):
            remote_before = _remote_bejerman_sql_csv(validation_sql, company_key=company_key)
            if not remote_before:
                remote_warning = "No se pudo validar Bejerman remoto o no hay credenciales SSH configuradas."

        errors = self._validate_preflight(
            old_remitos=old_remitos,
            old_groups=old_groups,
            old_os=old_os,
            target_os=target_os,
            pending_os=pending_os,
            ingreso_context=ingreso_context,
            customer_code=customer_code,
            company_key=company_key,
            remote_before=remote_before,
            allow_count_mismatch=bool(options.get("allow_count_mismatch")),
            require_remote=apply and not bool(options.get("skip_remote_validation")),
        )

        metadata = {
            "operation": "rehacer_rss_tmd_20260702",
            "dry_run": not apply,
            "date": operation_date,
            "issue_date": issue_date,
            "company_key": company_key,
            "customer_code": customer_code,
            "database": database,
            "backup_prefix": backup_prefix,
            "old_remitos": old_remitos,
            "pending_os": pending_os,
            "old_os_count": len(old_os),
            "target_os_count": len(target_os),
            "max_items_per_rss": service_release_max_items_per_order(),
            "batch_sizes": [len(batch) for batch in batches],
            "remote_warning": remote_warning,
            "errors": errors,
        }
        self._write_artifacts(
            out_dir=out_dir,
            metadata=metadata,
            local=local,
            ingreso_context=ingreso_context,
            batches=batches,
            old_targets=old_targets,
            validation_sql=validation_sql,
            delete_dry_sql=delete_dry_sql,
            delete_apply_sql=delete_apply_sql,
            remote_before=remote_before,
        )

        if errors:
            self.stdout.write(f"Reportes: {out_dir}")
            for error in errors:
                self.stdout.write(f"ERROR: {error}")
            if apply:
                raise CommandError("Preflight bloqueó el apply. Revisar artefactos.")
            self.stdout.write("Dry-run con errores de preflight. No se aplicó nada.")
            return

        if not apply:
            self.stdout.write(f"Reportes: {out_dir}")
            self.stdout.write(f"Dry-run OK. RSS viejos: {len(old_groups)} | OS viejas: {len(old_os)} | OS objetivo: {len(target_os)}")
            self.stdout.write(f"Lotes nuevos previstos: {[len(batch) for batch in batches]}")
            self.stdout.write(f"SQL dry-run: {out_dir / 'bejerman_delete_apply0.sql'}")
            if remote_warning:
                self.stdout.write(f"Advertencia: {remote_warning}")
            return

        if not options.get("skip_remote_delete"):
            remote_apply_output = _remote_bejerman_sql_execute(
                delete_apply_sql,
                company_key=company_key,
                timeout_seconds=180,
            )
            (out_dir / "remote_delete_apply_output.txt").write_text(remote_apply_output, encoding="utf-8")
            remote_after_delete = _remote_bejerman_sql_csv(validation_sql, company_key=company_key)
            _write_json(out_dir / "remote_after_delete.json", remote_after_delete)
            still_active = [
                row
                for row in remote_after_delete
                if int(float(row.get("matches") or 0)) > 0
            ]
            if still_active:
                raise CommandError("El borrado remoto no dejó todos los RSS viejos en cero. Revisar remote_after_delete.json.")

        remediation_id = f"tmd-rss-20260702-{_timestamp_slug()}"
        self._reset_local_for_reissue(
            old_order_ids=local["old_order_ids"],
            old_group_ids=[group["group_id"] for group in old_groups],
            target_os=target_os,
            actor_user_id=actor_user_id,
            remediation_id=remediation_id,
        )
        new_order_ids = self._ensure_new_service_release_orders(target_os, actor_user_id)
        result = generate_bejerman_remito(new_order_ids, actor_user_id, {"issueDate": issue_date})
        final_state = self._collect_final_state(target_os)
        mapping = self._build_mapping(local["old_os_remito"], final_state)
        _write_json(out_dir / "apply_result.json", result)
        _write_json(out_dir / "local_after.json", final_state)
        _write_csv(
            out_dir / "old_new_mapping.csv",
            mapping,
            ["ingreso_id", "old_remito", "new_remito", "estado", "fecha_entrega"],
        )
        bad_final = [
            row
            for row in final_state
            if _clean(row.get("estado")).lower() not in {"entregado", "vendido_entregado"}
            or not _clean(row.get("remito_salida"))
        ]
        if bad_final:
            _write_json(out_dir / "local_after_errors.json", bad_final)
            raise CommandError("La reemisión terminó, pero la verificación local encontró OS sin entrega/remito.")
        self._mark_old_groups_superseded(
            old_group_ids=[group["group_id"] for group in old_groups],
            result=result,
            actor_user_id=actor_user_id,
            remediation_id=remediation_id,
        )

        self.stdout.write(f"Reportes: {out_dir}")
        self.stdout.write(f"Apply OK. Nuevos RSS: {', '.join(result.get('remitoNumbers') or [result.get('remitoNumber')])}")
        self.stdout.write(f"OS entregadas verificadas: {len(final_state)}")

    def _collect_local_state(
        self,
        *,
        old_remitos: list[str],
        pending_os: list[int],
        customer_code: str,
        company_key: str,
    ) -> dict[str, Any]:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT
                  g.id AS group_id,
                  COALESCE(g.company_key, 'SEPID') AS company_key,
                  g.status AS group_status,
                  g.remito_number,
                  g.comprobante_tipo,
                  g.comprobante_letra,
                  g.comprobante_pto_venta,
                  g.comprobante_numero,
                  g.customer_code,
                  g.customer_name,
                  g.generated_at,
                  g.created_at AS group_created_at,
                  g.response_summary,
                  o.id AS order_id,
                  o.order_number,
                  o.status AS order_status,
                  o.ingreso_id AS order_ingreso_id,
                  o.source_external_id,
                  doi.id AS item_id,
                  doi.sort_order AS item_sort_order,
                  doi.ingreso_id AS item_ingreso_id,
                  doip.ingreso_id AS partida_ingreso_id
                FROM bejerman_remito_groups g
                JOIN delivery_orders o
                  ON o.bejerman_remito_group_id = g.id
                LEFT JOIN delivery_order_items doi
                  ON doi.order_id = o.id
                LEFT JOIN delivery_order_item_partidas doip
                  ON doip.order_item_id = doi.id
                WHERE g.remito_number = ANY(%s)
                  AND UPPER(COALESCE(g.customer_code, '')) = %s
                  AND UPPER(COALESCE(g.company_key, o.company_key, 'SEPID')) = %s
                ORDER BY g.remito_number, o.created_at, o.order_number, doi.sort_order NULLS LAST, doi.id
                """,
                [old_remitos, customer_code, company_key],
            )
            rows = _rows(cur)

        groups_by_id: dict[str, dict[str, Any]] = {}
        old_os: list[int] = []
        old_os_remito: dict[int, str] = {}
        old_order_ids: list[str] = []
        for row in rows:
            group_id = row["group_id"]
            group = groups_by_id.setdefault(
                group_id,
                {
                    "group_id": group_id,
                    "company_key": row.get("company_key"),
                    "status": row.get("group_status"),
                    "remito_number": row.get("remito_number"),
                    "comprobante_tipo": row.get("comprobante_tipo"),
                    "comprobante_letra": row.get("comprobante_letra"),
                    "comprobante_pto_venta": row.get("comprobante_pto_venta"),
                    "comprobante_numero": row.get("comprobante_numero"),
                    "customer_code": row.get("customer_code"),
                    "customer_name": row.get("customer_name"),
                    "generated_at": row.get("generated_at"),
                    "created_at": row.get("group_created_at"),
                    "response_summary": row.get("response_summary"),
                    "order_ids": [],
                    "ingreso_ids": [],
                },
            )
            order_id = _clean(row.get("order_id"))
            if order_id and order_id not in group["order_ids"]:
                group["order_ids"].append(order_id)
            if order_id and order_id not in old_order_ids:
                old_order_ids.append(order_id)
            ingreso_id = row.get("item_ingreso_id") or row.get("partida_ingreso_id") or row.get("order_ingreso_id")
            try:
                ingreso_id = int(ingreso_id)
            except (TypeError, ValueError):
                ingreso_id = 0
            if ingreso_id > 0:
                if ingreso_id not in group["ingreso_ids"]:
                    group["ingreso_ids"].append(ingreso_id)
                if ingreso_id not in old_os:
                    old_os.append(ingreso_id)
                old_os_remito[ingreso_id] = _clean(row.get("remito_number"))

        old_groups = list(groups_by_id.values())
        remito_order = {remito: index for index, remito in enumerate(old_remitos)}
        for group in old_groups:
            group["ingreso_ids"] = sorted(int(item) for item in (group.get("ingreso_ids") or []))
        old_groups.sort(key=lambda item: remito_order.get(item["remito_number"], 999))
        old_os = []
        for group in old_groups:
            for ingreso_id in group["ingreso_ids"]:
                if ingreso_id not in old_os:
                    old_os.append(ingreso_id)

        return {
            "old_groups": old_groups,
            "old_os": old_os,
            "old_os_remito": old_os_remito,
            "old_order_ids": old_order_ids,
            "pending_os": pending_os,
            "raw_rows": rows,
        }

    def _collect_ingreso_context(self, ingreso_ids: list[int]) -> list[dict[str, Any]]:
        if not ingreso_ids:
            return []
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT
                  i.id AS ingreso_id,
                  i.estado,
                  i.resolucion,
                  i.presupuesto_estado,
                  i.remito_salida,
                  i.fecha_entrega,
                  i.factura_numero,
                  COALESCE(i.empresa_bejerman, 'SEPID') AS company_key,
                  c.id AS customer_id,
                  COALESCE(c.cod_empresa, '') AS customer_code,
                  COALESCE(c.razon_social, '') AS customer_name,
                  COALESCE(d.numero_serie, '') AS numero_serie,
                  COALESCE(d.numero_interno, '') AS numero_interno,
                  COALESCE(m.tipo_equipo, '') AS tipo_equipo,
                  COALESCE(b.nombre, '') AS marca,
                  COALESCE(m.nombre, '') AS modelo,
                  COALESCE(i.equipo_variante, d.variante, m.variante, '') AS variante
                FROM ingresos i
                JOIN devices d ON d.id = i.device_id
                JOIN customers c ON c.id = d.customer_id
                LEFT JOIN models m ON m.id = d.model_id
                LEFT JOIN marcas b ON b.id = d.marca_id
                WHERE i.id = ANY(%s)
                ORDER BY array_position(%s::int[], i.id)
                """,
                [ingreso_ids, ingreso_ids],
            )
            return _rows(cur)

    def _build_batches(self, ingreso_ids: list[int]) -> list[list[int]]:
        max_items = service_release_max_items_per_order()
        return [ingreso_ids[index : index + max_items] for index in range(0, len(ingreso_ids), max_items)]

    def _build_old_targets(self, groups: list[dict[str, Any]], customer_code: str) -> list[dict[str, Any]]:
        targets: list[dict[str, Any]] = []
        for group in groups:
            parsed = _parse_remito(group["remito_number"])
            targets.append(
                {
                    "group_id": group["group_id"],
                    "remito_number": parsed["remitoNumber"],
                    "type": parsed["type"],
                    "letter": parsed["letter"],
                    "point": parsed["point"],
                    "number": parsed["number"],
                    "customer_code": customer_code,
                    "expected_items": len(group.get("ingreso_ids") or []),
                    "ingreso_ids": group.get("ingreso_ids") or [],
                }
            )
        return targets

    def _validate_preflight(
        self,
        *,
        old_remitos: list[str],
        old_groups: list[dict[str, Any]],
        old_os: list[int],
        target_os: list[int],
        pending_os: list[int],
        ingreso_context: list[dict[str, Any]],
        customer_code: str,
        company_key: str,
        remote_before: list[dict[str, Any]],
        allow_count_mismatch: bool,
        require_remote: bool,
    ) -> list[str]:
        errors: list[str] = []
        found_remitos = {group.get("remito_number") for group in old_groups}
        missing_remitos = [remito for remito in old_remitos if remito not in found_remitos]
        if missing_remitos:
            errors.append(f"No se encontraron en NEXORA los RSS viejos: {', '.join(missing_remitos)}")
        if len(old_groups) != len(old_remitos):
            errors.append(f"Cantidad de grupos vieja inesperada: {len(old_groups)} de {len(old_remitos)}")
        if not allow_count_mismatch and len(old_os) != EXPECTED_OLD_OS_COUNT:
            errors.append(f"Cantidad de OS viejas inesperada: {len(old_os)}; se esperaban {EXPECTED_OLD_OS_COUNT}")
        if not allow_count_mismatch and len(target_os) != EXPECTED_TARGET_OS_COUNT:
            errors.append(f"Cantidad de OS objetivo inesperada: {len(target_os)}; se esperaban {EXPECTED_TARGET_OS_COUNT}")

        context_by_id = {int(row["ingreso_id"]): row for row in ingreso_context if row.get("ingreso_id") is not None}
        missing_context = [ingreso_id for ingreso_id in target_os if ingreso_id not in context_by_id]
        if missing_context:
            errors.append(f"No se encontraron estas OS en NEXORA: {', '.join(str(item) for item in missing_context)}")
        for ingreso_id in pending_os:
            row = context_by_id.get(ingreso_id)
            if not row:
                continue
            if _clean(row.get("remito_salida")):
                errors.append(f"La OS pendiente {ingreso_id} ya tiene remito_salida={row.get('remito_salida')}")
            if _clean(row.get("estado")).lower() != "liberado":
                errors.append(f"La OS pendiente {ingreso_id} no está liberada: estado={row.get('estado')}")
        for ingreso_id, row in context_by_id.items():
            if _upper(row.get("customer_code")) != customer_code:
                errors.append(f"La OS {ingreso_id} no pertenece a TMD: customer_code={row.get('customer_code')}")
            if _upper(row.get("company_key")) != company_key:
                errors.append(f"La OS {ingreso_id} no corresponde a SEPID: company_key={row.get('company_key')}")
            if _clean(row.get("factura_numero")):
                errors.append(f"La OS {ingreso_id} tiene factura_numero={row.get('factura_numero')}; no se toca automáticamente")

        if require_remote and not remote_before:
            errors.append("No hubo validación remota Bejerman; se bloquea el apply.")
        for row in remote_before:
            matches = int(float(row.get("matches") or 0))
            expected = int(float(row.get("expected_items") or 0))
            article_items = int(float(row.get("article_items") or 0))
            if matches != 1:
                errors.append(f"Bejerman no matchea exactamente un documento para {row.get('remito_number')}: matches={matches}")
            if article_items != expected:
                errors.append(
                    f"Bejerman tiene {article_items} artículos para {row.get('remito_number')}; NEXORA esperaba {expected}"
                )
        return errors

    def _write_artifacts(
        self,
        *,
        out_dir: Path,
        metadata: dict[str, Any],
        local: dict[str, Any],
        ingreso_context: list[dict[str, Any]],
        batches: list[list[int]],
        old_targets: list[dict[str, Any]],
        validation_sql: str,
        delete_dry_sql: str,
        delete_apply_sql: str,
        remote_before: list[dict[str, Any]],
    ) -> None:
        _write_json(out_dir / "metadata.json", metadata)
        _write_json(out_dir / "local_before.json", local)
        _write_json(out_dir / "ingresos_context.json", ingreso_context)
        _write_json(out_dir / "remote_before.json", remote_before)
        (out_dir / "bejerman_validate.sql").write_text(validation_sql + "\n", encoding="utf-8")
        (out_dir / "bejerman_delete_apply0.sql").write_text(delete_dry_sql + "\n", encoding="utf-8")
        (out_dir / "bejerman_delete_apply1.sql").write_text(delete_apply_sql + "\n", encoding="utf-8")

        old_rows = [
            {
                "group_id": target["group_id"],
                "remito_number": target["remito_number"],
                "expected_items": target["expected_items"],
                "os_ids": ",".join(str(item) for item in target.get("ingreso_ids") or []),
            }
            for target in old_targets
        ]
        _write_csv(out_dir / "old_remitos.csv", old_rows, ["group_id", "remito_number", "expected_items", "os_ids"])

        batch_rows = []
        for batch_index, batch in enumerate(batches, start=1):
            for position, ingreso_id in enumerate(batch, start=1):
                batch_rows.append({"batch": batch_index, "position": position, "ingreso_id": ingreso_id})
        _write_csv(out_dir / "target_batches.csv", batch_rows, ["batch", "position", "ingreso_id"])

        if remote_before:
            _write_csv(
                out_dir / "remote_before.csv",
                remote_before,
                [
                    "group_id",
                    "remito_number",
                    "customer_code",
                    "expected_items",
                    "matches",
                    "scv_id",
                    "article_items",
                    "legend_items",
                    "total_lines",
                    "issue_date",
                    "matched_customer_code",
                ],
            )

    def _reset_local_for_reissue(
        self,
        *,
        old_order_ids: list[str],
        old_group_ids: list[str],
        target_os: list[int],
        actor_user_id: int,
        remediation_id: str,
    ) -> None:
        suffix = remediation_id[-15:]
        target_os_text = [str(item) for item in target_os]
        target_order_numbers = [f"OS-{item:05d}" for item in target_os]
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("SELECT id FROM delivery_orders WHERE id = ANY(%s) FOR UPDATE", [old_order_ids])
                cur.execute("SELECT id FROM bejerman_remito_groups WHERE id = ANY(%s) FOR UPDATE", [old_group_ids])
                cur.execute("SELECT id FROM ingresos WHERE id = ANY(%s) FOR UPDATE", [target_os])
                cur.execute(
                    """
                    UPDATE delivery_orders
                       SET status = 'cancelado',
                           order_number = order_number || '-REEMPLAZADO-' || %s || '-' || RIGHT(id, 6),
                           source_system = 'nexora-rss-reemplazado',
                           source_external_id = COALESCE(NULLIF(source_external_id, ''), id) || ':' || %s,
                           cancelled_by_user_id = COALESCE(cancelled_by_user_id, %s),
                           cancelled_at = COALESCE(cancelled_at, CURRENT_TIMESTAMP),
                           updated_at = CURRENT_TIMESTAMP
                     WHERE id = ANY(%s)
                    """,
                    [suffix, remediation_id, actor_user_id, old_order_ids],
                )
                cur.execute(
                    """
                    UPDATE delivery_orders
                       SET status = 'cancelado',
                           order_number = order_number || '-REEMPLAZADO-' || %s || '-' || RIGHT(id, 6),
                           source_system = 'nexora-rss-reemplazado',
                           source_external_id = COALESCE(NULLIF(source_external_id, ''), id) || ':' || %s,
                           bejerman_remito_group_id = CASE
                             WHEN NULLIF(BTRIM(COALESCE(remito_number, '')), '') IS NULL THEN NULL
                             ELSE bejerman_remito_group_id
                           END,
                           cancelled_by_user_id = COALESCE(cancelled_by_user_id, %s),
                           cancelled_at = COALESCE(cancelled_at, CURRENT_TIMESTAMP),
                           updated_at = CURRENT_TIMESTAMP
                     WHERE delivery_type = 'service_release'
                       AND order_number NOT LIKE %s
                       AND (
                            ingreso_id = ANY(%s)
                            OR source_external_id = ANY(%s)
                            OR order_number = ANY(%s)
                       )
                    """,
                    [
                        suffix,
                        remediation_id,
                        actor_user_id,
                        "%-REEMPLAZADO-%",
                        target_os,
                        target_os_text,
                        target_order_numbers,
                    ],
                )
                cur.execute(
                    """
                    UPDATE bejerman_remito_groups
                       SET status = 'failed_review',
                           last_error = 'Reemplazado por reemisión TMD RSS 2026-07-02',
                           response_summary = COALESCE(response_summary, '{}'::jsonb) || %s::jsonb
                     WHERE id = ANY(%s)
                    """,
                    [
                        _json_dump(
                            {
                                "orchestrationStatus": "failed_review",
                                "supersededByTmdRssReissue": True,
                                "remediationId": remediation_id,
                                "supersededAt": timezone.now().isoformat(),
                            }
                        ),
                        old_group_ids,
                    ],
                )
                cur.execute(
                    """
                    UPDATE ingresos
                       SET estado = (CASE
                             WHEN estado = 'vendido_entregado' THEN 'vendido_pendiente_entrega'
                             ELSE 'liberado'
                           END)::ticket_state,
                           remito_salida = NULL,
                           fecha_entrega = NULL,
                           factura_numero = NULL
                     WHERE id = ANY(%s)
                    """,
                    [target_os],
                )

    def _ensure_new_service_release_orders(self, target_os: list[int], actor_user_id: int) -> list[str]:
        order_ids: list[str] = []
        for ingreso_id in target_os:
            order = ensure_service_release_order_for_ingreso(ingreso_id, actor_user_id)
            if not order:
                raise CommandError(f"No se pudo preparar orden RSS para OS {ingreso_id}")
            order_id = _clean(order.get("id"))
            if order_id and order_id not in order_ids:
                order_ids.append(order_id)
        return order_ids

    def _collect_final_state(self, target_os: list[int]) -> list[dict[str, Any]]:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT id AS ingreso_id, estado, remito_salida, fecha_entrega
                  FROM ingresos
                 WHERE id = ANY(%s)
                 ORDER BY array_position(%s::int[], id)
                """,
                [target_os, target_os],
            )
            return _rows(cur)

    def _build_mapping(self, old_os_remito: dict[int, str], final_state: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows = []
        for row in final_state:
            ingreso_id = int(row["ingreso_id"])
            rows.append(
                {
                    "ingreso_id": ingreso_id,
                    "old_remito": old_os_remito.get(ingreso_id, ""),
                    "new_remito": row.get("remito_salida"),
                    "estado": row.get("estado"),
                    "fecha_entrega": row.get("fecha_entrega"),
                }
            )
        return rows

    def _mark_old_groups_superseded(
        self,
        *,
        old_group_ids: list[str],
        result: dict[str, Any],
        actor_user_id: int,
        remediation_id: str,
    ) -> None:
        patch = {
            "supersededByTmdRssReissue": True,
            "remediationId": remediation_id,
            "newGroupIds": result.get("groupIds") or ([result.get("groupId")] if result.get("groupId") else []),
            "newRemitoNumbers": result.get("remitoNumbers") or ([result.get("remitoNumber")] if result.get("remitoNumber") else []),
            "supersededByUserId": actor_user_id,
            "supersededAt": timezone.now().isoformat(),
        }
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE bejerman_remito_groups
                   SET response_summary = COALESCE(response_summary, '{}'::jsonb) || %s::jsonb
                 WHERE id = ANY(%s)
                """,
                [_json_dump(patch), old_group_ids],
            )
