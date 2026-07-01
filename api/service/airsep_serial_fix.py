from __future__ import annotations

import base64
import csv
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from django.db import connection, transaction


AIRSEP_SERIAL_EXCEPTION_RE = re.compile(r"^[1-4]\d{5}$")
NUMERIC_SERIAL_RE = re.compile(r"^\d+$")
ALREADY_FIXED_RE = re.compile(r"^N\d+$", re.IGNORECASE)

DEVICE_REFERENCE_TABLES = (
    "ingresos",
    "bejerman_sync_jobs",
    "delivery_orders",
    "delivery_order_items",
    "device_mg_events",
    "preventivo_planes",
    "preventivo_revision_items",
)

SERIAL_SNAPSHOT_COLUMNS = (
    ("bejerman_sync_jobs", "numero_serie"),
    ("delivery_orders", "equipment_serial"),
    ("delivery_order_items", "partida"),
    ("delivery_order_item_partidas", "partida"),
    ("preventivo_revision_items", "serie_snapshot"),
)

BEJERMAN_HISTORY_COLUMNS = (
    ("dbo", "MovStock", "mststp_Partida", ("mstart_CodGen", "mstart_CodEle1", "mstart_CodEle2", "mstart_CodEle3")),
    ("dbo", "SegDetV", "sdvstp_Partida", ("sdvart_CodGen", "sdvart_CodEle1", "sdvart_CodEle2", "sdvart_CodEle3")),
    ("dbo", "SegDetC", "sdcstp_Partida", ("sdcart_CodGen", "sdcart_CodEle1", "sdcart_CodEle2", "sdcart_CodEle3")),
)


@dataclass
class DeviceRow:
    id: int
    numero_serie: str
    numero_interno: str
    marca: str
    modelo: str
    article_code: str
    ingreso_count: int

    @property
    def ns_norm(self) -> str:
        return normalize_serial_key(self.numero_serie)


@dataclass
class AirSepCandidate:
    old_device_id: int
    old_serial: str
    old_norm: str
    new_serial: str
    old_numero_interno: str
    marca: str
    modelo: str
    article_code: str
    action: str
    canonical_device_id: int | None = None
    canonical_serial: str = ""
    canonical_numero_interno: str = ""
    canonical_marca: str = ""
    block_reason: str = ""
    internal_note: str = ""


def timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def normalize_serial_key(value: Any) -> str:
    return re.sub(r"[\s-]+", "", str(value or "").strip()).upper()


def airsep_fixed_serial(value: Any) -> str | None:
    norm = normalize_serial_key(value)
    if not norm:
        return None
    if ALREADY_FIXED_RE.match(norm):
        return None
    if not NUMERIC_SERIAL_RE.match(norm):
        return None
    if AIRSEP_SERIAL_EXCEPTION_RE.match(norm):
        return None
    return f"N{norm}"


def _quote_name(name: str) -> str:
    return connection.ops.quote_name(name)


def table_exists(table_name: str) -> bool:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                  FROM information_schema.tables
                 WHERE table_schema = 'public'
                   AND table_name = %s
            )
            """,
            [table_name],
        )
        row = cur.fetchone()
    return bool(row and row[0])


def column_exists(table_name: str, column_name: str) -> bool:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                  FROM information_schema.columns
                 WHERE table_schema = 'public'
                   AND table_name = %s
                   AND column_name = %s
            )
            """,
            [table_name, column_name],
        )
        row = cur.fetchone()
    return bool(row and row[0])


def _optional_count_expr(table_name: str, column_name: str = "device_id") -> str:
    if table_exists(table_name) and column_exists(table_name, column_name):
        return f"(SELECT COUNT(*) FROM {_quote_name(table_name)} x WHERE x.{_quote_name(column_name)} = d.id)"
    return "0"


def _article_code_expr() -> str:
    if not table_exists("bejerman_article_mappings"):
        return "''"
    required = {"model_id", "article_code"}
    if not all(column_exists("bejerman_article_mappings", column) for column in required):
        return "''"
    return (
        "(SELECT COALESCE(bam.article_code, '') "
        "   FROM bejerman_article_mappings bam "
        "  WHERE bam.model_id = d.model_id "
        "  ORDER BY CASE WHEN COALESCE(NULLIF(TRIM(bam.variante_norm), ''), '') = '' THEN 1 ELSE 0 END, bam.id "
        "  LIMIT 1)"
    )


