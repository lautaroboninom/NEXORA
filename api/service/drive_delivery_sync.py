from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import jwt
import requests
from django.db import connection
from django.utils import timezone

from .delivery_orders import load_items_by_order, serialize_order


DEFAULT_START_DATE = date(2026, 6, 23)
DEFAULT_SHEET_NAME = "RECEPCI\u00d3N"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SHEETS_BASE_URL = "https://sheets.googleapis.com/v4/spreadsheets"
TEST_TOKENS = ("demo123", "demo456", "prueba123", "asd123")
TEST_DUMMY_CHARS = frozenset("asd")
DELIVERED_STATUSES = {
    "entregado_pendiente_facturacion",
    "entregado_no_facturable",
    "facturado",
}
SELLER_LABELS = {
    "ADM": "Administración",
    "EZE": "Ezequiel",
    "MAX": "Maximiliano",
    "MER": "Mercado Libre",
    "TOM": "Tomas",
}


class DriveDeliverySyncError(Exception):
    def __init__(self, code: str, detail: str, *, status_code: int = 400):
        super().__init__(detail)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class DriveSyncConfig:
    sheet_id: str
    sheet_name: str
    service_account: dict[str, Any]


@dataclass(frozen=True)
class AppsScriptSyncConfig:
    webapp_url: str
    secret: str
    sheet_id: str
    sheet_name: str


class GoogleSheetsClient:
    def __init__(self, config: DriveSyncConfig):
        self.config = config
        self._access_token: str | None = None
        self._token_expires_at = 0.0

    def get_values(self, range_name: str) -> list[list[Any]]:
        response = requests.get(
            self._values_url(range_name),
            headers=self._headers(),
            timeout=30,
        )
        self._raise_for_response(response)
        payload = response.json()
        return payload.get("values") or []

    def update_values(self, range_name: str, values: list[list[Any]]) -> dict[str, Any]:
        response = requests.put(
            f"{self._values_url(range_name)}?valueInputOption=USER_ENTERED",
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"values": values},
            timeout=30,
        )
        self._raise_for_response(response)
        return response.json()

    def _values_url(self, range_name: str) -> str:
        sheet_id = quote(self.config.sheet_id, safe="")
        encoded_range = quote(range_name, safe="")
        return f"{SHEETS_BASE_URL}/{sheet_id}/values/{encoded_range}"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}"}

    def _token(self) -> str:
        now = time.time()
        if self._access_token and now < self._token_expires_at - 60:
            return self._access_token

        account = self.config.service_account
        private_key = str(account.get("private_key") or "").replace("\\n", "\n")
        client_email = str(account.get("client_email") or "").strip()
        if not private_key or not client_email:
            raise DriveDeliverySyncError(
                "DRIVE_SYNC_CREDENTIALS_INVALID",
                "Las credenciales de Google no tienen private_key/client_email.",
                status_code=503,
            )

        issued_at = int(now)
        assertion = jwt.encode(
            {
                "iss": client_email,
                "scope": SHEETS_SCOPE,
                "aud": TOKEN_URL,
                "iat": issued_at,
                "exp": issued_at + 3600,
            },
            private_key,
            algorithm="RS256",
        )
        response = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
            timeout=30,
        )
        self._raise_for_response(response)
        payload = response.json()
        self._access_token = payload.get("access_token")
        self._token_expires_at = now + int(payload.get("expires_in") or 3600)
        if not self._access_token:
            raise DriveDeliverySyncError(
                "DRIVE_SYNC_TOKEN_FAILED",
                "Google no devolvio access_token para Sheets.",
                status_code=503,
            )
        return self._access_token

    @staticmethod
    def _raise_for_response(response):
        if response.ok:
            return
        detail = ""
        try:
            detail = response.json().get("error", {}).get("message") or response.text
        except Exception:
            detail = response.text
        raise DriveDeliverySyncError(
            "DRIVE_SYNC_GOOGLE_ERROR",
            f"Google Sheets rechazo la sincronizacion: {detail or response.status_code}",
            status_code=502,
        )


