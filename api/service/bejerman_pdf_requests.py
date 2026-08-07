from __future__ import annotations

import hashlib
import logging
import os
import re
import socket
import time
import uuid
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import close_old_connections, connection
from django.utils import timezone

from .bejerman_companies import company_for_key
from .bejerman_sdk import (
    REMITO_COMPROBANTE_TYPES,
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
HEARTBEAT_TABLE = "bejerman_worker_heartbeats"
PDF_WORKER_KEY = "bejerman_pdf_requests"
GLOBAL_PDF_LOCK_NAME = "nexora:bejerman:pdf-generation"
PROCESSABLE_STATES = (
    "queued",
    "requesting",
    "waiting_sdk",
    "waiting_folder",
    "ready_direct",
)
# Estados cuyo avance depende exclusivamente del worker. `waiting_folder` queda
# afuera a propósito: su camino en el request es autosuficiente (escanea la carpeta
# y decide el corte por su cuenta), así que cortarlo por latido saltearía un rescate
# que hoy funciona.
WORKER_OWNED_STATES = ("queued", "requesting", "waiting_sdk", "ready_direct")

STAGE_LABELS = {
    "queued": "En cola para generar el remito",
    "requesting": "Generando el remito",
    "waiting_sdk": "Consultando el PDF oficial en Bejerman",
    "waiting_folder": "Esperando el archivo en la carpeta de Bejerman",
    "ready_direct": "Copiando el PDF a la carpeta configurada",
    "ready_folder": "PDF disponible",
    "failed": "La generación no pudo completarse",
}


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
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message, retry_after_ms=retry_after_ms)
        self.pdf_state = pdf_state
        self.generation_requested = generation_requested
        self.pdf_details = details or {}


class BejermanPdfNotPublishedError(BejermanSdkResponseError):
    code = "BEJERMAN_PDF_NOT_PUBLISHED"
    status_code = 504
    pdf_state = "failed"
    generation_requested = True

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.pdf_details = details or {}