def _load_device_rows(*, airsep_only: bool) -> list[DeviceRow]:
    ingreso_count_expr = _optional_count_expr("ingresos")
    article_expr = _article_code_expr()
    where = ""
    if airsep_only:
        where = "WHERE UPPER(REPLACE(COALESCE(b.nombre, ''), ' ', '')) LIKE '%AIRSEP%'"
    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT d.id,
                   COALESCE(d.numero_serie, '') AS numero_serie,
                   COALESCE(d.numero_interno, '') AS numero_interno,
                   COALESCE(b.nombre, '') AS marca,
                   COALESCE(m.nombre, '') AS modelo,
                   COALESCE({article_expr}, '') AS article_code,
                   {ingreso_count_expr} AS ingreso_count
              FROM devices d
              LEFT JOIN marcas b ON b.id = d.marca_id
              LEFT JOIN models m ON m.id = d.model_id
             {where}
             ORDER BY d.id
            """
        )
        rows = cur.fetchall() or []
    return [
        DeviceRow(
            id=int(row[0]),
            numero_serie=row[1] or "",
            numero_interno=row[2] or "",
            marca=row[3] or "",
            modelo=row[4] or "",
            article_code=row[5] or "",
            ingreso_count=int(row[6] or 0),
        )
        for row in rows
    ]


def load_airsep_candidates() -> list[AirSepCandidate]:
    all_rows = _load_device_rows(airsep_only=False)
    airsep_rows = _load_device_rows(airsep_only=True)

    by_norm: dict[str, list[DeviceRow]] = {}
    for row in all_rows:
        key = row.ns_norm
        if key:
            by_norm.setdefault(key, []).append(row)

    desired_counts: dict[str, int] = {}
    provisional: list[tuple[DeviceRow, str]] = []
    for row in airsep_rows:
        new_serial = airsep_fixed_serial(row.numero_serie)
        if not new_serial:
            continue
        provisional.append((row, new_serial))
        desired_counts[new_serial.upper()] = desired_counts.get(new_serial.upper(), 0) + 1

    candidates: list[AirSepCandidate] = []
    for row, new_serial in provisional:
        if desired_counts.get(new_serial.upper(), 0) > 1:
            candidates.append(
                AirSepCandidate(
                    old_device_id=row.id,
                    old_serial=row.numero_serie,
                    old_norm=row.ns_norm,
                    new_serial=new_serial,
                    old_numero_interno=row.numero_interno,
                    marca=row.marca,
                    modelo=row.modelo,
                    article_code=row.article_code,
                    action="blocked",
                    block_reason="duplicate_candidate_target",
                )
            )
            continue

        conflicts = [item for item in by_norm.get(new_serial.upper(), []) if item.id != row.id]
        if not conflicts:
            candidates.append(
                AirSepCandidate(
                    old_device_id=row.id,
                    old_serial=row.numero_serie,
                    old_norm=row.ns_norm,
                    new_serial=new_serial,
                    old_numero_interno=row.numero_interno,
                    marca=row.marca,
                    modelo=row.modelo,
                    article_code=row.article_code,
                    action="update",
                )
            )
            continue

        sorted_conflicts = sorted(conflicts, key=lambda item: (-item.ingreso_count, item.id))
        canonical = sorted_conflicts[0]
        block_reason = ""
        action = "merge"
        if len(sorted_conflicts) > 1:
            action = "blocked"
            block_reason = "multiple_target_devices"
        elif "AIRSEP" not in canonical.marca.replace(" ", "").upper():
            action = "blocked"
            block_reason = "target_device_not_airsep"

        internal_note = ""
        old_internal = row.numero_interno.strip()
        canonical_internal = canonical.numero_interno.strip()
        if canonical_internal and old_internal and canonical_internal != old_internal:
            internal_note = "canonical_internal_kept_source_differs"
        elif not canonical_internal and old_internal:
            internal_note = "source_internal_will_be_copied"

        candidates.append(
            AirSepCandidate(
                old_device_id=row.id,
                old_serial=row.numero_serie,
                old_norm=row.ns_norm,
                new_serial=new_serial,
                old_numero_interno=row.numero_interno,
                marca=row.marca,
                modelo=row.modelo,
                article_code=row.article_code,
                action=action,
                canonical_device_id=canonical.id,
                canonical_serial=canonical.numero_serie,
                canonical_numero_interno=canonical.numero_interno,
                canonical_marca=canonical.marca,
                block_reason=block_reason,
                internal_note=internal_note,
            )
        )
    return candidates


def _serial_norm_sql(column_name: str) -> str:
    quoted = _quote_name(column_name)
    return f"UPPER(REPLACE(REPLACE(COALESCE({quoted}, ''), ' ', ''), '-', ''))"


def _reference_count(cur, table_name: str, device_id: int) -> int:
    if not table_exists(table_name) or not column_exists(table_name, "device_id"):
        return 0
    cur.execute(f"SELECT COUNT(*) FROM {_quote_name(table_name)} WHERE device_id = %s", [device_id])
    row = cur.fetchone()
    return int(row[0] or 0) if row else 0


def _active_preventivo_conflict(cur, source_device_id: int, target_device_id: int) -> bool:
    if not table_exists("preventivo_planes"):
        return False
    if not column_exists("preventivo_planes", "device_id") or not column_exists("preventivo_planes", "activa"):
        return False
    cur.execute(
        """
        SELECT
          EXISTS(SELECT 1 FROM preventivo_planes WHERE device_id = %s AND activa = TRUE),
          EXISTS(SELECT 1 FROM preventivo_planes WHERE device_id = %s AND activa = TRUE)
        """,
        [source_device_id, target_device_id],
    )
    row = cur.fetchone()
    return bool(row and row[0] and row[1])


def _update_snapshot_column(cur, table_name: str, column_name: str, old_norm: str, new_serial: str) -> int:
    if not table_exists(table_name) or not column_exists(table_name, column_name):
        return 0
    cur.execute(
        f"""
        UPDATE {_quote_name(table_name)}
           SET {_quote_name(column_name)} = %s
         WHERE {_serial_norm_sql(column_name)} = %s
        """,
        [new_serial, old_norm],
    )
    return int(cur.rowcount or 0)


def _update_snapshot_columns(cur, candidate: AirSepCandidate) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table_name, column_name in SERIAL_SNAPSHOT_COLUMNS:
        changed = _update_snapshot_column(cur, table_name, column_name, candidate.old_norm, candidate.new_serial)
        if changed:
            counts[f"{table_name}.{column_name}"] = changed
    return counts


def _move_device_references(cur, source_device_id: int, target_device_id: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table_name in DEVICE_REFERENCE_TABLES:
        if not table_exists(table_name) or not column_exists(table_name, "device_id"):
            continue
        cur.execute(
            f"UPDATE {_quote_name(table_name)} SET device_id = %s WHERE device_id = %s",
            [target_device_id, source_device_id],
        )
        if cur.rowcount:
            counts[table_name] = int(cur.rowcount)
    return counts


def _copy_internal_if_needed(cur, candidate: AirSepCandidate) -> str:
    if not candidate.canonical_device_id:
        return ""
    canonical_internal = (candidate.canonical_numero_interno or "").strip()
    source_internal = (candidate.old_numero_interno or "").strip()
    if canonical_internal:
        return "kept_canonical"
    if not source_internal:
        return "none"
    cur.execute(
        "UPDATE devices SET numero_interno = %s WHERE id = %s",
        [source_internal, candidate.canonical_device_id],
    )
    return "copied_source"


def _canonicalize_target_serial(cur, candidate: AirSepCandidate) -> None:
    if not candidate.canonical_device_id:
        return
    if normalize_serial_key(candidate.canonical_serial) == candidate.new_serial.upper() and candidate.canonical_serial == candidate.new_serial:
        return
    cur.execute("UPDATE devices SET numero_serie = %s WHERE id = %s", [candidate.new_serial, candidate.canonical_device_id])


def collect_candidate_backups(candidates: list[AirSepCandidate]) -> dict[str, list[dict[str, Any]]]:
    device_ids: set[int] = set()
    for candidate in candidates:
        device_ids.add(candidate.old_device_id)
        if candidate.canonical_device_id:
            device_ids.add(candidate.canonical_device_id)
    backups: dict[str, list[dict[str, Any]]] = {"devices": [], "references": []}
    if not device_ids:
        return backups
    ids = sorted(device_ids)
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id, customer_id, marca_id, model_id, numero_serie, numero_interno,
                   tipo_equipo, variante, garantia_vence, propietario, n_de_control, alquilado, alquiler_a
              FROM devices
             WHERE id = ANY(%s)
             ORDER BY id
            """,
            [ids],
        )
        columns = [column[0] for column in cur.description]
        backups["devices"] = [dict(zip(columns, row)) for row in (cur.fetchall() or [])]

        for device_id in ids:
            row = {"device_id": device_id}
            for table_name in DEVICE_REFERENCE_TABLES:
                row[table_name] = _reference_count(cur, table_name, device_id)
            backups["references"].append(row)
    return backups