def load_drive_sync_config() -> DriveSyncConfig:
    sheet_id = (os.getenv("DRIVE_DELIVERY_SYNC_SHEET_ID") or "").strip()
    if not sheet_id:
        raise DriveDeliverySyncError(
            "DRIVE_SYNC_CONFIG_MISSING",
            "Falta configurar DRIVE_DELIVERY_SYNC_SHEET_ID.",
            status_code=503,
        )

    raw_json = (os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
    credentials_path = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    if raw_json:
        try:
            service_account = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise DriveDeliverySyncError(
                "DRIVE_SYNC_CREDENTIALS_INVALID",
                "GOOGLE_SERVICE_ACCOUNT_JSON no es JSON valido.",
                status_code=503,
            ) from exc
    elif credentials_path:
        try:
            service_account = json.loads(Path(credentials_path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise DriveDeliverySyncError(
                "DRIVE_SYNC_CREDENTIALS_INVALID",
                f"No se pudo leer GOOGLE_APPLICATION_CREDENTIALS: {exc}",
                status_code=503,
            ) from exc
        except json.JSONDecodeError as exc:
            raise DriveDeliverySyncError(
                "DRIVE_SYNC_CREDENTIALS_INVALID",
                "GOOGLE_APPLICATION_CREDENTIALS no contiene JSON valido.",
                status_code=503,
            ) from exc
    else:
        raise DriveDeliverySyncError(
            "DRIVE_SYNC_CREDENTIALS_MISSING",
            "Falta configurar GOOGLE_SERVICE_ACCOUNT_JSON o GOOGLE_APPLICATION_CREDENTIALS.",
            status_code=503,
        )

    return DriveSyncConfig(
        sheet_id=sheet_id,
        sheet_name=(os.getenv("DRIVE_DELIVERY_SYNC_SHEET_NAME") or DEFAULT_SHEET_NAME).strip() or DEFAULT_SHEET_NAME,
        service_account=service_account,
    )


def load_apps_script_sync_config() -> AppsScriptSyncConfig | None:
    webapp_url = (os.getenv("DRIVE_DELIVERY_SYNC_WEBAPP_URL") or "").strip()
    if not webapp_url:
        return None

    secret = (os.getenv("DRIVE_DELIVERY_SYNC_SECRET") or "").strip()
    if not secret:
        raise DriveDeliverySyncError(
            "DRIVE_SYNC_APPS_SCRIPT_CONFIG_MISSING",
            "Falta configurar DRIVE_DELIVERY_SYNC_SECRET.",
            status_code=503,
        )

    return AppsScriptSyncConfig(
        webapp_url=webapp_url,
        secret=secret,
        sheet_id=(os.getenv("DRIVE_DELIVERY_SYNC_SHEET_ID") or "").strip(),
        sheet_name=(os.getenv("DRIVE_DELIVERY_SYNC_SHEET_NAME") or DEFAULT_SHEET_NAME).strip() or DEFAULT_SHEET_NAME,
    )


def sync_delivery_orders_to_drive(
    *,
    sheets_client: Any | None = None,
    today: date | None = None,
    start_date: date = DEFAULT_START_DATE,
) -> dict[str, Any]:
    if sheets_client is None:
        apps_script_config = load_apps_script_sync_config()
        if apps_script_config is not None:
            return _sync_delivery_orders_via_apps_script(
                apps_script_config,
                today=today,
                start_date=start_date,
            )

    config = load_drive_sync_config() if sheets_client is None else None
    client = sheets_client or GoogleSheetsClient(config)
    sheet_name = getattr(getattr(client, "config", None), "sheet_name", DEFAULT_SHEET_NAME)
    sheet_id = getattr(getattr(client, "config", None), "sheet_id", "")
    today = today or timezone.localdate()

    orders = _load_orders_for_sync(start_date, today)
    existing_rows = client.get_values(f"{sheet_name}!A:I")
    existing_keys = _existing_drive_keys(existing_rows)

    rows_to_write: list[list[Any]] = []
    already_in_drive = 0
    excluded_cancelled = 0
    excluded_test = 0
    eligible_rows = 0
    for order in orders:
        status = str(order.get("status") or "").strip().lower()
        if status == "cancelado":
            excluded_cancelled += 1
            continue
        if _is_test_order(order):
            excluded_test += 1
            continue
        drive_row = delivery_order_to_drive_row(order)
        key = _drive_row_key(drive_row)
        if key in existing_keys:
            already_in_drive += 1
            continue
        eligible_rows += 1
        existing_keys.add(key)
        rows_to_write.append(drive_row)

    start_row = _next_free_row(existing_rows)
    end_row = start_row + len(rows_to_write) - 1 if rows_to_write else None
    written_range = f"A{start_row}:I{end_row}" if end_row else ""
    if rows_to_write:
        client.update_values(f"{sheet_name}!{written_range}", rows_to_write)

    return {
        "ok": True,
        "sheetId": sheet_id,
        "sheetName": sheet_name,
        "dateFrom": start_date.isoformat(),
        "dateTo": today.isoformat(),
        "range": written_range,
        "startRow": start_row if rows_to_write else None,
        "endRow": end_row,
        "createdRows": len(rows_to_write),
        "appendedRows": len(rows_to_write),
        "alreadyInDrive": already_in_drive,
        "skippedExisting": already_in_drive,
        "excludedCancelled": excluded_cancelled,
        "excludedTest": excluded_test,
        "eligibleRows": eligible_rows,
        "consideredRows": len(orders),
    }


def _sync_delivery_orders_via_apps_script(
    config: AppsScriptSyncConfig,
    *,
    today: date | None,
    start_date: date,
) -> dict[str, Any]:
    today = today or timezone.localdate()
    orders = _load_orders_for_sync(start_date, today)

    rows_to_send: list[list[Any]] = []
    excluded_cancelled = 0
    excluded_test = 0
    for order in orders:
        status = str(order.get("status") or "").strip().lower()
        if status == "cancelado":
            excluded_cancelled += 1
            continue
        if _is_test_order(order):
            excluded_test += 1
            continue
        rows_to_send.append(delivery_order_to_drive_row(order))

    try:
        response = requests.post(
            config.webapp_url,
            json={"secret": config.secret, "rows": rows_to_send},
            timeout=60,
        )
    except requests.RequestException as exc:
        raise DriveDeliverySyncError(
            "DRIVE_SYNC_APPS_SCRIPT_ERROR",
            f"No se pudo conectar con Apps Script: {exc}",
            status_code=502,
        ) from exc

    payload = _response_json(response)
    if not response.ok or not payload.get("ok"):
        detail = (
            payload.get("error")
            or payload.get("detail")
            or payload.get("message")
            or response.text[:300]
            or response.status_code
        )
        raise DriveDeliverySyncError(
            "DRIVE_SYNC_APPS_SCRIPT_ERROR",
            f"Apps Script rechazó la sincronización: {detail}",
            status_code=502,
        )

    created_rows = _payload_int(payload, "createdRows", "appendedRows")
    already_in_drive = _payload_int(payload, "alreadyInDrive", "skippedExisting")

    return {
        "ok": True,
        "backend": "apps_script",
        "sheetId": payload.get("sheetId") or config.sheet_id,
        "sheetName": payload.get("sheetName") or config.sheet_name,
        "dateFrom": start_date.isoformat(),
        "dateTo": today.isoformat(),
        "range": payload.get("range") or "",
        "startRow": payload.get("startRow"),
        "endRow": payload.get("endRow"),
        "createdRows": created_rows,
        "appendedRows": created_rows,
        "alreadyInDrive": already_in_drive,
        "skippedExisting": already_in_drive,
        "excludedCancelled": excluded_cancelled,
        "excludedTest": excluded_test,
        "eligibleRows": len(rows_to_send),
        "consideredRows": len(orders),
    }


def _response_json(response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise DriveDeliverySyncError(
            "DRIVE_SYNC_APPS_SCRIPT_ERROR",
            f"Apps Script devolvió una respuesta inválida: {response.text[:300]}",
            status_code=502,
        ) from exc
    if not isinstance(payload, dict):
        raise DriveDeliverySyncError(
            "DRIVE_SYNC_APPS_SCRIPT_ERROR",
            "Apps Script devolvió una respuesta inválida.",
            status_code=502,
        )
    return payload


def _payload_int(payload: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = payload.get(key)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return 0


def delivery_order_to_drive_row(order: dict[str, Any]) -> list[Any]:
    delivery_type = str(order.get("deliveryType") or "").strip()
    is_service = delivery_type == "service_release"
    return [
        _seller_label(order),
        _date_text(order.get("orderDate")),
        str(order.get("customerName") or "").strip(),
        "REPARACION" if is_service else _company_label(order),
        _pedido_text(order),
        order.get("status") in DELIVERED_STATUSES,
        "" if is_service else str(order.get("remitoNumber") or "").strip(),
        _service_reference(order) if is_service else "-",
        str(order.get("invoiceNumber") or "").strip(),
    ]


def _load_orders_for_sync(start_date: date, end_date: date) -> list[dict[str, Any]]:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM delivery_orders
            WHERE order_date >= %s
              AND order_date <= %s
            ORDER BY order_date ASC, source_row ASC NULLS LAST, created_at ASC, id ASC
            """,
            [start_date, end_date],
        )
        rows = _rows(cur)

    items_by_order = load_items_by_order([row["id"] for row in rows])
    out = []
    for row in rows:
        row["items"] = items_by_order.get(row["id"], [])
        out.append(serialize_order(row))
    return out


def _rows(cur) -> list[dict[str, Any]]:
    columns = [column[0] for column in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _date_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else text


def _seller_label(order: dict[str, Any]) -> str:
    code = str(order.get("sellerCode") or "").strip().upper()
    if order.get("deliveryType") == "service_release" and not code:
        return SELLER_LABELS["ADM"]
    if code in SELLER_LABELS:
        return SELLER_LABELS[code]
    name = str(order.get("sellerName") or "").strip()
    if not name:
        return SELLER_LABELS["ADM"]
    if len(name.split()) > 1 and name.split()[0].upper() in SELLER_LABELS:
        name = " ".join(name.split()[1:])
    return name.split()[0] if name else SELLER_LABELS["ADM"]


def _company_label(order: dict[str, Any]) -> str:
    marker = str(order.get("companyKey") or order.get("operationCompanyLabel") or "").strip().upper()
    normalized = re.sub(r"[\s_\-]+", "", marker)
    if normalized in {"MG", "MGB", "MGBIO", "MGBIOSA"} or "MGBIO" in normalized:
        return "MGBIO"
    return "SEPID"


def _pedido_text(order: dict[str, Any]) -> str:
    if order.get("deliveryType") == "service_release":
        parts = [
            str(order.get("equipmentModel") or "").strip(),
            str(order.get("equipmentSerial") or "").strip(),
            str(order.get("equipmentInternalNumber") or "").strip(),
        ]
        text = " ".join(part for part in parts if part)
        if text:
            return text

    raw = str(order.get("rawPedido") or "").strip()
    if raw:
        return raw

    item_labels = []
    for item in order.get("items") or []:
        label = str(
            item.get("sourceText")
            or item.get("description")
            or item.get("articleName")
            or item.get("articleCode")
            or ""
        ).strip()
        if label:
            quantity = item.get("quantity")
            prefix = "" if quantity in (None, "", 1, 1.0) else f"{quantity} x "
            item_labels.append(f"{prefix}{label}")
    if item_labels:
        return " | ".join(item_labels)
    return str(order.get("sourceReference") or order.get("orderNumber") or "").strip()


def _service_reference(order: dict[str, Any]) -> str:
    source_reference = str(order.get("sourceReference") or "").strip()
    if source_reference:
        return _normalize_os_reference(source_reference)
    ingreso_id = order.get("ingresoId")
    if ingreso_id:
        return _normalize_os_reference(str(ingreso_id))
    return "-"


def _normalize_os_reference(value: Any) -> str:
    text = str(value or "").strip()
    digits = "".join(re.findall(r"\d+", text))
    if digits:
        return f"OS-{int(digits):05d}"
    return text or "-"


def _is_test_order(order: dict[str, Any]) -> bool:
    values: list[str] = [
        order.get("customerName"),
        order.get("orderNumber"),
        order.get("equipmentSerial"),
        order.get("equipmentInternalNumber"),
        order.get("rawPedido"),
        order.get("sourceReference"),
    ]
    for item in order.get("items") or []:
        values.extend(
            [
                item.get("partida"),
                item.get("sourceText"),
                item.get("description"),
                item.get("articleName"),
                item.get("articleCode"),
            ]
        )
        for partida in item.get("partidas") or []:
            values.append(partida.get("partida"))
    haystack = " ".join(str(value or "").casefold() for value in values)
    if any(token in haystack for token in TEST_TOKENS):
        return True
    return any(_is_dummy_test_text(value) for value in values)


def _is_dummy_test_text(value: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "", str(value or "").strip().casefold())
    if len(normalized) < 3:
        return False
    return set(normalized).issubset(TEST_DUMMY_CHARS)


def _existing_drive_keys(rows: list[list[Any]]) -> set[str]:
    keys = set()
    for row in rows[1:]:
        key = _drive_row_key(_pad_row(row))
        if key:
            keys.add(key)
    return keys


def _drive_row_key(row: list[Any]) -> str:
    padded = _pad_row(row)
    os_reference = str(padded[7] or "").strip()
    if os_reference and os_reference != "-":
        return f"os:{_normalize_os_reference(os_reference).casefold()}"
    parts = [_norm_key(padded[index]) for index in (1, 2, 3, 4)]
    if not any(parts):
        return ""
    return "row:" + "|".join(parts)


def _pad_row(row: list[Any]) -> list[Any]:
    padded = list(row or [])
    while len(padded) < 9:
        padded.append("")
    return padded[:9]


def _norm_key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def _next_free_row(rows: list[list[Any]]) -> int:
    last_used = 1
    for index, row in enumerate(rows, start=1):
        padded = _pad_row(row)
        business_values = [padded[i] for i in (0, 1, 2, 3, 4, 6, 7, 8)]
        if any(str(value or "").strip() for value in business_values):
            last_used = index
    return last_used + 1
