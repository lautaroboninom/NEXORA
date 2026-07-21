from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import close_old_connections, connection
from django.utils import timezone

from .bejerman_companies import company_for_key
from .bejerman_sdk import (
    BejermanPdfPendingError,
    BejermanPdfReference,
    BejermanSDKClient,
    BejermanSdkConfigError,
    BejermanSdkResponseError,
    BejermanSdkUnavailable,
    comprobante_pdf_output_path,
    extract_pdf_bytes,
    normalize_date,
    normalize_pdf_bytes,
    probe_comprobante_pdf_copy,
    store_comprobante_pdf_copy,
)


logger = logging.getLogger(__name__)

TABLE_NAME = "bejerman_pdf_requests"
GLOBAL_PDF_LOCK_NAME = "nexora:bejerman:pdf-generation"
PROCESSABLE_STATES = (
    "queued",
    "requesting",
    "waiting_sdk",
    "waiting_folder",
    "ready_direct",
)


class BejermanPdfRequestPending(BejermanPdfPendingError):
    code = "BEJERMAN_PDF_PENDING"
    status_code = 202

    def __init__(
        self,
        message: str,
        *,
        pdf_state: str,
        generation_requested: bool,
        retry_after_ms: int,
    ):
        super().__init__(message, retry_after_ms=retry_after_ms)
        self.pdf_state = pdf_state
        self.generation_requested = generation_requested


class BejermanPdfNotPublishedError(BejermanSdkResponseError):
    code = "BEJERMAN_PDF_NOT_PUBLISHED"
    status_code = 504
    pdf_state = "failed"
    generation_requested = True


class BejermanPdfFolderUnavailableError(BejermanSdkUnavailable):
    code = "BEJERMAN_PDF_FOLDER_UNAVAILABLE"
    status_code = 503
    pdf_state = "folder_error"

    def __init__(self, message: str, *, generation_requested: bool):
        super().__init__(message)
        self.generation_requested = generation_requested


def pdf_error_metadata(exc: Exception) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    pdf_state = str(getattr(exc, "pdf_state", "") or "").strip()
    if pdf_state:
        payload["pdf_state"] = pdf_state
    if hasattr(exc, "generation_requested"):
        payload["generation_requested"] = bool(getattr(exc, "generation_requested"))
    return payload