def apply_local_corrections(candidates: list[AirSepCandidate], *, apply: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "apply": bool(apply),
        "updated_devices": 0,
        "merged_devices": 0,
        "deleted_devices": 0,
        "blocked": 0,
        "snapshot_updates": {},
        "reference_updates": {},
        "actions": [],
    }
    actionable = [item for item in candidates if item.action in {"update", "merge"}]
    if not apply:
        result["updated_devices"] = sum(1 for item in actionable if item.action == "update")
        result["merged_devices"] = sum(1 for item in actionable if item.action == "merge")
        result["blocked"] = sum(1 for item in candidates if item.action == "blocked")
        result["actions"] = [asdict(item) for item in candidates]
        return result

    with transaction.atomic():
        with connection.cursor() as cur:
            for candidate in candidates:
                action_row = asdict(candidate)
                action_row["applied"] = False
                action_row["delete_device"] = ""
                if candidate.action == "blocked":
                    result["blocked"] += 1
                    result["actions"].append(action_row)
                    continue

                if candidate.action == "update":
                    cur.execute(
                        "UPDATE devices SET numero_serie = %s WHERE id = %s",
                        [candidate.new_serial, candidate.old_device_id],
                    )
                    result["updated_devices"] += int(cur.rowcount or 0)
                    snapshot_counts = _update_snapshot_columns(cur, candidate)
                    for key, value in snapshot_counts.items():
                        result["snapshot_updates"][key] = result["snapshot_updates"].get(key, 0) + value
                    action_row["applied"] = True
                    result["actions"].append(action_row)
                    continue

                if not candidate.canonical_device_id:
                    action_row["block_reason"] = "missing_canonical_device"
                    result["blocked"] += 1
                    result["actions"].append(action_row)
                    continue

                if _active_preventivo_conflict(cur, candidate.old_device_id, candidate.canonical_device_id):
                    action_row["action"] = "blocked"
                    action_row["block_reason"] = "active_preventivo_plan_conflict"
                    result["blocked"] += 1
                    result["actions"].append(action_row)
                    continue

                action_row["internal_result"] = _copy_internal_if_needed(cur, candidate)
                _canonicalize_target_serial(cur, candidate)
                reference_counts = _move_device_references(cur, candidate.old_device_id, candidate.canonical_device_id)
                for key, value in reference_counts.items():
                    result["reference_updates"][key] = result["reference_updates"].get(key, 0) + value
                snapshot_counts = _update_snapshot_columns(cur, candidate)
                for key, value in snapshot_counts.items():
                    result["snapshot_updates"][key] = result["snapshot_updates"].get(key, 0) + value

                cur.execute("DELETE FROM devices WHERE id = %s", [candidate.old_device_id])
                deleted = int(cur.rowcount or 0)
                result["deleted_devices"] += deleted
                result["merged_devices"] += 1 if deleted else 0
                action_row["applied"] = bool(deleted)
                action_row["delete_device"] = "deleted" if deleted else "not_deleted"
                result["actions"].append(action_row)
    return result


