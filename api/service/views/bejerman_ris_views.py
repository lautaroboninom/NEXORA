from __future__ import annotations

from django.http import HttpResponse
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from ..bejerman_ris import (
    BejermanRisBusyError,
    BejermanRisError,
    BejermanRisPdfError,
    BejermanRisPdfPendingError,
    BejermanRisPreflightError,
    BejermanRisRegisteredNoPdfError,
    BejermanRisReviewRequiredError,
    apply_article_fix_from_bejerman,
    apply_customer_fix_from_bejerman,
    emit_or_get_ris,
    fetch_ris_pdf,
    get_ris_status_for_ingreso,
    preflight_ris_for_ingreso,
    preflight_ris_for_request_payload,
    serialize_ris_row,
)
from ..bejerman_documents import retry_after_header_value
from ..bejerman_sdk import BejermanSdkConfigError, BejermanSdkResponseError, BejermanSdkUnavailable
from ..pdf import render_serial_barcode_pdf
from ..permissions import require_any_permission
from .helpers import _set_audit_user, os_label, q, require_roles
from .pdf_wait_pages import render_bejerman_pdf_wait_page
from .reportes_views import _merge_pdf_documents


def _actor_id(request):
    return getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)


def _pdf_response(pdf_bytes: bytes, filename: str, content_type: str = "application/pdf") -> HttpResponse:
    response = HttpResponse(pdf_bytes, content_type=content_type or "application/pdf")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


def _parse_ris_lote_ingreso_ids(raw_ids, *, max_ids: int = 50) -> list[int]:
    ingreso_ids: list[int] = []
    for raw_id in str(raw_ids or "").split(","):
        value = raw_id.strip()
        if not value:
            continue
        try:
            ingreso_id = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("El parámetro 'ids' debe contener identificadores numéricos.") from exc
        if ingreso_id <= 0:
            raise ValueError("El parámetro 'ids' debe contener identificadores positivos.")
        if ingreso_id not in ingreso_ids:
            ingreso_ids.append(ingreso_id)
    if not ingreso_ids:
        raise ValueError("El parámetro 'ids' es obligatorio.")
    if len(ingreso_ids) > max_ids:
        raise ValueError(f"Se pueden preparar como máximo {max_ids} remitos por lote.")
    return ingreso_ids


def _ris_lote_pdf_url(ingreso_ids: list[int]) -> str:
    ids = ",".join(str(ingreso_id) for ingreso_id in ingreso_ids)
    return f"/api/ingresos/ris/lote/pdf/?ids={ids}"


def _ris_lote_document(ingreso_id: int, ris: dict | None = None, **extra) -> dict:
    status = ris if isinstance(ris, dict) else get_ris_status_for_ingreso(ingreso_id)
    return {
        "ingreso_id": ingreso_id,
        "remito_number": status.get("remito_number") or "",
        "comprobante_tipo": status.get("comprobante_tipo") or "",
        "pdf_status": status.get("pdf_status") or "pending",
        **_ris_urls(ingreso_id),
        **extra,
    }


def _ris_urls(ingreso_id: int) -> dict[str, str]:
    return {
        "pdf_url": f"/api/ingresos/{ingreso_id}/ris/pdf/",
        "print_url": f"/api/ingresos/{ingreso_id}/ris/print/",
    }


def _ris_payload(ingreso_id: int, row_or_status: dict | None = None) -> dict:
    ris = serialize_ris_row(row_or_status) if row_or_status else get_ris_status_for_ingreso(ingreso_id)
    return {
        "ris": ris,
        "remito_number": ris.get("remito_number") or "",
        "document_mode": ris.get("document_mode") or "emit",
        "pdf_status": ris.get("pdf_status") or "pending",
        "retry_after_ms": 2500,
        **_ris_urls(ingreso_id),
    }