def _positive_int_setting(name: str, default: int) -> int:
    try:
        value = int(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _digits(value: Any, width: int) -> str:
    digits = re.sub(r"\D+", "", _clean(value))
    return digits.zfill(width) if digits else ""


def _company_key(client: BejermanSDKClient) -> str:
    marker = _clean(getattr(client, "company_key", "")) or _clean(getattr(client, "company", ""))
    company = company_for_key(marker, default="SEPID")
    return company.key if company else marker.upper()


def document_key_for(client: BejermanSDKClient, reference: BejermanPdfReference) -> str:
    issue_date = normalize_date(reference.issue_date) or _clean(reference.issue_date)
    parts = (
        _company_key(client),
        _clean(reference.type).upper(),
        _clean(reference.letter).upper(),
        _digits(reference.point_of_sale, 5),
        _digits(reference.number, 8),
        issue_date,
    )
    if not all(parts):
        raise BejermanSdkResponseError("La referencia del PDF de Bejerman está incompleta")
    return "|".join(parts)


def _reference_values(client: BejermanSDKClient, reference: BejermanPdfReference) -> dict[str, str]:
    issue_date = normalize_date(reference.issue_date) or ""
    values = {
        "document_key": document_key_for(client, reference),
        "company_key": _company_key(client),
        "customer_code": _clean(reference.customer_code),
        "tipo": _clean(reference.type).upper(),
        "letra": _clean(reference.letter).upper(),
        "pto_venta": _digits(reference.point_of_sale, 5),
        "numero": _digits(reference.number, 8),
        "issue_date": issue_date,
    }
    if not issue_date:
        raise BejermanSdkResponseError("La fecha del comprobante PDF de Bejerman es inválida")
    return values


def _row_from_cursor(cur) -> dict[str, Any] | None:
    raw = cur.fetchone()
    if raw is None:
        return None
    columns = [column[0] for column in cur.description]
    return dict(zip(columns, raw))


def _transition_log_context(
    row: dict[str, Any],
    *,
    previous_state: str,
    next_state: str,
    started_at: float,
) -> dict[str, Any]:
    comprobante = (
        f"{_clean(row.get('tipo'))} {_clean(row.get('letra'))} "
        f"{_clean(row.get('pto_venta'))}-{_clean(row.get('numero'))}"
    ).strip()
    return {
        "company_key": _clean(row.get("company_key")),
        "comprobante": comprobante,
        "remito_group_id": _clean(row.get("remito_group_id")),
        "pdf_previous_state": previous_state,
        "pdf_state": next_state,
        "duration_ms": int((time.monotonic() - started_at) * 1000),
    }


def _load_request(document_key: str, *, for_update: bool = False) -> dict[str, Any] | None:
    suffix = " FOR UPDATE" if for_update else ""
    with connection.cursor() as cur:
        cur.execute(f"SELECT * FROM {TABLE_NAME} WHERE document_key = %s{suffix}", [document_key])
        return _row_from_cursor(cur)


def _mark_ready_folder(document_key: str, remote_path: str = "") -> None:
    with connection.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {TABLE_NAME}
               SET state = 'ready_folder',
                   ready_at = CURRENT_TIMESTAMP,
                   remote_path = COALESCE(NULLIF(%s, ''), remote_path),
                   pdf_payload = NULL,
                   payload_sha256 = NULL,
                   lease_expires_at = NULL,
                   last_error_code = NULL,
                   last_error_detail = NULL
             WHERE document_key = %s
            """,
            [remote_path, document_key],
        )


def _pending_for(row: dict[str, Any]) -> BejermanPdfRequestPending:
    state = _clean(row.get("state")) or "queued"
    generation_requested = int(row.get("generation_attempt_count") or 0) > 0
    retry_after_ms = 2500 if state in {"queued", "requesting"} else 5000
    if state == "queued":
        detail = "El PDF oficial quedó en cola para solicitarse una sola vez a Bejerman."
    elif state == "requesting":
        detail = "Bejerman está procesando la única solicitud de generación del PDF oficial."
    elif state == "waiting_sdk":
        detail = (
            "NEXORA está consultando el PDF oficial en Bejerman para guardarlo "
            "directamente en la carpeta configurada."
        )
    else:
        detail = "La generación ya fue solicitada. NEXORA está esperando el archivo oficial en la carpeta de Bejerman."
    return BejermanPdfRequestPending(
        detail,
        pdf_state=state,
        generation_requested=generation_requested,
        retry_after_ms=retry_after_ms,
    )


def _not_published_for(row: dict[str, Any]) -> BejermanPdfNotPublishedError:
    detail = _clean(row.get("last_error_detail"))
    if not detail:
        detail = (
            "Bejerman no devolvió el PDF oficial dentro de los 10 minutos posteriores a la solicitud. "
            "Volver a comprobar no volverá a generarlo."
        )
    error = BejermanPdfNotPublishedError(detail)
    error.generation_requested = int(row.get("generation_attempt_count") or 0) > 0
    return error


def fetch_or_enqueue_comprobante_pdf(
    client: BejermanSDKClient,
    reference: BejermanPdfReference,
    *,
    actor_user_id: int | None = None,
    remito_group_id: str | None = None,
) -> tuple[bytes, str]:
    """Devuelve el PDF oficial o encola una única generación fuera del request HTTP."""

    started_at = time.monotonic()
    values = _reference_values(client, reference)
    document_key = values["document_key"]
    remote_path = comprobante_pdf_output_path(client, reference)
    row = _load_request(document_key)
    if row is None:
        # Sólo el primer acceso hace una lectura directa. Una vez encolado, el
        # worker centraliza los chequeos para que el polling del navegador no
        # se transforme en otra tormenta SFTP.
        folder_pdf, folder_status = probe_comprobante_pdf_copy(client, reference, interactive=True)
        if folder_pdf:
            return folder_pdf, "application/pdf"
        if folder_status == "error":
            raise BejermanPdfFolderUnavailableError(
                "No se pudo comprobar la carpeta configurada de PDFs de Bejerman; no se solicitó una generación nueva.",
                generation_requested=False,
            )
        deadline_seconds = _positive_int_setting("BEJERMAN_PDF_WAIT_DEADLINE_SECONDS", 600)
        with connection.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {TABLE_NAME} (
                  document_key, remito_group_id, actor_user_id, company_key, customer_code,
                  tipo, letra, pto_venta, numero, issue_date, state,
                  deadline_at, next_folder_check_at, remote_path
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::date, 'queued',
                        CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'), CURRENT_TIMESTAMP, %s)
                ON CONFLICT (document_key) DO UPDATE
                   SET remito_group_id = COALESCE({TABLE_NAME}.remito_group_id, EXCLUDED.remito_group_id),
                       actor_user_id = CASE
                           WHEN {TABLE_NAME}.state = 'queued'
                            AND {TABLE_NAME}.generation_attempt_count = 0
                           THEN COALESCE(EXCLUDED.actor_user_id, {TABLE_NAME}.actor_user_id)
                           ELSE {TABLE_NAME}.actor_user_id
                       END,
                       customer_code = COALESCE(NULLIF({TABLE_NAME}.customer_code, ''), EXCLUDED.customer_code),
                       remote_path = COALESCE(NULLIF({TABLE_NAME}.remote_path, ''), EXCLUDED.remote_path)
                RETURNING *
                """,
                [
                    document_key,
                    _clean(remito_group_id) or None,
                    actor_user_id or getattr(client, "actor_user_id", None),
                    values["company_key"],
                    values["customer_code"],
                    values["tipo"],
                    values["letra"],
                    values["pto_venta"],
                    values["numero"],
                    values["issue_date"],
                    deadline_seconds,
                    remote_path,
                ],
            )
            row = _row_from_cursor(cur) or {}
        logger.info(
            "bejerman_pdf_request_transition",
            extra=_transition_log_context(
                row,
                previous_state="none",
                next_state=_clean(row.get("state")) or "queued",
                started_at=started_at,
            ),
        )

    state = _clean(row.get("state"))
    raw_payload = row.get("pdf_payload")
    if state == "ready_direct" and raw_payload is not None:
        payload = bytes(raw_payload)
        pdf = normalize_pdf_bytes(payload)
        expected_checksum = _clean(row.get("payload_sha256"))
        checksum_matches = not expected_checksum or hashlib.sha256(pdf or b"").hexdigest() == expected_checksum
        if pdf and checksum_matches:
            return pdf, "application/pdf"
        with connection.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {TABLE_NAME}
                   SET state='failed', pdf_payload=NULL, payload_sha256=NULL,
                       last_error_code='BEJERMAN_PDF_INVALID',
                        last_error_detail='El PDF temporal de Bejerman es inválido o no coincide con su huella.'
                 WHERE document_key=%s
                """,
                [document_key],
            )
        row = {
            **row,
            "state": "failed",
            "last_error_detail": "El PDF temporal de Bejerman es inválido o no coincide con su huella.",
        }
        raise _not_published_for(row)

    if state in {"ready_folder", "failed"}:
        # Estados terminales admiten una comprobación manual tardía. Esto
        # permite recuperar un archivo que Bejerman publique después del
        # deadline sin volver a invocar SOAP.
        folder_pdf, folder_status = probe_comprobante_pdf_copy(client, reference, interactive=True)
        if folder_pdf:
            _mark_ready_folder(document_key, remote_path)
            return folder_pdf, "application/pdf"
        if folder_status == "error":
            raise BejermanPdfFolderUnavailableError(
                "No se pudo comprobar la carpeta configurada de PDFs de Bejerman.",
                generation_requested=int(row.get("generation_attempt_count") or 0) > 0,
            )

    if state == "ready_folder":
        with connection.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {TABLE_NAME}
                   SET state='waiting_folder',
                       deadline_at=CURRENT_TIMESTAMP + INTERVAL '10 minutes',
                       next_folder_check_at=CURRENT_TIMESTAMP,
                       last_error_code='BEJERMAN_PDF_FOLDER_MISSING',
                       last_error_detail='El PDF marcado como listo ya no se encontró en la carpeta configurada.'
                 WHERE document_key=%s
                RETURNING *
                """,
                [document_key],
            )
            row = _row_from_cursor(cur) or row
        state = "waiting_folder"

    deadline = row.get("deadline_at")
    if state in {"requesting", "waiting_sdk"} and deadline and deadline <= timezone.now():
        # La consulta HTTP no debe ejecutar SOAP ni cerrar el caso mientras el
        # worker puede hacer una última consulta oficial de forma serializada.
        with connection.cursor() as cur:
            cur.execute(
                f"UPDATE {TABLE_NAME} SET next_folder_check_at=CURRENT_TIMESTAMP WHERE document_key=%s",
                [document_key],
            )
        raise _pending_for(row)

    if state == "waiting_folder" and deadline and deadline <= timezone.now():
        # Antes de declarar que Bejerman no publicó el archivo, confirmar una
        # última vez la fuente durable. Un error de acceso no equivale a un
        # archivo ausente y no debe convertirse en un falso NOT_PUBLISHED.
        folder_pdf, folder_status = probe_comprobante_pdf_copy(client, reference, interactive=True)
        if folder_pdf:
            _mark_ready_folder(document_key, remote_path)
            return folder_pdf, "application/pdf"
        if folder_status == "error":
            raise BejermanPdfFolderUnavailableError(
                "No se pudo comprobar la carpeta configurada de PDFs de Bejerman.",
                generation_requested=int(row.get("generation_attempt_count") or 0) > 0,
            )
        with connection.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {TABLE_NAME}
                   SET state='failed', lease_expires_at=NULL,
                       last_error_code='BEJERMAN_PDF_NOT_PUBLISHED',
                       last_error_detail=%s
                 WHERE document_key=%s
                RETURNING *
                """,
                [
                    "Bejerman no publicó el PDF oficial dentro de los 10 minutos posteriores a la solicitud.",
                    document_key,
                ],
            )
            row = _row_from_cursor(cur) or row
        raise _not_published_for(row)

    if state == "failed":
        raise _not_published_for(row)
    raise _pending_for(row)


def _reference_from_row(row: dict[str, Any]) -> BejermanPdfReference:
    return BejermanPdfReference(
        type=_clean(row.get("tipo")),
        number=_clean(row.get("numero")),
        letter=_clean(row.get("letra")),
        point_of_sale=_clean(row.get("pto_venta")),
        issue_date=str(row.get("issue_date") or ""),
        customer_code=_clean(row.get("customer_code")),
    )


def _client_from_row(row: dict[str, Any]) -> BejermanSDKClient:
    return BejermanSDKClient(
        company_key=_clean(row.get("company_key")),
        actor_user_id=row.get("actor_user_id"),
    )


def _try_global_lock() -> bool:
    with connection.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", [GLOBAL_PDF_LOCK_NAME])
        result = cur.fetchone()
    return bool(result and result[0])


def _release_global_lock() -> None:
    with connection.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", [GLOBAL_PDF_LOCK_NAME])


def _set_next_check(document_key: str, seconds: int) -> None:
    with connection.cursor() as cur:
        cur.execute(
            f"UPDATE {TABLE_NAME} SET next_folder_check_at=CURRENT_TIMESTAMP + (%s * INTERVAL '1 second') WHERE document_key=%s",
            [seconds, document_key],
        )


def _claim_due_request(document_key: str) -> dict[str, Any] | None:
    """Reserva brevemente un chequeo para evitar trabajo duplicado entre workers."""

    with connection.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {TABLE_NAME}
               SET next_folder_check_at=CURRENT_TIMESTAMP + INTERVAL '2 minutes'
             WHERE document_key=%s
               AND state = ANY(%s)
               AND next_folder_check_at <= CURRENT_TIMESTAMP
            RETURNING *
            """,
            [document_key, list(PROCESSABLE_STATES)],
        )
        return _row_from_cursor(cur)