def bejerman_mappings_from_candidates(candidates: list[AirSepCandidate]) -> list[dict[str, str]]:
    by_old: dict[str, dict[str, str]] = {}
    for candidate in candidates:
        if not candidate.old_norm or not candidate.new_serial:
            continue
        current = by_old.get(candidate.old_norm)
        article_code = (candidate.article_code or "").strip()
        if current and current.get("article_code"):
            continue
        by_old[candidate.old_norm] = {
            "old_partida": candidate.old_norm,
            "new_partida": candidate.new_serial,
            "article_code": article_code,
        }
    return [by_old[key] for key in sorted(by_old)]


def _sql_string(value: Any) -> str:
    return "N'" + str(value or "").replace("'", "''") + "'"


def _sql_identifier(value: str) -> str:
    if not re.match(r"^[A-Za-z0-9_]+$", value or ""):
        raise ValueError(f"Identificador SQL no valido: {value}")
    return f"[{value}]"


def _article_match_sql(alias: str, columns: tuple[str, str, str, str]) -> str:
    pieces = " + ".join(f"RTRIM(COALESCE({alias}.{column}, ''))" for column in columns)
    return f"(m.article_code = '' OR REPLACE({pieces}, '-', '') = REPLACE(m.article_code, '-', ''))"


def build_bejerman_sql(mappings: list[dict[str, str]], *, database: str = "SBDASEP", include_history: bool = True) -> str:
    db_name = _sql_identifier(database)
    lines = [
        "SET XACT_ABORT ON;",
        f"USE {db_name};",
        "",
        "IF OBJECT_ID('tempdb..#AirSepPartidaMap') IS NOT NULL DROP TABLE #AirSepPartidaMap;",
        "CREATE TABLE #AirSepPartidaMap (",
        "  old_partida NVARCHAR(80) NOT NULL PRIMARY KEY,",
        "  new_partida NVARCHAR(80) NOT NULL,",
        "  article_code NVARCHAR(80) NOT NULL DEFAULT ''",
        ");",
    ]
    if mappings:
        values = ",\n".join(
            f"({_sql_string(item['old_partida'])}, {_sql_string(item['new_partida'])}, {_sql_string(item.get('article_code', ''))})"
            for item in mappings
        )
        lines.append("INSERT INTO #AirSepPartidaMap (old_partida, new_partida, article_code) VALUES")
        lines.append(values + ";")
    lines.extend(
        [
            "",
            "IF OBJECT_ID('tempdb..#AirSepStockParConflicts') IS NOT NULL DROP TABLE #AirSepStockParConflicts;",
            "CREATE TABLE #AirSepStockParConflicts (",
            "  old_partida NVARCHAR(80),",
            "  new_partida NVARCHAR(80),",
            "  article_code NVARCHAR(80),",
            "  stpdep_Cod NVARCHAR(40),",
            "  old_cant_um1 DECIMAL(28, 8) NULL,",
            "  new_cant_um1 DECIMAL(28, 8) NULL",
            ");",
            "",
            "IF OBJECT_ID('tempdb..#AirSepUnknownPartidaMatches') IS NOT NULL DROP TABLE #AirSepUnknownPartidaMatches;",
            "CREATE TABLE #AirSepUnknownPartidaMatches (",
            "  table_schema SYSNAME,",
            "  table_name SYSNAME,",
            "  column_name SYSNAME,",
            "  match_count INT",
            ");",
            "",
            "DECLARE @schema SYSNAME, @table SYSNAME, @column SYSNAME, @dynsql NVARCHAR(MAX), @count INT;",
            "DECLARE unknown_partida_columns CURSOR LOCAL FAST_FORWARD FOR",
            "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME",
            "  FROM INFORMATION_SCHEMA.COLUMNS",
            " WHERE COLUMN_NAME LIKE '%Partida%'",
            "   AND DATA_TYPE IN ('char','varchar','nchar','nvarchar','text','ntext')",
            "   AND NOT (TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'StockPar' AND COLUMN_NAME = 'stp_Partida')",
            "   AND NOT (TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'MovStock' AND COLUMN_NAME = 'mststp_Partida')",
            "   AND NOT (TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'SegDetV' AND COLUMN_NAME = 'sdvstp_Partida')",
            "   AND NOT (TABLE_SCHEMA = 'dbo' AND TABLE_NAME = 'SegDetC' AND COLUMN_NAME = 'sdcstp_Partida');",
            "OPEN unknown_partida_columns;",
            "FETCH NEXT FROM unknown_partida_columns INTO @schema, @table, @column;",
            "WHILE @@FETCH_STATUS = 0",
            "BEGIN",
            "  SET @count = 0;",
            "  SET @dynsql = N'SELECT @count = COUNT(1) FROM ' + QUOTENAME(@schema) + N'.' + QUOTENAME(@table)",
            "    + N' t JOIN #AirSepPartidaMap m ON LTRIM(RTRIM(CONVERT(NVARCHAR(255), t.' + QUOTENAME(@column) + N'))) = m.old_partida';",
            "  EXEC sp_executesql @dynsql, N'@count INT OUTPUT', @count OUTPUT;",
            "  IF @count > 0",
            "    INSERT INTO #AirSepUnknownPartidaMatches(table_schema, table_name, column_name, match_count)",
            "    VALUES(@schema, @table, @column, @count);",
            "  FETCH NEXT FROM unknown_partida_columns INTO @schema, @table, @column;",
            "END",
            "CLOSE unknown_partida_columns;",
            "DEALLOCATE unknown_partida_columns;",
            "",
            "BEGIN TRANSACTION;",
            "",
            "INSERT INTO #AirSepStockParConflicts(old_partida, new_partida, article_code, stpdep_Cod, old_cant_um1, new_cant_um1)",
            "SELECT m.old_partida, m.new_partida, m.article_code, old_sp.stpdep_Cod, old_sp.stp_CantUM1, new_sp.stp_CantUM1",
            "  FROM dbo.StockPar old_sp",
            "  JOIN #AirSepPartidaMap m",
            "    ON LTRIM(RTRIM(old_sp.stp_Partida)) = m.old_partida",
            "  JOIN dbo.StockPar new_sp",
            "    ON new_sp.stpart_CodGen = old_sp.stpart_CodGen",
            "   AND new_sp.stpart_CodEle1 = old_sp.stpart_CodEle1",
            "   AND new_sp.stpart_CodEle2 = old_sp.stpart_CodEle2",
            "   AND new_sp.stpart_CodEle3 = old_sp.stpart_CodEle3",
            "   AND new_sp.stpdep_Cod = old_sp.stpdep_Cod",
            "   AND LTRIM(RTRIM(new_sp.stp_Partida)) = m.new_partida",
            f" WHERE {_article_match_sql('old_sp', ('stpart_CodGen', 'stpart_CodEle1', 'stpart_CodEle2', 'stpart_CodEle3'))};",
            "",
            "UPDATE old_sp",
            "   SET stp_Partida = m.new_partida",
            "  FROM dbo.StockPar old_sp",
            "  JOIN #AirSepPartidaMap m",
            "    ON LTRIM(RTRIM(old_sp.stp_Partida)) = m.old_partida",
            f" WHERE {_article_match_sql('old_sp', ('stpart_CodGen', 'stpart_CodEle1', 'stpart_CodEle2', 'stpart_CodEle3'))}",
            "   AND NOT EXISTS (",
            "       SELECT 1",
            "         FROM dbo.StockPar new_sp",
            "        WHERE new_sp.stpart_CodGen = old_sp.stpart_CodGen",
            "          AND new_sp.stpart_CodEle1 = old_sp.stpart_CodEle1",
            "          AND new_sp.stpart_CodEle2 = old_sp.stpart_CodEle2",
            "          AND new_sp.stpart_CodEle3 = old_sp.stpart_CodEle3",
            "          AND new_sp.stpdep_Cod = old_sp.stpdep_Cod",
            "          AND LTRIM(RTRIM(new_sp.stp_Partida)) = m.new_partida",
            "   );",
        ]
    )
    if include_history:
        for schema, table, column, article_columns in BEJERMAN_HISTORY_COLUMNS:
            lines.extend(
                [
                    "",
                    f"UPDATE hist",
                    f"   SET {column} = m.new_partida",
                    f"  FROM {schema}.{table} hist",
                    "  JOIN #AirSepPartidaMap m",
                    f"    ON LTRIM(RTRIM(hist.{column})) = m.old_partida",
                    f" WHERE {_article_match_sql('hist', article_columns)};",
                ]
            )
    lines.extend(
        [
            "",
            "COMMIT TRANSACTION;",
            "",
            "SELECT 'map_rows' AS metric, COUNT(*) AS value FROM #AirSepPartidaMap;",
            "SELECT 'stockpar_conflicts' AS metric, COUNT(*) AS value FROM #AirSepStockParConflicts;",
            "SELECT * FROM #AirSepStockParConflicts ORDER BY old_partida, stpdep_Cod;",
            "SELECT * FROM #AirSepUnknownPartidaMatches ORDER BY table_schema, table_name, column_name;",
        ]
    )
    return "\n".join(lines) + "\n"