def _require_ris_print_permissions(request):
    require_roles(request, ["jefe", "jefe_veedor", "admin", "recepcion"])
    require_any_permission(
        request,
        ["action.ingreso.emit_ingress_order", "action.ingreso.create", "page.new_ingreso"],
    )


class IngresoRisStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ingreso_id: int):
        require_roles(request, ["tecnico", "jefe", "jefe_veedor", "admin", "recepcion"])
        return Response(_ris_payload(ingreso_id))


class IngresoRisPreflightPayloadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        _require_ris_print_permissions(request)
        return Response(preflight_ris_for_request_payload(request.data or {}, user_id=_actor_id(request)))


class IngresoRisPreflightView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, ingreso_id: int):
        _require_ris_print_permissions(request)
        return Response(preflight_ris_for_ingreso(ingreso_id, user_id=_actor_id(request)))


def _require_ris_preflight_fix_permissions(request):
    require_roles(request, ["jefe", "jefe_veedor", "admin", "recepcion"])
    require_any_permission(request, ["action.ingreso.fix_ris_preflight", "action.bejerman_sync.manage"])


class IngresoRisPreflightCustomerFixView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        _require_ris_preflight_fix_permissions(request)
        data = request.data or {}
        customer_id = data.get("customer_id") or data.get("customerId")
        customer_code = data.get("customer_code") or data.get("customerCode") or data.get("code")
        company_key = data.get("company_key") or data.get("companyKey") or ""
        try:
            result = apply_customer_fix_from_bejerman(
                int(customer_id or 0),
                customer_code or "",
                company_key=company_key,
                user_id=_actor_id(request),
            )
        except BejermanSdkConfigError as exc:
            return Response({"detail": str(exc)}, status=503)
        except (BejermanSdkResponseError, BejermanSdkUnavailable) as exc:
            return Response({"detail": str(exc)}, status=502)
        except BejermanRisError as exc:
            return Response({"detail": str(exc)}, status=409)
        return Response({"ok": True, **result})


class IngresoRisPreflightArticleFixView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        _require_ris_preflight_fix_permissions(request)
        data = request.data or {}
        user_id = _actor_id(request)
        try:
            result = apply_article_fix_from_bejerman(
                model_id=int(data.get("model_id") or data.get("modelId") or 0),
                variante=data.get("variante") or data.get("variant") or "",
                article_code=data.get("article_code") or data.get("articleCode") or "",
                article_description=data.get("article_description") or data.get("articleDescription") or "",
                user_id=user_id,
                company_key=data.get("company_key") or data.get("companyKey") or "",
            )
        except BejermanSdkConfigError as exc:
            return Response({"detail": str(exc)}, status=503)
        except (BejermanSdkResponseError, BejermanSdkUnavailable) as exc:
            return Response({"detail": str(exc)}, status=502)
        except BejermanRisError as exc:
            return Response({"detail": str(exc)}, status=409)
        return Response({"ok": True, **result})


class IngresoRisEmitirView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, ingreso_id: int):
        _require_ris_print_permissions(request)
        _set_audit_user(request)
        user_id = _actor_id(request)
        force_reviewed_reissue = request.data.get("forceReviewedReissue", False) if isinstance(request.data, dict) else False
        try:
            row = emit_or_get_ris(ingreso_id, user_id=user_id, force_reviewed_reissue=force_reviewed_reissue)
        except BejermanRisPreflightError as exc:
            return Response({**exc.payload, **_ris_payload(ingreso_id)}, status=409)
        except BejermanRisBusyError as exc:
            return Response({"detail": str(exc), **_ris_payload(ingreso_id)}, status=409)
        except BejermanRisReviewRequiredError as exc:
            return Response({"detail": str(exc), "code": exc.code, **_ris_payload(ingreso_id)}, status=409)
        except BejermanSdkConfigError as exc:
            return Response({"detail": str(exc), **_ris_payload(ingreso_id)}, status=503)
        except BejermanRisError as exc:
            return Response({"detail": str(exc), **_ris_payload(ingreso_id)}, status=502)
        return Response(_ris_payload(ingreso_id, row))