class BejermanPdfWorkerStalledError(BejermanSdkResponseError):
    """El servicio que prepara los PDF no está avanzando la cola.

    Hereda de BejermanSdkResponseError y NO de BejermanSdkUnavailable a propósito:
    `get_facturacion_pdf` y `fetch_ris_pdf` convierten BejermanSdkUnavailable en un
    202 pendiente, que es exactamente el 202 eterno que esta excepción viene a
    cortar.
    """

    code = "BEJERMAN_PDF_WORKER_STALLED"
    status_code = 503

    def __init__(
        self,
        message: str,
        *,
        pdf_state: str,
        generation_requested: bool,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.pdf_state = pdf_state
        self.generation_requested = generation_requested
        self.pdf_details = details or {}


class BejermanPdfFolderUnavailableError(BejermanSdkUnavailable):
    code = "BEJERMAN_PDF_FOLDER_UNAVAILABLE"
    status_code = 503
    pdf_state = "folder_error"

    def __init__(self, message: str, *, generation_requested: bool, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.generation_requested = generation_requested
        self.pdf_details = details or {}


def pdf_error_metadata(exc: Exception) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    pdf_state = str(getattr(exc, "pdf_state", "") or "").strip()
    if pdf_state:
        payload["pdf_state"] = pdf_state
    if hasattr(exc, "generation_requested"):
        payload["generation_requested"] = bool(getattr(exc, "generation_requested"))
    # Merge puro de lo que la excepción ya trae: calcular acá el estado del worker
    # rompería a quien construya estas excepciones a mano (tests incluidos).
    details = getattr(exc, "pdf_details", None)
    if isinstance(details, dict):
        payload.update(details)
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


def remito_pdf_enabled() -> bool:
    """¿Genera NEXORA el PDF de los remitos?

    `BEJERMAN_REMITO_PDF_FALLBACK_ENABLED` es el nombre viejo, de cuando esto era
    sólo un último recurso; se sigue leyendo para no romper entornos ya configurados.
    """

    if getattr(settings, "BEJERMAN_REMITO_PDF_ENABLED", False):
        return True
    return bool(getattr(settings, "BEJERMAN_REMITO_PDF_FALLBACK_ENABLED", False))


def is_remito_type(tipo: Any) -> bool:
    """Las facturas quedan afuera: el renderer sólo reproduce remitos."""

    return _clean(tipo).upper() in REMITO_COMPROBANTE_TYPES


def _render_remito_pdf_bytes(comprobante: dict[str, Any], company_key: Any) -> bytes | None:
    """Renderiza el remito. Devuelve None ante cualquier problema, nunca levanta."""

    try:
        from .bejerman_remito_pdf import render_remito_pdf
        from .bejerman_remito_pdf_data import enrich_comprobante_for_pdf

        pdf = render_remito_pdf(
            enrich_comprobante_for_pdf(comprobante, company_key=company_key),
            company_key=company_key,
        )
    except Exception:
        logger.exception("bejerman_remito_pdf_render_failed", extra={"company_key": str(company_key)})
        return None
    return normalize_pdf_bytes(pdf) or None


def store_rendered_remito_pdf(
    comprobante: dict[str, Any],
    *,
    company_key: Any,
    remito_group_id: str | None = None,
    actor_user_id: int | None = None,
) -> bool:
    """Deja listo el PDF del remito apenas Bejerman devolvió el número.

    Se llama desde los circuitos de emisión, con el comprobante que Bejerman acaba de
    confirmar todavía en la mano — no hay que ir a desenterrarlo de lo guardado.

    Escribe la fila directamente en `ready_direct`, que es el estado que el request ya
    sabe servir, así que **todos los flujos de la interfaz quedan cubiertos sin
    tocarlos**. La copia a la carpeta del ERP la hace después el worker por el camino
    de siempre (`_process_direct_copy`).

    Nunca levanta: cuando esto corre el remito YA está emitido en Bejerman, así que un
    problema de PDF no puede tumbar la emisión. Devuelve si quedó guardado.
    """

    if not remito_pdf_enabled() or not isinstance(comprobante, dict):
        return False
    try:
        tipo = _clean(comprobante.get("Comprobante_Tipo")).upper()
        if not is_remito_type(tipo):
            return False

        client = BejermanSDKClient(company_key=company_key, actor_user_id=actor_user_id)
        reference = BejermanPdfReference(
            type=tipo,
            number=_clean(comprobante.get("Comprobante_Numero")),
            letter=_clean(comprobante.get("Comprobante_Letra")) or "R",
            point_of_sale=_clean(comprobante.get("Comprobante_PtoVenta")),
            issue_date=_clean(comprobante.get("Comprobante_FechaEmision")),
            customer_code=_clean(comprobante.get("Cliente_Codigo")),
        )
        values = _reference_values(client, reference)
        if not values["numero"] or not values["pto_venta"]:
            return False

        # Un lote de ingresos comparte el mismo remito y llama acá una vez por
        # ingreso: si ya está resuelto, no se vuelve a renderizar.
        existente = _load_request(values["document_key"])
        if existente and _clean(existente.get("state")) in {"ready_direct", "ready_folder"}:
            return True

        pdf = _render_remito_pdf_bytes(comprobante, company_key)
        if not pdf:
            return False

        checksum = hashlib.sha256(pdf).hexdigest()
        with connection.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {TABLE_NAME} (
                  document_key, remito_group_id, actor_user_id, company_key, customer_code,
                  tipo, letra, pto_venta, numero, issue_date, state, ready_at,
                  deadline_at, next_folder_check_at, remote_path, pdf_payload, payload_sha256
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::date, 'ready_direct',
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + INTERVAL '10 minutes',
                        CURRENT_TIMESTAMP, %s, %s, %s)
                ON CONFLICT (document_key) DO UPDATE
                   SET state='ready_direct',
                       ready_at=CURRENT_TIMESTAMP,
                       lease_expires_at=NULL,
                       next_folder_check_at=CURRENT_TIMESTAMP,
                       pdf_payload=EXCLUDED.pdf_payload,
                       payload_sha256=EXCLUDED.payload_sha256,
                       remito_group_id=COALESCE({TABLE_NAME}.remito_group_id, EXCLUDED.remito_group_id),
                       last_error_code=NULL, last_error_detail=NULL
                 WHERE {TABLE_NAME}.state <> 'ready_folder'
                """,
                [
                    values["document_key"],
                    _clean(remito_group_id) or None,
                    actor_user_id,
                    values["company_key"],
                    values["customer_code"],
                    values["tipo"],
                    values["letra"],
                    values["pto_venta"],
                    values["numero"],
                    values["issue_date"],
                    comprobante_pdf_output_path(client, reference),
                    pdf,
                    checksum,
                ],
            )
    except Exception:
        logger.exception(
            "bejerman_remito_pdf_store_failed",
            extra={"company_key": str(company_key), "remito_group_id": remito_group_id},
        )
        return False

    logger.info(
        "bejerman_remito_pdf_listo_al_emitir",
        extra={
            "document_key": values["document_key"],
            "bytes": len(pdf),
            "remito_group_id": remito_group_id,
        },
    )
    return True


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


def _seconds_since(value: Any, *, now: Any) -> int | None:
    if not value:
        return None
    try:
        return max(0, int((now - value).total_seconds()))
    except (TypeError, ValueError):
        return None


def _progress_metadata(
    row: dict[str, Any],
    *,
    worker: dict[str, Any] | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Datos honestos de progreso para que la UI pueda decir en qué está."""

    moment = now or timezone.now()
    state = _clean(row.get("state")) or "queued"
    deadline = row.get("deadline_at")
    payload: dict[str, Any] = {
        # El document_key viaja para que la UI pueda ofrecer "Reintentar PDF" sin
        # tener que reconstruir la referencia del comprobante.
        "document_key": _clean(row.get("document_key")),
        "stage_label": STAGE_LABELS.get(state, "Preparando el PDF"),
        "queued_seconds": _seconds_since(row.get("created_at"), now=moment),
        "waiting_seconds": _seconds_since(
            row.get("generation_requested_at") or row.get("created_at"), now=moment
        ),
        "regeneration_count": int(row.get("regeneration_count") or 0),
    }
    if deadline:
        payload["deadline_at"] = deadline.isoformat()
        payload["deadline_seconds_left"] = max(0, int((deadline - moment).total_seconds()))
    if worker is not None:
        payload["worker_alive"] = worker.get("alive")
        last_beat = worker.get("last_beat_at")
        if last_beat:
            payload["worker_last_beat_at"] = last_beat.isoformat()
        if worker.get("stage"):
            payload["worker_stage"] = worker.get("stage")
    return payload


def _pending_for(
    row: dict[str, Any],
    *,
    worker: dict[str, Any] | None = None,
    now: Any = None,
    retry_after_ms: int | None = None,
) -> BejermanPdfRequestPending:
    state = _clean(row.get("state")) or "queued"
    generation_requested = int(row.get("generation_attempt_count") or 0) > 0
    if retry_after_ms is None:
        retry_after_ms = 2500 if state in {"queued", "requesting"} else 5000
    genera_nexora = remito_pdf_enabled() and is_remito_type(row.get("tipo"))
    if state == "queued":
        detail = (
            "El remito ya está emitido en Bejerman. NEXORA está preparando el PDF para imprimir."
            if genera_nexora
            else "El PDF oficial quedó en cola para solicitarse una sola vez a Bejerman."
        )
    elif state == "requesting":
        detail = (
            "NEXORA está generando el PDF del remito."
            if genera_nexora
            else "Bejerman está procesando la única solicitud de generación del PDF oficial."
        )
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
        details=_progress_metadata(row, worker=worker, now=now),
    )


def _not_published_for(
    row: dict[str, Any],
    *,
    worker: dict[str, Any] | None = None,
    now: Any = None,
) -> BejermanPdfNotPublishedError:
    detail = _clean(row.get("last_error_detail"))
    if not detail:
        detail = (
            "Bejerman no devolvió el PDF oficial dentro de los 10 minutos posteriores a la solicitud. "
            "Se puede reintentar la generación desde el panel de Bejerman."
        )
    error = BejermanPdfNotPublishedError(detail, details=_progress_metadata(row, worker=worker, now=now))
    error.generation_requested = int(row.get("generation_attempt_count") or 0) > 0
    return error


def _worker_stall_error(
    row: dict[str, Any],
    worker: dict[str, Any],
    *,
    now: Any,
) -> BejermanPdfWorkerStalledError | None:
    """¿Hay que dejar de responder "pendiente" porque nadie está avanzando la cola?

    No escribe nada en la fila: `generation_attempt_count` queda intacto, así que
    cuando el worker vuelve genera igual y una sola vez.
    """

    state = _clean(row.get("state")) or "queued"
    deadline = row.get("deadline_at")

    if worker.get("alive") is False:
        grace = _positive_int_setting("BEJERMAN_PDF_WORKER_STALL_GRACE_SECONDS", 90)
        waited = _seconds_since(row.get("generation_requested_at") or row.get("created_at"), now=now)
        if waited is not None and waited >= grace:
            return BejermanPdfWorkerStalledError(
                "El servicio que prepara los PDF de Bejerman no está respondiendo. "
                "El comprobante ya está emitido en Bejerman; el PDF se va a poder recuperar "
                "cuando el servicio vuelva o al reintentar la generación.",
                pdf_state=state,
                generation_requested=int(row.get("generation_attempt_count") or 0) > 0,
                details=_progress_metadata(row, worker=worker, now=now),
            )

    if state == "queued" and deadline and deadline <= now:
        # El worker puede estar vivo pero nunca haber reclamado esta fila (lock
        # global tomado, o un sondeo de carpeta que falla siempre).
        return BejermanPdfWorkerStalledError(
            "La cola de PDF de Bejerman no avanzó dentro del plazo previsto y la generación "
            "todavía no llegó a solicitarse. El comprobante ya está emitido en Bejerman.",
            pdf_state=state,
            generation_requested=False,
            details=_progress_metadata(row, worker=worker, now=now),
        )

    return None


def requeue_pdf_request(
    document_key: str,
    *,
    reason: str,
    actor_user_id: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Abre un ciclo de generación nuevo para un PDF que no llegó.

    `generation_attempt_count` cuenta intentos *dentro de un ciclo*, así que
    reencolar lo vuelve a 0 y suma uno a `regeneration_count`: Bejerman nunca recibe
    dos WS_GenerarComprobantePDF por el mismo ciclo, que es la razón de ser del
    single-shot (un timeout no confirma que Bejerman haya cancelado el trabajo de
    Crystal). Antes de pedir nada se busca el archivo en la carpeta: si ya está, no
    se genera de nuevo.

    Devuelve {"outcome": "ready" | "requeued" | "exhausted" | "missing"}.
    """

    row = _load_request(document_key)
    if row is None:
        return {"outcome": "missing", "document_key": document_key}

    client = _client_from_row(row)
    reference = _reference_from_row(row)
    remote_path = comprobante_pdf_output_path(client, reference)

    # Primero la vía barata y sin efectos: el archivo puede estar publicado con un
    # nombre que el sondeo normal (solo nombres exactos) no mira.
    folder_pdf, folder_status = probe_comprobante_pdf_copy(
        client, reference, interactive=True, allow_directory_scan=True,
    )
    if folder_pdf:
        _mark_ready_folder(document_key, remote_path)
        logger.info(
            "bejerman_pdf_request_requeue_resolved_from_folder",
            extra={"document_key": document_key, "reason": reason},
        )
        return {"outcome": "ready", "document_key": document_key, "state": "ready_folder"}

    max_regenerations = _positive_int_setting("BEJERMAN_PDF_MAX_REGENERATIONS", 2)
    regeneration_count = int(row.get("regeneration_count") or 0)
    if not force and regeneration_count >= max_regenerations:
        logger.warning(
            "bejerman_pdf_request_requeue_exhausted",
            extra={
                "document_key": document_key,
                "reason": reason,
                "regeneration_count": regeneration_count,
                "folder_status": folder_status,
            },
        )
        return {
            "outcome": "exhausted",
            "document_key": document_key,
            "regeneration_count": regeneration_count,
        }

    deadline_seconds = _positive_int_setting("BEJERMAN_PDF_WAIT_DEADLINE_SECONDS", 600)
    with connection.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {TABLE_NAME}
               SET state='queued',
                   generation_attempt_count=0,
                   generation_requested_at=NULL,
                   regeneration_count = {TABLE_NAME}.regeneration_count + 1,
                   last_regenerated_at=CURRENT_TIMESTAMP,
                   lease_expires_at=NULL,
                   pdf_payload=NULL,
                   payload_sha256=NULL,
                   ready_at=NULL,
                   deadline_at=CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                   next_folder_check_at=CURRENT_TIMESTAMP,
                   actor_user_id=COALESCE(%s, {TABLE_NAME}.actor_user_id),
                   last_error_code=NULL,
                   last_error_detail=NULL
             WHERE document_key=%s
            RETURNING *
            """,
            [deadline_seconds, actor_user_id, document_key],
        )
        updated = _row_from_cursor(cur) or {}

    logger.info(
        "bejerman_pdf_request_requeued",
        extra={
            "document_key": document_key,
            "reason": reason,
            "regeneration_count": int(updated.get("regeneration_count") or 0),
            "actor_user_id": actor_user_id,
        },
    )
    return {
        "outcome": "requeued",
        "document_key": document_key,
        "state": "queued",
        "regeneration_count": int(updated.get("regeneration_count") or 0),
    }


def _render_own_remito_pdf(row: dict[str, Any]) -> bool:
    """Reproduce el remito a partir de la emisión guardada.

    Es el camino para los comprobantes que no pasaron por `store_rendered_remito_pdf`
    al emitirse: los anteriores a este cambio y los que se reimprimen. El comprobante
    ya está emitido y numerado en Bejerman; lo único que falta es el papel. Se deja
    como si hubiera llegado del SDK, así el resto del circuito (incluida la copia a la
    carpeta del ERP) sigue igual.
    """

    if not remito_pdf_enabled() or not is_remito_type(row.get("tipo")):
        return False
    document_key = row["document_key"]
    try:
        from .bejerman_remito_pdf_data import load_comprobante_for_document

        comprobante = load_comprobante_for_document(row)
    except Exception:
        logger.exception("bejerman_remito_pdf_payload_failed", extra={"document_key": document_key})
        return False
    if not comprobante:
        logger.warning("bejerman_remito_pdf_sin_payload", extra={"document_key": document_key})
        return False

    # `_render_remito_pdf_bytes` ya valida los bytes. Se comprueba acá y no en
    # `_store_direct_pdf`, que ante un PDF inválido marca la fila como fallada
    # culpando a Bejerman: si el que salió mal es el nuestro, corresponde dejar el
    # error original de la cola.
    if not pdf:
        logger.error("bejerman_remito_pdf_invalido", extra={"document_key": document_key})
        return False

    logger.info(
        "bejerman_remito_pdf_generado",
        extra={"document_key": document_key, "bytes": len(pdf)},
    )
    return _store_direct_pdf(row, pdf)


def _fail_or_requeue(row: dict[str, Any], *, code: str, detail: str, reason: str) -> None:
    """Antes de dar un PDF por perdido, abrir un ciclo nuevo si quedan intentos."""

    document_key = row["document_key"]
    max_regenerations = _positive_int_setting("BEJERMAN_PDF_MAX_REGENERATIONS", 2)
    if int(row.get("regeneration_count") or 0) < max_regenerations:
        result = requeue_pdf_request(document_key, reason=reason)
        if result.get("outcome") in {"ready", "requeued"}:
            return
    # Agotados los reintentos contra Bejerman, mejor un PDF nuestro que ninguno.
    if _render_own_remito_pdf(row):
        return
    _mark_failed(document_key, code, detail)


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
        # El probe interactivo es caro (puede escanear la carpeta remota de Bejerman).
        # Un lock consultivo por documento asegura que, mientras un request lo está
        # pagando, los sondeos concurrentes del navegador para el mismo documento no
        # disparen su propio probe en paralelo (la "tormenta SFTP" que se quiere evitar).
        # No usamos un INSERT especulativo antes del probe porque ATOMIC_REQUESTS deja
        # esa fila sin commitear durante todo el probe, y cualquier otro INSERT con el
        # mismo document_key quedaría bloqueado esperando esa transacción — mismo
        # problema, solo que como espera de lock de fila en vez de llamadas SFTP en paralelo.
        if _try_document_probe_lock(document_key):
            try:
                row = _load_request(document_key)  # otro request pudo terminar mientras esperábamos el lock
                if row is None:
                    folder_pdf, folder_status = probe_comprobante_pdf_copy(
                        client, reference, interactive=True, allow_directory_scan=False,
                    )
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
            finally:
                _release_document_probe_lock(document_key)
        else:
            logger.info("bejerman_pdf_request_probe_lock_busy", extra={"document_key": document_key})
            row = _load_request(document_key)
            if row is None:
                raise BejermanPdfRequestPending(
                    "Otra solicitud ya está comprobando este comprobante en Bejerman; NEXORA no repite la consulta.",
                    pdf_state="queued",
                    generation_requested=False,
                    retry_after_ms=2500,
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
        # deadline sin volver a invocar SOAP. Chequeo de última oportunidad:
        # sí vale la pena pagar el escaneo completo de carpeta acá.
        folder_pdf, folder_status = probe_comprobante_pdf_copy(
            client, reference, interactive=True, allow_directory_scan=True,
        )
        if folder_pdf:
            _mark_ready_folder(document_key, remote_path)
            return folder_pdf, "application/pdf"
        if folder_status == "error":
            raise BejermanPdfFolderUnavailableError(
                "No se pudo comprobar la carpeta configurada de PDFs de Bejerman.",
                generation_requested=int(row.get("generation_attempt_count") or 0) > 0,
            )
        if state == "failed":
            # `failed` dejó de ser una lápida: si todavía quedan ciclos, se abre uno
            # nuevo en vez de devolver 504 para siempre.
            max_regenerations = _positive_int_setting("BEJERMAN_PDF_MAX_REGENERATIONS", 2)
            if int(row.get("regeneration_count") or 0) < max_regenerations:
                result = requeue_pdf_request(
                    document_key,
                    reason="http_request_failed_state",
                    actor_user_id=actor_user_id or getattr(client, "actor_user_id", None),
                )
                if result.get("outcome") == "requeued":
                    row = _load_request(document_key) or row
                    state = "queued"
                elif result.get("outcome") == "ready":
                    folder_pdf, _ = probe_comprobante_pdf_copy(
                        client, reference, interactive=True, allow_directory_scan=True,
                    )
                    if folder_pdf:
                        return folder_pdf, "application/pdf"

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

    now = timezone.now()
    deadline = row.get("deadline_at")
    worker = worker_liveness()

    if state in WORKER_OWNED_STATES:
        stalled = _worker_stall_error(row, worker, now=now)
        if stalled is not None:
            raise stalled

    if state in {"requesting", "waiting_sdk"} and deadline and deadline <= now:
        grace = _positive_int_setting("BEJERMAN_PDF_DEADLINE_GRACE_SECONDS", 120)
        if now < deadline + timedelta(seconds=grace):
            # Dentro de la gracia la consulta HTTP no cierra el caso: el worker
            # todavía puede hacer una última consulta oficial de forma serializada.
            with connection.cursor() as cur:
                cur.execute(
                    f"UPDATE {TABLE_NAME} SET next_folder_check_at=CURRENT_TIMESTAMP WHERE document_key=%s",
                    [document_key],
                )
            raise _pending_for(row, worker=worker, now=now)
        # El worker tuvo su ventana y no cerró el caso. Cerrarlo desde acá evita el
        # 202 eterno; el guard por estado evita pisarlo si justo terminó.
        with connection.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {TABLE_NAME}
                   SET state='failed', lease_expires_at=NULL,
                       last_error_code='BEJERMAN_PDF_NOT_PUBLISHED',
                       last_error_detail=%s
                 WHERE document_key=%s AND state = ANY(%s)
                RETURNING *
                """,
                [
                    "Bejerman no devolvió el PDF oficial y el servicio que lo prepara no cerró el caso a tiempo.",
                    document_key,
                    ["requesting", "waiting_sdk"],
                ],
            )
            closed = _row_from_cursor(cur)
        if closed is None:
            # Carrera con el worker: reconsultar y dejar que el próximo sondeo
            # levante el archivo en vez de devolver un 504 falso.
            row = _load_request(document_key) or row
            if _clean(row.get("state")) in {"ready_direct", "ready_folder"}:
                raise _pending_for(row, worker=worker, now=now, retry_after_ms=1000)
            raise _pending_for(row, worker=worker, now=now)
        raise _not_published_for(closed, worker=worker, now=now)

    if state == "waiting_folder" and deadline and deadline <= now:
        # Antes de declarar que Bejerman no publicó el archivo, confirmar una
        # última vez la fuente durable. Un error de acceso no equivale a un
        # archivo ausente y no debe convertirse en un falso NOT_PUBLISHED.
        # Chequeo de última oportunidad: sí vale la pena el escaneo completo.
        folder_pdf, folder_status = probe_comprobante_pdf_copy(
            client, reference, interactive=True, allow_directory_scan=True,
        )
        if folder_pdf:
            _mark_ready_folder(document_key, remote_path)
            return folder_pdf, "application/pdf"
        if folder_status == "error":
            raise BejermanPdfFolderUnavailableError(
                "No se pudo comprobar la carpeta configurada de PDFs de Bejerman.",
                generation_requested=int(row.get("generation_attempt_count") or 0) > 0,
            )
        # Antes de darlo por perdido, abrir un ciclo de generación nuevo si quedan.
        max_regenerations = _positive_int_setting("BEJERMAN_PDF_MAX_REGENERATIONS", 2)
        if int(row.get("regeneration_count") or 0) < max_regenerations:
            result = requeue_pdf_request(document_key, reason="http_request_folder_deadline")
            if result.get("outcome") in {"ready", "requeued"}:
                row = _load_request(document_key) or row
                raise _pending_for(row, worker=worker, now=now)
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
        raise _not_published_for(row, worker=worker, now=now)

    if state == "failed":
        raise _not_published_for(row, worker=worker, now=now)
    raise _pending_for(row, worker=worker, now=now)


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


_heartbeat_table_ready_cache: bool = False
_worker_instance_id = uuid.uuid4().hex[:12]


def _heartbeat_table_ready() -> bool:
    """¿Existe la tabla de latidos?

    Se pregunta con to_regclass y nunca capturando ProgrammingError: con
    ATOMIC_REQUESTS un error de SQL aborta toda la transacción del request y las
    consultas siguientes fallarían con "current transaction is aborted".
    """

    global _heartbeat_table_ready_cache
    if _heartbeat_table_ready_cache:
        return True
    with connection.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", [f"public.{HEARTBEAT_TABLE}"])
        row = cur.fetchone()
    _heartbeat_table_ready_cache = bool(row and row[0])
    return _heartbeat_table_ready_cache


def record_worker_heartbeat(
    *,
    stage: str,
    document_key: str | None = None,
    ttl_seconds: int | None = None,
    error: str | None = None,
) -> None:
    """Late en nombre del worker. Nunca puede tumbar el loop."""

    try:
        if not _heartbeat_table_ready():
            return
        ttl = ttl_seconds or _positive_int_setting("BEJERMAN_PDF_WORKER_HEARTBEAT_TTL_SECONDS", 300)
        with connection.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {HEARTBEAT_TABLE} (
                  worker_key, instance_id, hostname, pid, stage, current_document_key,
                  last_beat_at, beat_expires_at, tick_count, last_error_detail
                )
                VALUES (%s, %s, %s, %s, %s, %s,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'), 1, %s)
                ON CONFLICT (worker_key) DO UPDATE
                   SET instance_id = EXCLUDED.instance_id,
                       hostname = EXCLUDED.hostname,
                       pid = EXCLUDED.pid,
                       stage = EXCLUDED.stage,
                       current_document_key = EXCLUDED.current_document_key,
                       last_beat_at = CURRENT_TIMESTAMP,
                       beat_expires_at = EXCLUDED.beat_expires_at,
                       tick_count = {HEARTBEAT_TABLE}.tick_count + 1,
                       last_error_detail = EXCLUDED.last_error_detail,
                       started_at = CASE
                           WHEN {HEARTBEAT_TABLE}.instance_id = EXCLUDED.instance_id
                           THEN {HEARTBEAT_TABLE}.started_at
                           ELSE CURRENT_TIMESTAMP
                       END
                """,
                [
                    PDF_WORKER_KEY,
                    _worker_instance_id,
                    socket.gethostname()[:200],
                    os.getpid(),
                    _clean(stage)[:100],
                    _clean(document_key) or None,
                    ttl,
                    (str(error)[:2000] if error else None),
                ],
            )
    except Exception:
        logger.warning("bejerman_pdf_worker_heartbeat_failed", exc_info=True)


def worker_liveness() -> dict[str, Any]:
    """Estado del worker en tres valores.

    `alive=None` significa "no se sabe" (tabla todavía sin aplicar) y se trata como
    fail-open: el sistema se comporta igual que antes de este cambio.
    """

    unknown: dict[str, Any] = {"alive": None}
    try:
        if not _heartbeat_table_ready():
            return unknown
        with connection.cursor() as cur:
            cur.execute(
                f"""
                SELECT last_beat_at, beat_expires_at, stage, current_document_key,
                       started_at, hostname, pid, tick_count,
                       beat_expires_at > CURRENT_TIMESTAMP AS alive,
                       EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - last_beat_at)) AS age_seconds
                  FROM {HEARTBEAT_TABLE}
                 WHERE worker_key = %s
                """,
                [PDF_WORKER_KEY],
            )
            row = _row_from_cursor(cur)
    except Exception:
        logger.warning("bejerman_pdf_worker_liveness_failed", exc_info=True)
        return unknown
    if row is None:
        # La tabla existe pero el worker nunca latió: está caído o nunca arrancó.
        return {"alive": False}
    return {
        "alive": bool(row.get("alive")),
        "last_beat_at": row.get("last_beat_at"),
        "beat_expires_at": row.get("beat_expires_at"),
        "age_seconds": int(row.get("age_seconds") or 0),
        "stage": _clean(row.get("stage")),
        "current_document_key": _clean(row.get("current_document_key")),
        "started_at": row.get("started_at"),
        "hostname": _clean(row.get("hostname")),
        "pid": row.get("pid"),
        "tick_count": int(row.get("tick_count") or 0),
    }


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def pdf_queue_status(*, limit: int = 20, stuck_only: bool = False) -> dict[str, Any]:
    """Estado de la cola de PDF, para diagnosticar sin entrar al servidor."""

    now = timezone.now()
    worker = worker_liveness()
    stuck_grace = _positive_int_setting("BEJERMAN_PDF_WORKER_STALL_GRACE_SECONDS", 90)

    with connection.cursor() as cur:
        cur.execute(f"SELECT state, COUNT(*) FROM {TABLE_NAME} GROUP BY state")
        counts = {str(state): int(total) for state, total in cur.fetchall()}

        cur.execute(
            f"""
            SELECT EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - MIN(next_folder_check_at)))
              FROM {TABLE_NAME}
             WHERE state = ANY(%s) AND next_folder_check_at <= CURRENT_TIMESTAMP
            """,
            [list(PROCESSABLE_STATES)],
        )
        oldest_due = cur.fetchone()
        oldest_due_seconds = int(oldest_due[0]) if oldest_due and oldest_due[0] is not None else None

        conditions = ["(state = ANY(%s) OR (state = 'failed' AND updated_at >= CURRENT_TIMESTAMP - INTERVAL '24 hours'))"]
        params: list[Any] = [list(PROCESSABLE_STATES)]
        if stuck_only:
            conditions.append("(deadline_at <= CURRENT_TIMESTAMP OR state = 'failed')")
        cur.execute(
            f"""
            SELECT document_key, company_key, customer_code, tipo, letra, pto_venta, numero,
                   issue_date, state, generation_attempt_count, regeneration_count,
                   created_at, deadline_at, next_folder_check_at, last_regenerated_at,
                   last_error_code, last_error_detail, remote_path,
                   EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - created_at)) AS age_seconds
              FROM {TABLE_NAME}
             WHERE {' AND '.join(conditions)}
             ORDER BY created_at DESC
             LIMIT %s
            """,
            [*params, max(1, int(limit or 20))],
        )
        columns = [column[0] for column in cur.description]
        raw_rows = [dict(zip(columns, values)) for values in cur.fetchall()]

    rows: list[dict[str, Any]] = []
    stuck_count = 0
    for row in raw_rows:
        deadline = row.get("deadline_at")
        state = _clean(row.get("state"))
        is_stuck = state == "failed" or bool(deadline and deadline <= now)
        if is_stuck:
            stuck_count += 1
        rows.append(
            {
                "documentKey": _clean(row.get("document_key")),
                "companyKey": _clean(row.get("company_key")),
                "customerCode": _clean(row.get("customer_code")),
                "comprobante": " ".join(
                    part
                    for part in [
                        _clean(row.get("tipo")),
                        _clean(row.get("letra")),
                        f"{_clean(row.get('pto_venta'))}-{_clean(row.get('numero'))}",
                    ]
                    if part
                ),
                "issueDate": str(row.get("issue_date") or ""),
                "state": state,
                "stageLabel": STAGE_LABELS.get(state, ""),
                "generationAttemptCount": int(row.get("generation_attempt_count") or 0),
                "regenerationCount": int(row.get("regeneration_count") or 0),
                "ageSeconds": int(row.get("age_seconds") or 0),
                "createdAt": _iso(row.get("created_at")),
                "deadlineAt": _iso(deadline),
                "nextFolderCheckAt": _iso(row.get("next_folder_check_at")),
                "lastRegeneratedAt": _iso(row.get("last_regenerated_at")),
                "lastErrorCode": _clean(row.get("last_error_code")),
                "lastErrorDetail": _clean(row.get("last_error_detail")),
                "remotePath": _clean(row.get("remote_path")),
                "stuck": is_stuck,
            }
        )

    return {
        "worker": {
            "alive": worker.get("alive"),
            "lastBeatAt": _iso(worker.get("last_beat_at")),
            "beatExpiresAt": _iso(worker.get("beat_expires_at")),
            "ageSeconds": worker.get("age_seconds"),
            "stage": worker.get("stage") or "",
            "currentDocumentKey": worker.get("current_document_key") or "",
            "startedAt": _iso(worker.get("started_at")),
            "hostname": worker.get("hostname") or "",
            "pid": worker.get("pid"),
            "tickCount": worker.get("tick_count"),
            "stallGraceSeconds": stuck_grace,
        },
        "queue": {
            "counts": counts,
            "oldestDueSeconds": oldest_due_seconds,
            "stuckCount": stuck_count,
            "maxRegenerations": _positive_int_setting("BEJERMAN_PDF_MAX_REGENERATIONS", 2),
        },
        "rows": rows,
    }


def _try_global_lock() -> bool:
    with connection.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", [GLOBAL_PDF_LOCK_NAME])
        result = cur.fetchone()
    return bool(result and result[0])


def _release_global_lock() -> None:
    with connection.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", [GLOBAL_PDF_LOCK_NAME])


DOCUMENT_PROBE_LOCK_SALT = 1  # Distinto del salt de GLOBAL_PDF_LOCK_NAME (0); mismo namespace de hash, sin colisión posible.


def _try_document_probe_lock(document_key: str) -> bool:
    """Serializa el probe interactivo de un mismo documento entre requests HTTP concurrentes.

    No bloqueante y ajeno a MVCC/transacciones abiertas (a diferencia de un INSERT bajo
    ATOMIC_REQUESTS), así que no se cuelga esperando a otro request que todavía está
    consultando el SFTP de Bejerman.
    """
    with connection.cursor() as cur:
        cur.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, %s))",
            [document_key, DOCUMENT_PROBE_LOCK_SALT],
        )
        result = cur.fetchone()
    return bool(result and result[0])