def _mark_failed(document_key: str, code: str, detail: str) -> None:
    with connection.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {TABLE_NAME}
               SET state='failed', lease_expires_at=NULL, pdf_payload=NULL,
                   payload_sha256=NULL, last_error_code=%s, last_error_detail=%s
             WHERE document_key=%s
            """,
            [code, detail[:2000], document_key],
        )


def _set_waiting_sdk(document_key: str, error: Exception | None = None) -> None:
    error_detail = str(error)[:2000] if error else None
    error_code = "BEJERMAN_PDF_GENERATION_AMBIGUOUS" if error else None
    with connection.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {TABLE_NAME}
               SET state='waiting_sdk', lease_expires_at=NULL,
                   next_folder_check_at=CURRENT_TIMESTAMP,
                   last_error_code=%s, last_error_detail=%s
             WHERE document_key=%s
            """,
            [error_code, error_detail, document_key],
        )


def _store_direct_pdf(row: dict[str, Any], pdf: bytes) -> bool:
    document_key = row["document_key"]
    normalized = normalize_pdf_bytes(pdf)
    if not normalized:
        _mark_failed(
            document_key,
            "BEJERMAN_PDF_INVALID",
            "Bejerman devolvió un PDF inválido.",
        )
        return True
    checksum = hashlib.sha256(normalized).hexdigest()
    with connection.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {TABLE_NAME}
               SET state='ready_direct', ready_at=CURRENT_TIMESTAMP,
                   lease_expires_at=NULL, next_folder_check_at=CURRENT_TIMESTAMP,
                   pdf_payload=%s, payload_sha256=%s,
                   last_error_code=NULL, last_error_detail=NULL
             WHERE document_key=%s
            """,
            [normalized, checksum, document_key],
        )
    _process_direct_copy(_load_request(document_key) or row)
    return True


def _process_sdk_wait(row: dict[str, Any]) -> bool:
    """Consulta bytes oficiales y los guarda en el destino configurado."""

    document_key = row["document_key"]
    client = _client_from_row(row)
    reference = _reference_from_row(row)
    connect_timeout = _positive_int_setting("BEJERMAN_PDF_GENERATION_CONNECT_TIMEOUT", 10)
    read_timeout = _positive_int_setting("BEJERMAN_PDF_SDK_READ_TIMEOUT", 30)
    client.timeout = (connect_timeout, read_timeout)
    try:
        response = client.consultar_comprobante_ventas_pdf(reference)
        pdf = extract_pdf_bytes(response)
    except BejermanSdkConfigError as exc:
        _mark_failed(document_key, "BEJERMAN_PDF_PREFLIGHT_FAILED", str(exc))
        return True
    except (BejermanSdkResponseError, BejermanSdkUnavailable) as exc:
        pdf = None
        logger.info(
            "bejerman_pdf_request_sdk_pending",
            extra={"document_key": document_key, "error": str(exc)},
        )
    except Exception as exc:
        pdf = None
        logger.warning(
            "bejerman_pdf_request_sdk_pending",
            extra={"document_key": document_key, "error": str(exc)},
            exc_info=True,
        )

    if pdf:
        logger.info("bejerman_pdf_request_sdk_ready", extra={"document_key": document_key})
        return _store_direct_pdf(row, pdf)

    deadline = row.get("deadline_at")
    if deadline and deadline <= timezone.now():
        _mark_failed(
            document_key,
            "BEJERMAN_PDF_NOT_PUBLISHED",
            "Bejerman no devolvió el PDF oficial dentro de los 10 minutos posteriores a la solicitud.",
        )
        logger.warning("bejerman_pdf_request_not_available", extra={"document_key": document_key})
        return True
    _set_next_check(document_key, _positive_int_setting("BEJERMAN_PDF_SDK_CHECK_SECONDS", 5))
    return False


def _process_folder_wait(row: dict[str, Any]) -> bool:
    document_key = row["document_key"]
    client = _client_from_row(row)
    reference = _reference_from_row(row)
    pdf, folder_status = probe_comprobante_pdf_copy(client, reference, interactive=False)
    if pdf:
        _mark_ready_folder(document_key, comprobante_pdf_output_path(client, reference))
        logger.info("bejerman_pdf_request_ready_folder", extra={"document_key": document_key})
        return True
    if folder_status == "error":
        _set_next_check(document_key, _positive_int_setting("BEJERMAN_PDF_FOLDER_CHECK_SECONDS", 5))
        return False
    deadline = row.get("deadline_at")
    if deadline and deadline <= timezone.now():
        _mark_failed(
            document_key,
            "BEJERMAN_PDF_NOT_PUBLISHED",
            "Bejerman no publicó el PDF oficial dentro de los 10 minutos posteriores a la solicitud.",
        )
        logger.warning("bejerman_pdf_request_not_published", extra={"document_key": document_key})
        return True
    _set_next_check(document_key, _positive_int_setting("BEJERMAN_PDF_FOLDER_CHECK_SECONDS", 5))
    return False


def _process_direct_copy(row: dict[str, Any]) -> bool:
    document_key = row["document_key"]
    raw_payload = row.get("pdf_payload")
    pdf = normalize_pdf_bytes(bytes(raw_payload)) if raw_payload is not None else None
    expected_checksum = _clean(row.get("payload_sha256"))
    checksum_matches = not expected_checksum or hashlib.sha256(pdf or b"").hexdigest() == expected_checksum
    if not pdf or not checksum_matches:
        _mark_failed(
            document_key,
            "BEJERMAN_PDF_INVALID",
            "El PDF temporal de Bejerman es inválido o no coincide con su huella.",
        )
        return True
    client = _client_from_row(row)
    reference = _reference_from_row(row)
    if store_comprobante_pdf_copy(client, reference, pdf):
        _mark_ready_folder(document_key, comprobante_pdf_output_path(client, reference))
        logger.info("bejerman_pdf_request_copy_saved", extra={"document_key": document_key})
        return True
    _set_next_check(document_key, _positive_int_setting("BEJERMAN_PDF_COPY_RETRY_SECONDS", 10))
    return False


def _process_queued(row: dict[str, Any]) -> bool:
    document_key = row["document_key"]
    if not _try_global_lock():
        _set_next_check(document_key, 2)
        return False
    try:
        current = _load_request(document_key) or row
        if current.get("state") != "queued" or int(current.get("generation_attempt_count") or 0) != 0:
            return False
        client = _client_from_row(current)
        reference = _reference_from_row(current)

        folder_pdf, folder_status = probe_comprobante_pdf_copy(client, reference, interactive=False)
        if folder_pdf:
            _mark_ready_folder(document_key, comprobante_pdf_output_path(client, reference))
            return True
        if folder_status == "error":
            _set_next_check(document_key, _positive_int_setting("BEJERMAN_PDF_FOLDER_CHECK_SECONDS", 5))
            logger.warning(
                "bejerman_pdf_request_folder_unavailable",
                extra={"document_key": document_key},
            )
            return False

        connect_timeout = _positive_int_setting("BEJERMAN_PDF_GENERATION_CONNECT_TIMEOUT", 10)
        read_timeout = _positive_int_setting("BEJERMAN_PDF_GENERATION_READ_TIMEOUT", 90)
        client.timeout = (connect_timeout, read_timeout)
        try:
            client.register()
        except (BejermanSdkConfigError, BejermanSdkResponseError, BejermanSdkUnavailable) as exc:
            _mark_failed(document_key, "BEJERMAN_PDF_PREFLIGHT_FAILED", str(exc))
            logger.warning(
                "bejerman_pdf_request_preflight_failed",
                extra={"document_key": document_key, "error": str(exc)},
            )
            return True

        lease_seconds = read_timeout + 30
        deadline_seconds = _positive_int_setting("BEJERMAN_PDF_WAIT_DEADLINE_SECONDS", 600)
        with connection.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {TABLE_NAME}
                   SET state='requesting', generation_attempt_count=1,
                       generation_requested_at=CURRENT_TIMESTAMP,
                       lease_expires_at=CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                       deadline_at=CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                       next_folder_check_at=CURRENT_TIMESTAMP,
                       last_error_code=NULL, last_error_detail=NULL
                 WHERE document_key=%s AND state='queued' AND generation_attempt_count=0
                RETURNING document_key
                """,
                [lease_seconds, deadline_seconds, document_key],
            )
            claimed = cur.fetchone()
        if not claimed:
            return False

        logger.info(
            "bejerman_pdf_request_generation_started",
            extra={
                "document_key": document_key,
                "company_key": current.get("company_key"),
                "remito_group_id": current.get("remito_group_id"),
            },
        )
        try:
            response = client.generar_comprobante_ventas_pdf(reference)
            pdf = extract_pdf_bytes(response)
        except BejermanSdkUnavailable as exc:
            _set_waiting_sdk(document_key, exc)
            logger.info(
                "bejerman_pdf_request_generation_ambiguous",
                extra={"document_key": document_key, "error": str(exc)},
            )
            return True
        except (BejermanSdkConfigError, BejermanSdkResponseError) as exc:
            # Después del guard persistente no podemos saber con certeza si el
            # SDK llegó a disparar Crystal antes de responder el error. La
            # reconciliación consulta los bytes oficiales sin volver a generar.
            _set_waiting_sdk(document_key, exc)
            logger.warning(
                "bejerman_pdf_request_generation_ambiguous",
                extra={"document_key": document_key, "error": str(exc)},
            )
            return True
        except Exception as exc:
            _set_waiting_sdk(document_key, exc)
            logger.exception("bejerman_pdf_request_generation_ambiguous", extra={"document_key": document_key})
            return True

        if not pdf:
            _set_waiting_sdk(document_key)
            return True

        _store_direct_pdf(current, pdf)
        logger.info("bejerman_pdf_request_generation_finished", extra={"document_key": document_key})
        return True
    finally:
        _release_global_lock()