class IngresoRisPdfView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ingreso_id: int):
        _require_ris_print_permissions(request)
        try:
            pdf_bytes, content_type, row = fetch_ris_pdf(ingreso_id, user_id=_actor_id(request))
        except BejermanRisPdfPendingError as exc:
            retry_after_ms = getattr(exc, "retry_after_ms", 2500)
            extra_payload = getattr(exc, "payload", None)
            response = Response(
                {
                    "detail": str(exc),
                    "code": getattr(exc, "code", "BEJERMAN_PDF_PENDING"),
                    **_ris_payload(ingreso_id),
                    **(extra_payload if isinstance(extra_payload, dict) else {}),
                    "retry_after_ms": retry_after_ms,
                },
                status=202,
            )
            response["Retry-After"] = retry_after_header_value(retry_after_ms)
            return response
        except BejermanRisPdfError as exc:
            extra_payload = getattr(exc, "payload", None)
            return Response(
                {
                    "detail": str(exc),
                    "code": getattr(exc, "code", "BEJERMAN_RIS_PDF_ERROR"),
                    **_ris_payload(ingreso_id),
                    **(extra_payload if isinstance(extra_payload, dict) else {}),
                },
                status=getattr(exc, "status_code", 502),
            )
        except BejermanRisReviewRequiredError as exc:
            return Response({"detail": str(exc), "code": exc.code, **_ris_payload(ingreso_id)}, status=409)
        except BejermanRisRegisteredNoPdfError as exc:
            return Response({"detail": str(exc), "code": exc.code, **_ris_payload(ingreso_id)}, status=409)
        except BejermanRisError as exc:
            return Response({"detail": str(exc), **_ris_payload(ingreso_id)}, status=409)
        filename = f"RIS-{row.get('remito_number') or os_label(ingreso_id)}.pdf".replace("/", "-")
        return _pdf_response(pdf_bytes, filename, content_type)


