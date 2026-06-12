from __future__ import annotations

import json
from html import escape

from django.http import HttpResponse
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from ..bejerman_ris import (
    BejermanRisBusyError,
    BejermanRisError,
    BejermanRisPdfError,
    BejermanRisPdfPendingError,
    emit_or_get_ris,
    fetch_ris_pdf,
    get_ris_status_for_ingreso,
    serialize_ris_row,
)
from ..bejerman_sdk import BejermanSdkConfigError
from ..pdf import render_serial_barcode_pdf
from ..permissions import require_any_permission
from .helpers import _set_audit_user, os_label, q, require_roles


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


class IngresoRisEmitirView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, ingreso_id: int):
        _require_ris_print_permissions(request)
        _set_audit_user(request)
        user_id = getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)
        try:
            row = emit_or_get_ris(ingreso_id, user_id=user_id)
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
            pdf_bytes, content_type, row = fetch_ris_pdf(ingreso_id)
        except BejermanRisPdfPendingError as exc:
            retry_after_ms = getattr(exc, "retry_after_ms", 2500)
            response = Response(
                {"detail": str(exc), **_ris_payload(ingreso_id), "retry_after_ms": retry_after_ms},
                status=202,
            )
            response["Retry-After"] = str(max(1, (int(retry_after_ms) + 999) // 1000))
            return response
        except BejermanRisPdfError as exc:
            return Response({"detail": str(exc), **_ris_payload(ingreso_id)}, status=502)
        except BejermanRisError as exc:
            return Response({"detail": str(exc), **_ris_payload(ingreso_id)}, status=409)
        filename = f"RIS-{row.get('remito_number') or os_label(ingreso_id)}.pdf".replace("/", "-")
        return _pdf_response(pdf_bytes, filename, content_type)


def _ris_print_wait_page(ingreso_id: int) -> str:
    pdf_url = f"/api/ingresos/{ingreso_id}/ris/pdf/"
    return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>RIS</title>
  <style>
    body {{ margin: 0; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f8fafc; color: #111827; }}
    main {{ max-width: 680px; margin: 72px auto; padding: 32px; background: white; border: 1px solid #d1d5db; border-radius: 8px; box-shadow: 0 18px 50px rgba(17, 24, 39, .08); }}
    h1 {{ margin: 0 0 12px; font-size: 24px; }}
    p {{ line-height: 1.55; }}
    .actions {{ display: none; gap: 12px; flex-wrap: wrap; margin-top: 20px; }}
    .actions.visible {{ display: flex; }}
    button, a {{ border: 1px solid #9ca3af; border-radius: 8px; background: #111827; color: white; padding: 10px 14px; font: inherit; text-decoration: none; cursor: pointer; }}
    a {{ background: white; color: #111827; }}
  </style>
</head>
<body>
  <main>
    <h1>Preparando RIS</h1>
    <p id="status">Esperando el PDF emitido por Bejerman...</p>
    <div id="actions" class="actions">
      <button id="retry" type="button">Reintentar</button>
      <a href="{escape(pdf_url)}" target="_self">Abrir PDF directo</a>
    </div>
  </main>
  <script>
    const pdfUrl = {json.dumps(pdf_url)};
    const maxAttempts = 18;
    const statusEl = document.getElementById('status');
    const actionsEl = document.getElementById('actions');
    const retryEl = document.getElementById('retry');
    let attempts = 0;

    function isPdf(bytes) {{
      return bytes.length >= 5 && bytes[0] === 37 && bytes[1] === 80 && bytes[2] === 68 && bytes[3] === 70 && bytes[4] === 45;
    }}

    function fail(message) {{
      statusEl.textContent = message;
      actionsEl.classList.add('visible');
    }}

    async function pollPdf() {{
      attempts += 1;
      actionsEl.classList.remove('visible');
      statusEl.textContent = attempts === 1
        ? 'Buscando el PDF emitido por Bejerman...'
        : 'El RIS ya fue emitido. Esperando PDF (' + attempts + '/' + maxAttempts + ')...';

      try {{
        const separator = pdfUrl.includes('?') ? '&' : '?';
        const response = await fetch(pdfUrl + separator + 't=' + Date.now(), {{
          cache: 'no-store',
          credentials: 'same-origin',
        }});
        const buffer = await response.arrayBuffer();
        const bytes = new Uint8Array(buffer);
        if (response.ok && isPdf(bytes)) {{
          const blob = new Blob([bytes], {{ type: 'application/pdf' }});
          window.location.replace(URL.createObjectURL(blob));
          return;
        }}
        if (response.status === 401 || response.status === 403) {{
          fail('Tu sesión no tiene permiso para ver este RIS.');
          return;
        }}
        if (response.status === 409) {{
          fail('El RIS todavía no está emitido o no tiene referencia de comprobante.');
          return;
        }}
      }} catch {{
        // Bejerman puede tardar unos segundos en exponer el PDF.
      }}

      if (attempts >= maxAttempts) {{
        fail('El RIS fue emitido, pero el PDF todavía no está listo. Reintente en unos segundos.');
        return;
      }}
      window.setTimeout(pollPdf, Math.min(1200 + attempts * 600, 4500));
    }}

    retryEl.addEventListener('click', () => {{
      attempts = 0;
      void pollPdf();
    }});
    void pollPdf();
  </script>
</body>
</html>"""


class IngresoRisPrintView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ingreso_id: int):
        _require_ris_print_permissions(request)
        return HttpResponse(
            _ris_print_wait_page(ingreso_id),
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
    "IngresoRisPdfView",
    "IngresoRisPrintView",
    "SerialBarcodePdfView",
    "IngresoBarcodePdfView",
]
