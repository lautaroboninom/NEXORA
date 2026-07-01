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


def _actor_id(request):
    return getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)


def _pdf_response(pdf_bytes: bytes, filename: str, content_type: str = "application/pdf") -> HttpResponse:
    response = HttpResponse(pdf_bytes, content_type=content_type or "application/pdf")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


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
        try:
            row = emit_or_get_ris(ingreso_id, user_id=user_id)
        except BejermanRisPreflightError as exc:
            return Response({**exc.payload, **_ris_payload(ingreso_id)}, status=409)
        except BejermanRisBusyError as exc:
            return Response({"detail": str(exc), **_ris_payload(ingreso_id)}, status=409)
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
            response = Response(
                {"detail": str(exc), **_ris_payload(ingreso_id), "retry_after_ms": retry_after_ms},
                status=202,
            )
            response["Retry-After"] = retry_after_header_value(retry_after_ms)
            return response
        except BejermanRisPdfError as exc:
            return Response({"detail": str(exc), **_ris_payload(ingreso_id)}, status=502)
        except BejermanRisRegisteredNoPdfError as exc:
            return Response({"detail": str(exc), "code": exc.code, **_ris_payload(ingreso_id)}, status=409)
        except BejermanRisError as exc:
            return Response({"detail": str(exc), **_ris_payload(ingreso_id)}, status=409)
        filename = f"RIS-{row.get('remito_number') or os_label(ingreso_id)}.pdf".replace("/", "-")
        return _pdf_response(pdf_bytes, filename, content_type)


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