def _release_document_probe_lock(document_key: str) -> None:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_unlock(hashtextextended(%s, %s))",
            [document_key, DOCUMENT_PROBE_LOCK_SALT],
        )


def _set_next_check(document_key: str, seconds: int) -> None:
    with connection.cursor() as cur:
        cur.execute(
            f"UPDATE {TABLE_NAME} SET next_folder_check_at=CURRENT_TIMESTAMP + (%s * INTERVAL '1 second') WHERE document_key=%s",
            [seconds, document_key],
        )


def _claim_due_request(document_key: str, state: str = "") -> dict[str, Any] | None:
    """Reserva brevemente un chequeo para evitar trabajo duplicado entre workers.

    Solo `queued` paga la reserva larga, porque es el único que puede disparar una
    generación de varios minutos. Para el resto, una reserva larga significaba que
    un tick interrumpido dejaba la fila parada 2 minutos con un cliente esperando.
    """

    if _clean(state) == "queued":
        lease_seconds = _positive_int_setting("BEJERMAN_PDF_CLAIM_LEASE_QUEUED_SECONDS", 120)
    else:
        lease_seconds = _positive_int_setting("BEJERMAN_PDF_CLAIM_LEASE_SECONDS", 30)
    with connection.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {TABLE_NAME}
               SET next_folder_check_at=CURRENT_TIMESTAMP + (%s * INTERVAL '1 second')
             WHERE document_key=%s
               AND state = ANY(%s)
               AND next_folder_check_at <= CURRENT_TIMESTAMP
            RETURNING *
            """,
            [lease_seconds, document_key, list(PROCESSABLE_STATES)],
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
        _fail_or_requeue(
            row,
            code="BEJERMAN_PDF_NOT_PUBLISHED",
            detail="Bejerman no devolvió el PDF oficial dentro de los 10 minutos posteriores a la solicitud.",
            reason="sdk_wait_deadline",
        )
        logger.warning("bejerman_pdf_request_not_available", extra={"document_key": document_key})
        return True
    _set_next_check(document_key, _positive_int_setting("BEJERMAN_PDF_SDK_CHECK_SECONDS", 5))
    return False


def _process_folder_wait(row: dict[str, Any]) -> bool:
    document_key = row["document_key"]
    client = _client_from_row(row)
    reference = _reference_from_row(row)
    deadline = row.get("deadline_at")
    deadline_exceeded = bool(deadline and deadline <= timezone.now())
    # El barrido por prefijo ahora sí corre en cada tick: el listado de la carpeta
    # se cachea unos segundos, así que cuesta un listdir cada tanto y no uno por
    # documento por tick. Antes estaba apagado por costo, y por eso un PDF publicado
    # por Bejerman con su propia convención de nombre no aparecía nunca.
    pdf, folder_status = probe_comprobante_pdf_copy(
        client, reference, interactive=False, allow_directory_scan=True,
    )
    if pdf:
        _mark_ready_folder(document_key, comprobante_pdf_output_path(client, reference))
        logger.info("bejerman_pdf_request_ready_folder", extra={"document_key": document_key})
        return True
    if folder_status == "error":
        _set_next_check(document_key, _positive_int_setting("BEJERMAN_PDF_FOLDER_CHECK_SECONDS", 5))
        return False
    if deadline_exceeded:
        _fail_or_requeue(
            row,
            code="BEJERMAN_PDF_NOT_PUBLISHED",
            detail="Bejerman no publicó el PDF oficial dentro de los 10 minutos posteriores a la solicitud.",
            reason="folder_wait_deadline",
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

        # Corre sosteniendo GLOBAL_PDF_LOCK_NAME: un escaneo lento de carpeta acá frena
        # la generación de *cualquier* PDF (ambas empresas, remitos/facturas/RIS), no
        # solo la de este documento. El chequeo por nombre exacto alcanza en el caso
        # normal (NEXORA escribe con el mismo nombre determinístico que prueba acá).
        folder_pdf, folder_status = probe_comprobante_pdf_copy(
            client, reference, interactive=False, allow_directory_scan=False,
        )
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

        # Remitos emitidos antes de este cambio (o cuya emisión no llegó a guardar el
        # PDF) no tienen la fila lista. Se reproducen acá en vez de pedirle a Bejerman
        # un PDF que no entrega desde el 20/07. Va antes del claim y del register: así
        # no consume el intento único ni paga la conexión al SDK.
        if is_remito_type(current.get("tipo")) and _render_own_remito_pdf(current):
            logger.info("bejerman_remito_pdf_generado_en_worker", extra={"document_key": document_key})
            return True

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


def process_bejerman_pdf_requests_once(*, limit: int = 1, heartbeat: bool = False) -> int:
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
        row = _claim_due_request(row["document_key"], _clean(row.get("state")))
        if not row:
            continue
        started_at = time.monotonic()
        state = _clean(row.get("state"))
        if heartbeat:
            # Refrescar el TTL antes de cada documento: una generación de 90s no
            # tiene que parecer un worker muerto, y deja ver en qué está trabado.
            record_worker_heartbeat(stage=f"processing:{state}", document_key=row["document_key"])
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
    close_old_connections()
    record_worker_heartbeat(stage="starting")
    while True:
        close_old_connections()
        try:
            # El latido lo escribe el propio loop y no un hilo aparte: un hilo
            # seguiría latiendo con el loop colgado en un socket SSH muerto, que es
            # justo el caso que hay que poder detectar.
            record_worker_heartbeat(stage="tick")
            process_bejerman_pdf_requests_once(limit=batch_limit, heartbeat=True)
        except Exception as exc:
            logger.exception("bejerman_pdf_request_worker_failed")
            record_worker_heartbeat(stage="error", error=str(exc))
        finally:
            close_old_connections()
        time.sleep(interval)
