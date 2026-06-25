from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from service.bejerman_companies import company_for_key
from service.bejerman_sdk import BejermanSDKClient, BejermanSdkError, as_string, first_value, normalize_search, records_from_response


BEJERMAN_CODE_FIELDS = ("Cliente_Codigo", "ClienteCodigo", "CodigoCliente", "Codigo", "CodCliente")
BEJERMAN_NAME_FIELDS = ("Cliente_RazonSocial", "Cliente_Nombre", "RazonSocial", "Nombre", "Cliente")
BEJERMAN_CUIT_FIELDS = ("Cliente_NroDocumento", "Cliente_CUIT", "Cliente_Cuit", "CUIT", "Cuit", "TaxId")
CUSTOMER_NAME_MATCH_THRESHOLD = 0.82


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _clean_upper(value: Any) -> str:
    return _clean(value).upper()


def _digits_only(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _valid_cuit(value: Any) -> str:
    digits = _digits_only(value)
    return digits if len(digits) == 11 else ""


def _name_score(left: Any, right: Any) -> float:
    left_norm = normalize_search(left)
    right_norm = normalize_search(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    ratio = SequenceMatcher(None, left_norm, right_norm).ratio()
    if left_norm in right_norm or right_norm in left_norm:
        ratio = max(ratio, 0.86)
    return ratio


def _name_matches(local_name: Any, bejerman_name: Any) -> bool:
    local = _clean(local_name)
    remote = _clean(bejerman_name)
    if not local or not remote:
        return True
    return _name_score(local, remote) >= CUSTOMER_NAME_MATCH_THRESHOLD


def _table_columns(table_name: str) -> set[str]:
    with connection.cursor() as cur:
        return {column.name for column in connection.introspection.get_table_description(cur, table_name)}


def _fetch_dicts(sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    with connection.cursor() as cur:
        cur.execute(sql, params or [])
        columns = [column[0] for column in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def _resolved_company_key(client: BejermanSDKClient, fallback: str) -> str:
    marker = _clean(getattr(client, "company_key", "")) or _clean(getattr(client, "company", "")) or fallback
    company = company_for_key(marker, default=None)
    if company:
        return company.key
    return _clean_upper(marker)


def _customer_rows() -> list[dict[str, Any]]:
    columns = _table_columns("customers")
    required = {"id", "razon_social", "cod_empresa", "cuit"}
    missing = sorted(required - columns)
    if missing:
        raise CommandError("Faltan columnas en customers: " + ", ".join(missing))
    return _fetch_dicts(
        """
        SELECT id,
               COALESCE(razon_social, '') AS razon_social,
               COALESCE(cod_empresa, '') AS cod_empresa,
               COALESCE(cuit, '') AS cuit
          FROM customers
         ORDER BY id ASC
        """
    )


def _bejerman_payload(record: dict[str, Any]) -> dict[str, str]:
    return {
        "code": _clean_upper(as_string(first_value(record, BEJERMAN_CODE_FIELDS))),
        "name": _clean(as_string(first_value(record, BEJERMAN_NAME_FIELDS))),
        "cuit": _valid_cuit(first_value(record, BEJERMAN_CUIT_FIELDS)),
        "raw_cuit": _digits_only(first_value(record, BEJERMAN_CUIT_FIELDS)),
    }


def _unique_by_code(records: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: dict[str, dict[str, str]] = {}
    for record in records:
        code = _clean_upper(record.get("code"))
        if code and code not in seen:
            seen[code] = record
    return list(seen.values())


def _choose_by_code(existing: dict[str, dict[str, str]], record: dict[str, str]) -> None:
    code = _clean_upper(record.get("code"))
    if code and code not in existing:
        existing[code] = record


def _write_report(path: str, rows: list[dict[str, Any]]) -> str:
    out_path = Path(path)
    if out_path.is_dir():
        out_path = out_path / f"bejerman_customer_cuit_backfill_{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "status",
        "reason",
        "customer_id",
        "razon_social",
        "cuit",
        "cuit_bejerman",
        "cod_empresa_actual",
        "cod_empresa_bejerman",
        "razon_social_bejerman",
        "locales_con_mismo_cuit",
        "bejerman_con_mismo_cuit",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
    return str(out_path)


class Command(BaseCommand):
    help = "Mapea customers.cod_empresa contra Bejerman por CUIT exacto y único."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Aplica los cambios. Sin este flag solo informa.")
        parser.add_argument(
            "--company-key",
            default="SEPID",
            help="Empresa Bejerman a consultar. Usar DEFAULT para tomar BEJERMAN_COMPANY.",
        )
        parser.add_argument("--output", default="", help="Ruta CSV opcional para guardar el reporte.")
        parser.add_argument(
            "--no-overwrite",
            action="store_true",
            help="No modifica clientes que ya tienen cod_empresa cargado.",
        )
        parser.add_argument(
            "--overwrite-code-by-cuit",
            action="store_true",
            help="Permite cambiar un cod_empresa no vacío cuando el CUIT apunta a otro código Bejerman.",
        )

    def handle(self, *args, **options):
        apply_changes = bool(options.get("apply"))
        no_overwrite = bool(options.get("no_overwrite"))
        overwrite_code_by_cuit = bool(options.get("overwrite_code_by_cuit"))
        company_key = _clean(options.get("company_key") or "")
        sdk_company_key = None if company_key.upper() in {"", "DEFAULT"} else company_key

        local_rows = _customer_rows()
        local_by_cuit: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in local_rows:
            cuit = _valid_cuit(row.get("cuit"))
            if cuit:
                local_by_cuit[cuit].append(row)

        try:
            client = BejermanSDKClient(company_key=sdk_company_key, allow_system_credentials=True)
            resolved_company_key = _resolved_company_key(client, company_key)
            bejerman_records = records_from_response(client.list_clientes())
        except BejermanSdkError as exc:
            raise CommandError(str(exc)) from exc

        bejerman_by_cuit: dict[str, list[dict[str, str]]] = defaultdict(list)
        bejerman_without_valid_cuit = 0
        bejerman_without_code = 0
        for record in bejerman_records:
            payload = _bejerman_payload(record)
            if not payload["cuit"]:
                bejerman_without_valid_cuit += 1
                continue
            if not payload["code"]:
                bejerman_without_code += 1
                continue
            bejerman_by_cuit[payload["cuit"]].append(payload)

        summary = {
            "mode": "apply" if apply_changes else "dry_run",
            "company_key": company_key or "DEFAULT",
            "local_total": len(local_rows),
            "bejerman_total": len(bejerman_records),
            "bejerman_without_valid_cuit": bejerman_without_valid_cuit,
            "bejerman_without_code": bejerman_without_code,
            "matched": 0,
            "updated": 0,
            "would_update": 0,
            "cuit_filled": 0,
            "cuit_would_fill": 0,
            "already_synced": 0,
            "no_overwrite": 0,
            "code_conflict_by_cuit": 0,
            "code_identity_mismatch": 0,
            "missing_local_cuit": 0,
            "invalid_local_cuit": 0,
            "code_not_found": 0,
            "bejerman_code_without_valid_cuit": 0,
            "not_found": 0,
            "ambiguous_bejerman_cuit": 0,
        }
        report_rows: list[dict[str, Any]] = []
        updates: list[dict[str, Any]] = []

        def add_report(row: dict[str, Any], status: str, reason: str, candidate: dict[str, str] | None = None) -> None:
            candidate = candidate or {}
            cuit = _valid_cuit(row.get("cuit"))
            matches = bejerman_by_cuit.get(cuit, []) if cuit else []
            report_rows.append(
                {
                    "status": status,
                    "reason": reason,
                    "customer_id": row.get("id"),
                    "razon_social": row.get("razon_social") or "",
                    "cuit": row.get("cuit") or "",
                    "cuit_bejerman": candidate.get("cuit", ""),
                    "cod_empresa_actual": row.get("cod_empresa") or "",
                    "cod_empresa_bejerman": candidate.get("code", ""),
                    "razon_social_bejerman": candidate.get("name", ""),
                    "locales_con_mismo_cuit": len(local_by_cuit.get(cuit, [])) if cuit else 0,
                    "bejerman_con_mismo_cuit": len(_unique_by_code(matches)) if matches else 0,
                }
            )

        bejerman_by_code: dict[str, dict[str, str]] = {}
        for matches in bejerman_by_cuit.values():
            for match in matches:
                _choose_by_code(bejerman_by_code, match)

        def fill_cuit_from_code(row: dict[str, Any], reason: str) -> bool:
            current_code = _clean_upper(row.get("cod_empresa"))
            if not current_code:
                return False
            candidate = bejerman_by_code.get(current_code)
            if not candidate:
                summary["code_not_found"] += 1
                add_report(row, "skipped", "El cod_empresa local no existe en Bejerman.")
                return True
            if not _name_matches(row.get("razon_social"), candidate.get("name")):
                summary["code_identity_mismatch"] += 1
                add_report(
                    row,
                    "skipped",
                    "El cod_empresa local existe en Bejerman, pero la razón social no coincide.",
                    candidate,
                )
                return True
            if not candidate.get("cuit"):
                summary["bejerman_code_without_valid_cuit"] += 1
                add_report(row, "skipped", "El cliente Bejerman no tiene CUIT válido.", candidate)
                return True
            if apply_changes:
                updates.append(
                    {
                        "customer_id": int(row["id"]),
                        "cuit": candidate["cuit"],
                        "bejerman_cod_empresa": candidate["code"],
                        "bejerman_company_key": resolved_company_key,
                    }
                )
                summary["cuit_filled"] += 1
                add_report(row, "cuit_filled", reason, candidate)
            else:
                summary["cuit_would_fill"] += 1
                add_report(row, "cuit_would_fill", reason, candidate)
            return True

        for row in local_rows:
            raw_cuit = _digits_only(row.get("cuit"))
            cuit = _valid_cuit(row.get("cuit"))
            if not raw_cuit:
                if fill_cuit_from_code(row, "CUIT completado desde Bejerman por cod_empresa exacto."):
                    continue
                summary["missing_local_cuit"] += 1
                add_report(row, "skipped", "Cliente sin CUIT local.")
                continue
            if not cuit:
                summary["invalid_local_cuit"] += 1
                if fill_cuit_from_code(row, "CUIT local inválido reemplazado desde Bejerman por cod_empresa exacto."):
                    continue
                add_report(row, "skipped", "CUIT local inválido para cruce fiscal.")
                continue

            matches = _unique_by_code(bejerman_by_cuit.get(cuit, []))
            if not matches:
                summary["not_found"] += 1
                add_report(row, "skipped", "No se encontró cliente Bejerman con ese CUIT.")
                continue
            if len(matches) > 1:
                summary["ambiguous_bejerman_cuit"] += 1
                add_report(row, "skipped", "El CUIT existe en más de un cliente Bejerman.")
                continue

            candidate = matches[0]
            summary["matched"] += 1
            current_code_raw = _clean(row.get("cod_empresa"))
            current_code = _clean_upper(current_code_raw)
            target_code = _clean_upper(candidate.get("code"))
            if current_code == target_code and not _name_matches(row.get("razon_social"), candidate.get("name")):
                summary["code_identity_mismatch"] += 1
                add_report(
                    row,
                    "skipped",
                    "El CUIT y el código coinciden, pero la razón social no coincide.",
                    candidate,
                )
                continue
            if current_code_raw == target_code:
                summary["already_synced"] += 1
                add_report(row, "already_synced", "El código Bejerman ya coincide.", candidate)
                continue
            if current_code and current_code != target_code and not overwrite_code_by_cuit:
                summary["code_conflict_by_cuit"] += 1
                add_report(
                    row,
                    "skipped",
                    "El CUIT coincide, pero cod_empresa local apunta a otro código Bejerman.",
                    candidate,
                )
                continue
            if current_code and current_code != target_code and no_overwrite:
                summary["no_overwrite"] += 1
                add_report(row, "skipped", "Tiene cod_empresa local y se ejecutó con --no-overwrite.", candidate)
                continue

            if apply_changes:
                updates.append(
                    {
                        "customer_id": int(row["id"]),
                        "cod_empresa": target_code,
                        "bejerman_cod_empresa": target_code,
                        "bejerman_company_key": resolved_company_key,
                    }
                )
                summary["updated"] += 1
                add_report(row, "updated", "cod_empresa actualizado por CUIT exacto.", candidate)
            else:
                summary["would_update"] += 1
                add_report(row, "would_update", "Se actualizaría cod_empresa por CUIT exacto.", candidate)

        if apply_changes and updates:
            columns = _table_columns("customers")
            with transaction.atomic():
                with connection.cursor() as cur:
                    for update in updates:
                        sets = []
                        params: list[Any] = []
                        if "cod_empresa" in update:
                            sets.append("cod_empresa = %s")
                            params.append(update["cod_empresa"])
                        if "cuit" in update:
                            sets.append("cuit = %s")
                            params.append(update["cuit"])
                        if "bejerman_cod_empresa" in update and "bejerman_cod_empresa" in columns:
                            sets.append("bejerman_cod_empresa = %s")
                            params.append(update["bejerman_cod_empresa"])
                        if "bejerman_company_key" in update and "bejerman_company_key" in columns:
                            sets.append("bejerman_company_key = %s")
                            params.append(update["bejerman_company_key"])
                        if not sets:
                            continue
                        params.append(update["customer_id"])
                        cur.execute(f"UPDATE customers SET {', '.join(sets)} WHERE id = %s", params)

        output_path = _clean(options.get("output"))
        if output_path:
            written_path = _write_report(output_path, report_rows)
            self.stdout.write(f"Reporte CSV: {written_path}")

        self.stdout.write(
            "BEJERMAN_CUSTOMER_CUIT_BACKFILL "
            f"mode={summary['mode']} "
            f"company_key={summary['company_key']} "
            f"local_total={summary['local_total']} "
            f"bejerman_total={summary['bejerman_total']} "
            f"matched={summary['matched']} "
            f"updated={summary['updated']} "
            f"would_update={summary['would_update']} "
            f"cuit_filled={summary['cuit_filled']} "
            f"cuit_would_fill={summary['cuit_would_fill']} "
            f"already_synced={summary['already_synced']} "
            f"no_overwrite={summary['no_overwrite']} "
            f"code_conflict_by_cuit={summary['code_conflict_by_cuit']} "
            f"code_identity_mismatch={summary['code_identity_mismatch']} "
            f"missing_local_cuit={summary['missing_local_cuit']} "
            f"invalid_local_cuit={summary['invalid_local_cuit']} "
            f"code_not_found={summary['code_not_found']} "
            f"bejerman_code_without_valid_cuit={summary['bejerman_code_without_valid_cuit']} "
            f"not_found={summary['not_found']} "
            f"ambiguous_bejerman_cuit={summary['ambiguous_bejerman_cuit']} "
            f"bejerman_without_valid_cuit={summary['bejerman_without_valid_cuit']} "
            f"bejerman_without_code={summary['bejerman_without_code']}"
        )
        if not apply_changes and (summary["would_update"] or summary["cuit_would_fill"]):
            self.stdout.write("Ejecutar nuevamente con --apply para aplicar los cambios informados.")