class IngresoRisLotePdfView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        _require_ris_print_permissions(request)
        try:
            requested_ids = _parse_ris_lote_ingreso_ids(request.query_params.get("ids"))
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)

        actor_id = _actor_id(request)
        documents: list[bytes] = []
        remitos: list[dict] = []
        pending_remitos: list[dict] = []
        failed_remitos: list[tuple[int, dict]] = []
        seen_documents: set[tuple[str, str | int]] = set()
        retry_after_ms = 2500

        for ingreso_id in requested_ids:
            try:
                ris = get_ris_status_for_ingreso(ingreso_id)
            except BejermanRisError as exc:
                failed = _ris_lote_document(
                    ingreso_id,
                    {},
                    state="failed",
                    code=getattr(exc, "code", "BEJERMAN_RIS_PDF_ERROR"),
                    detail=str(exc),
                )
                remitos.append(failed)
                failed_remitos.append((409, failed))
                continue

            remito_number = str(ris.get("remito_number") or "").strip()
            document_key: tuple[str, str | int] = (
                ("remito", remito_number.upper()) if remito_number else ("ingreso", ingreso_id)
            )
            if document_key in seen_documents:
                continue
            seen_documents.add(document_key)

            try:
                pdf_bytes, _content_type, row = fetch_ris_pdf(ingreso_id, user_id=actor_id)
            except BejermanRisPdfPendingError as exc:
                retry_after_ms = max(retry_after_ms, int(getattr(exc, "retry_after_ms", 2500) or 2500))
                pending_extra = dict(getattr(exc, "payload", None) or {})
                pending_extra.update(
                    {
                        "state": "pending",
                        "code": getattr(exc, "code", "BEJERMAN_PDF_PENDING"),
                        "detail": str(exc),
                    }
                )
                pending = _ris_lote_document(ingreso_id, ris, **pending_extra)
                remitos.append(pending)
                pending_remitos.append(pending)
            except BejermanRisPdfError as exc:
                failed_extra = dict(getattr(exc, "payload", None) or {})
                failed_extra.update(
                    {
                        "state": "failed",
                        "code": getattr(exc, "code", "BEJERMAN_RIS_PDF_ERROR"),
                        "detail": str(exc),
                    }
                )
                failed = _ris_lote_document(ingreso_id, ris, **failed_extra)
                remitos.append(failed)
                failed_remitos.append((int(getattr(exc, "status_code", 502) or 502), failed))
            except (BejermanRisRegisteredNoPdfError, BejermanRisReviewRequiredError, BejermanRisError) as exc:
                failed = _ris_lote_document(
                    ingreso_id,
                    ris,
                    state="failed",
                    code=getattr(exc, "code", "BEJERMAN_RIS_PDF_ERROR"),
                    detail=str(exc),
                )
                remitos.append(failed)
                failed_remitos.append((409, failed))
            else:
                ready_ris = serialize_ris_row(row)
                ready = _ris_lote_document(ingreso_id, ready_ris, state="ready", pdf_status="ready")
                remitos.append(ready)
                documents.append(pdf_bytes)

        if failed_remitos:
            response_status, first_failure = failed_remitos[0]
            return Response(
                {
                    "detail": first_failure.get("detail") or "No se pudo obtener uno de los PDFs del lote.",
                    "code": "BEJERMAN_RIS_BATCH_PDF_ERROR",
                    "total": len(remitos),
                    "ready_count": sum(item.get("state") == "ready" for item in remitos),
                    "failed_remitos": [item for _status, item in failed_remitos],
                    "remitos": remitos,
                },
                status=response_status,
            )

        if pending_remitos:
            ready_count = sum(item.get("state") == "ready" for item in remitos)
            total = len(remitos)
            pdf_word = "PDF listo" if total == 1 else "PDFs listos"
            response = Response(
                {
                    "detail": "Los remitos ya están emitidos. NEXORA volverá a comprobar los PDFs pendientes.",
                    "code": "BEJERMAN_RIS_BATCH_PDF_PENDING",
                    "pdf_state": "pending_documents",
                    "stage_label": f"{ready_count} de {total} {pdf_word}",
                    "ready_count": ready_count,
                    "pending_count": len(pending_remitos),
                    "total": total,
                    "pending_remitos": pending_remitos,
                    "remitos": remitos,
                    "retry_after_ms": retry_after_ms,
                },
                status=202,
            )
            response["Retry-After"] = retry_after_header_value(retry_after_ms)
            return response

        try:
            merged_pdf = _merge_pdf_documents(documents)
        except Exception:
            return Response(
                {
                    "detail": "Los PDFs están disponibles, pero NEXORA no pudo fusionarlos.",
                    "code": "BEJERMAN_RIS_BATCH_PDF_MERGE_ERROR",
                    "remitos": remitos,
                },
                status=502,
            )
        filename = f"Remitos-ingreso-{len(documents)}.pdf"
        response = _pdf_response(merged_pdf, filename)
        response["Cache-Control"] = "no-store"
        return response


class IngresoRisLotePrintView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        _require_ris_print_permissions(request)
        try:
            ingreso_ids = _parse_ris_lote_ingreso_ids(request.query_params.get("ids"))
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)

        count = len(ingreso_ids)
        document_name = "remito de ingreso" if count == 1 else f"lote de {count} remitos de ingreso"
        html = render_bejerman_pdf_wait_page(
            pdf_url=_ris_lote_pdf_url(ingreso_ids),
            document_name=document_name,
            title="Remitos de ingreso",
            fallback_detail="Puede reintentar o volver a Nuevo ingreso.",
        )
        return HttpResponse(
            html,
            content_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )


def _ingreso_remito_document_type(ris: dict | None) -> str:
    if not isinstance(ris, dict):
        return ""
    profile = ris.get("document_profile") or ris.get("documentProfile")
    if isinstance(profile, dict):
        document_type = profile.get("type")
    else:
        document_type = None
    return str(document_type or ris.get("comprobante_tipo") or "").strip().upper()


def _ris_print_wait_page(ingreso_id: int, ris: dict | None = None) -> str:
    pdf_url = f"/api/ingresos/{ingreso_id}/ris/pdf/"
    clean_type = _ingreso_remito_document_type(ris)
    document_name = f"remito {clean_type}" if clean_type else "remito"
    return render_bejerman_pdf_wait_page(
        pdf_url=pdf_url,
        document_name=document_name,
        title="Remito",
        fallback_detail="Puede reintentar o volver a la hoja de servicio.",
    )


class IngresoRisPrintView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ingreso_id: int):
        _require_ris_print_permissions(request)
        ris = get_ris_status_for_ingreso(ingreso_id)
        if (ris.get("document_mode") or "emit") == "register":
            exc = BejermanRisRegisteredNoPdfError(ris.get("remito_number") or ris.get("manual_remito_number"))
            return Response(
                {
                    "detail": str(exc),
                    "code": exc.code,
                    **_ris_payload(ingreso_id, ris),
                },
                status=409,
            )
        return HttpResponse(
            _ris_print_wait_page(ingreso_id, ris),
            content_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )


class SerialBarcodePdfView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_roles(request, ["tecnico", "jefe", "jefe_veedor", "admin", "recepcion"])
        require_any_permission(
            request,
            ["action.ingreso.print_barcode", "action.ingreso.create", "page.new_ingreso", "action.devices_preventivos.manage"],
        )
        value = (request.GET.get("value") or request.GET.get("serial") or "").strip()
        raw_title = request.GET.get("title")
        title = "N/S" if raw_title is None else raw_title.strip()
        subtitle = (request.GET.get("subtitle") or "").strip()
        pdf_bytes, filename = render_serial_barcode_pdf(value, title=title, subtitle=subtitle)
        if not pdf_bytes:
            return Response({"detail": "Número de serie requerido"}, status=400)
        return _pdf_response(pdf_bytes, filename)


class IngresoBarcodePdfView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ingreso_id: int):
        require_roles(request, ["tecnico", "jefe", "jefe_veedor", "admin", "recepcion"])
        require_any_permission(
            request,
            ["action.ingreso.print_barcode", "action.ingreso.create", "page.new_ingreso", "action.devices_preventivos.manage"],
        )
        row = q(
            """
            SELECT
              COALESCE(d.numero_serie, '') AS numero_serie,
              COALESCE(d.numero_interno, '') AS numero_interno,
              COALESCE(b.nombre, '') AS marca,
              COALESCE(m.nombre, '') AS modelo
            FROM ingresos t
            JOIN devices d ON d.id = t.device_id
            LEFT JOIN marcas b ON b.id = d.marca_id
            LEFT JOIN models m ON m.id = d.model_id
            WHERE t.id = %s
            """,
            [ingreso_id],
            one=True,
        )
        if not row:
            return Response({"detail": "Ingreso no encontrado"}, status=404)
        value = (row.get("numero_serie") or row.get("numero_interno") or "").strip()
        subtitle = " ".join(part for part in [row.get("marca"), row.get("modelo"), os_label(ingreso_id)] if part)
        pdf_bytes, filename = render_serial_barcode_pdf(value, title="", subtitle=subtitle)
        if not pdf_bytes:
            return Response({"detail": "El ingreso no tiene número de serie ni número interno"}, status=400)
        return _pdf_response(pdf_bytes, filename)


__all__ = [
    "IngresoRisStatusView",
    "IngresoRisEmitirView",
    "IngresoRisPdfView",
    "IngresoRisPrintView",
    "SerialBarcodePdfView",
    "IngresoBarcodePdfView",
]