def process_bejerman_pdf_requests_once(*, limit: int = 1) -> int:
    """Procesa solicitudes pendientes sin ejecutar más de una generación a la vez."""

    now = timezone.now()
    with connection.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {TABLE_NAME}
               SET state='waiting_sdk', lease_expires_at=NULL,
                   next_folder_check_at=CURRENT_TIMESTAMP,
                   last_error_code='BEJERMAN_PDF_GENERATION_AMBIGUOUS',
                   last_error_detail=COALESCE(last_error_detail, 'El worker se interrumpió durante la solicitud; se consultará el PDF oficial sin volver a generarlo.')
             WHERE state='requesting' AND lease_expires_at IS NOT NULL AND lease_expires_at <= %s
            """,
            [now],
        )
        cur.execute(
            f"""
            SELECT *
              FROM {TABLE_NAME}
             WHERE state = ANY(%s)
               AND next_folder_check_at <= %s
              ORDER BY next_folder_check_at,
                       CASE state
                         WHEN 'ready_direct' THEN 0
                         WHEN 'queued' THEN 1
                         WHEN 'requesting' THEN 2
                         WHEN 'waiting_sdk' THEN 3
                         ELSE 4
                       END,
                      created_at
             LIMIT %s
            """,
            [list(PROCESSABLE_STATES), now, max(1, int(limit or 1))],
        )
        columns = [column[0] for column in cur.description]
        rows = [dict(zip(columns, raw)) for raw in cur.fetchall()]

    processed = 0
    for row in rows:
        row = _claim_due_request(row["document_key"])
        if not row:
            continue
        started_at = time.monotonic()
        state = _clean(row.get("state"))
        if state == "queued":
            changed = _process_queued(row)
        elif state == "ready_direct":
            changed = _process_direct_copy(row)
        elif state in {"waiting_sdk", "requesting"}:
            changed = _process_sdk_wait(row)
        elif state == "waiting_folder":
            changed = _process_folder_wait(row)
        else:
            changed = False
        final_row = _load_request(row["document_key"]) or row
        final_state = _clean(final_row.get("state"))
        if final_state != state:
            logger.info(
                "bejerman_pdf_request_transition",
                extra=_transition_log_context(
                    final_row,
                    previous_state=state,
                    next_state=final_state,
                    started_at=started_at,
                ),
            )
        processed += int(bool(changed))
    return processed


def run_bejerman_pdf_requests_loop(*, interval_seconds: int = 2, limit: int = 1) -> None:
    interval = max(1, int(interval_seconds or 2))
    batch_limit = max(1, int(limit or 1))
    while True:
        close_old_connections()
        try:
            process_bejerman_pdf_requests_once(limit=batch_limit)
        except Exception:
            logger.exception("bejerman_pdf_request_worker_failed")
        finally:
            close_old_connections()
        time.sleep(interval)