def write_reports(
    out_dir: str | Path,
    *,
    candidates: list[AirSepCandidate],
    local_result: dict[str, Any],
    backups: dict[str, list[dict[str, Any]]],
    bejerman_sql: str,
    bejerman_mappings: list[dict[str, str]],
) -> Path:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    candidate_rows = [asdict(candidate) for candidate in candidates]
    _write_csv(out_path / "candidates.csv", candidate_rows)
    _write_csv(out_path / "local_actions.csv", local_result.get("actions") or [])
    _write_csv(out_path / "backup_devices.csv", backups.get("devices") or [])
    _write_csv(out_path / "backup_references.csv", backups.get("references") or [])
    _write_csv(out_path / "bejerman_mapping.csv", bejerman_mappings)
    (out_path / "bejerman_airsep_serial_fix.sql").write_text(bejerman_sql, encoding="utf-8")

    summary = {
        "candidates": len(candidates),
        "candidate_updates": sum(1 for item in candidates if item.action == "update"),
        "candidate_merges": sum(1 for item in candidates if item.action == "merge"),
        "candidate_blocked": sum(1 for item in candidates if item.action == "blocked"),
        "local_result": local_result,
        "bejerman_mapping_rows": len(bejerman_mappings),
    }
    (out_path / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return out_path


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def execute_bejerman_sql_via_plink(
    sql_script: str,
    *,
    plink_path: str,
    ssh_host: str,
    ssh_user: str,
    ssh_password: str,
    sql_server: str = "10.0.200.155,1433",
    sql_database: str = "SBDASEP",
    ssh_hostkey: str = "",
    timeout_seconds: int = 300,
) -> subprocess.CompletedProcess[str]:
    resolved_plink = shutil.which(plink_path) or (plink_path if Path(plink_path).exists() else "")
    if not resolved_plink:
        raise FileNotFoundError(f"No se encontro plink: {plink_path}")
    remote_script = f"""
$ErrorActionPreference = 'Stop'
$sql = @'
{sql_script}
'@
$cs = 'Server={sql_server};Database={sql_database};Integrated Security=SSPI;TrustServerCertificate=True;Connection Timeout=15'
$conn = New-Object System.Data.SqlClient.SqlConnection $cs
$conn.Open()
try {{
  $cmd = $conn.CreateCommand()
  $cmd.CommandTimeout = 0
  $cmd.CommandText = $sql
  $rows = $cmd.ExecuteNonQuery()
  Write-Output ('SQL_OK rows=' + $rows)
}} finally {{
  $conn.Close()
}}
"""
    encoded = base64.b64encode(remote_script.encode("utf-16le")).decode("ascii")
    command = [
        resolved_plink,
        "-batch",
        "-ssh",
        "-pw",
        ssh_password,
    ]
    if ssh_hostkey:
        command.extend(["-hostkey", ssh_hostkey])
    command.extend([f"{ssh_user}@{ssh_host}", "powershell", "-NoProfile", "-EncodedCommand", encoded])
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )


def default_out_dir() -> str:
    return str(Path("informes") / f"airsep_serial_fix_{timestamp_slug()}")


def bejerman_remote_options_from_env() -> dict[str, str]:
    password_env = os.getenv("BEJERMAN_SSH_PASSWORD_ENV", "BEJERMAN_SSH_PASSWORD")
    return {
        "plink_path": os.getenv("PLINK_PATH", "plink.exe"),
        "ssh_host": os.getenv("BEJERMAN_SSH_HOST", "45.173.2.155"),
        "ssh_user": os.getenv("BEJERMAN_SSH_USER", "administrator"),
        "ssh_password": os.getenv(password_env, ""),
        "ssh_hostkey": os.getenv("BEJERMAN_SSH_HOSTKEY", ""),
        "sql_server": os.getenv("BEJERMAN_SQL_SERVER", "10.0.200.155,1433"),
        "sql_database": os.getenv("BEJERMAN_SQL_DATABASE", "SBDASEP"),
    }
