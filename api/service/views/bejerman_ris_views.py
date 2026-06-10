from __future__ import annotations

from django.http import HttpResponse
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from ..bejerman_bridge import BejermanBridgeConfigError
from ..bejerman_ris import (
    BejermanRisBusyError,
    BejermanRisError,
    BejermanRisPdfError,
    emit_or_fetch_ris_pdf,
    get_ris_status_for_ingreso,
)
from ..pdf import render_serial_barcode_pdf
from ..permissions import require_any_permission
from .helpers import _set_audit_user, os_label, q, require_roles


def _pdf_response(pdf_bytes: bytes, filename: str, content_type: str = "application/pdf") -> HttpResponse:
    response = HttpResponse(pdf_bytes, content_type=content_type or "application/pdf")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


class IngresoRisStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ingreso_id: int):
        require_roles(request, ["tecnico", "jefe", "jefe_veedor", "admin", "recepcion"])
        return Response(get_ris_status_for_ingreso(ingreso_id))


class IngresoRisEmitirView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, ingreso_id: int):
        require_roles(request, ["jefe", "jefe_veedor", "admin", "recepcion"])
        require_any_permission(
            request,
            ["action.ingreso.emit_ingress_order", "action.ingreso.create", "page.new_ingreso"],
        )
        _set_audit_user(request)
        user_id = getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)
        try:
            pdf_bytes, content_type, row = emit_or_fetch_ris_pdf(ingreso_id, user_id=user_id)
        except BejermanRisBusyError as exc:
            return Response({"detail": str(exc), "ris": get_ris_status_for_ingreso(ingreso_id)}, status=409)
        except BejermanBridgeConfigError as exc:
            return Response({"detail": str(exc), "ris": get_ris_status_for_ingreso(ingreso_id)}, status=503)
        except BejermanRisPdfError as exc:
            return Response({"detail": str(exc), "ris": get_ris_status_for_ingreso(ingreso_id)}, status=502)
        except BejermanRisError as exc:
            return Response({"detail": str(exc), "ris": get_ris_status_for_ingreso(ingreso_id)}, status=502)
        filename = f"RIS-{row.get('remito_number') or os_label(ingreso_id)}.pdf".replace("/", "-")
        return _pdf_response(pdf_bytes, filename, content_type)


class SerialBarcodePdfView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_roles(request, ["tecnico", "jefe", "jefe_veedor", "admin", "recepcion"])
        require_any_permission(
            request,
            ["action.ingreso.print_barcode", "action.ingreso.create", "page.new_ingreso", "action.devices_preventivos.manage"],
        )
        value = (request.GET.get("value") or request.GET.get("serial") or "").strip()
        title = (request.GET.get("title") or "N/S").strip()
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
        pdf_bytes, filename = render_serial_barcode_pdf(value, title="Equipo", subtitle=subtitle)
        if not pdf_bytes:
            return Response({"detail": "El ingreso no tiene número de serie ni número interno"}, status=400)
        return _pdf_response(pdf_bytes, filename)


__all__ = [
    "IngresoRisStatusView",
    "IngresoRisEmitirView",
    "SerialBarcodePdfView",
    "IngresoBarcodePdfView",
]
