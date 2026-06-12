from __future__ import annotations

import json
from html import escape

from django.http import HttpResponse
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from ..permissions import MappedPermissionGuard
from ..pdf import render_remito_salida_pdf
from ..bejerman_delivery import (
    BillingError,
    generate_bejerman_remito,
    get_facturacion_pdf,
    get_remito_group_pdf,
    list_bejerman_articles,
    list_facturacion_company_options,
    list_facturacion_from_bejerman,
)
from ..delivery_orders import (
    DeliveryOrderError,
    cancel_order,
    create_delivery_order,
    get_delivery_order,
    list_delivery_orders,
    list_remito_history,
    mark_delivered,
    mark_invoiced,
    mark_prepared,
    update_item_article,
    update_item_partidas,
    update_remito_location,
)


def _actor_id(request):
    return getattr(getattr(request, "user", None), "id", None)


def _error_response(exc: Exception):
    if isinstance(exc, (DeliveryOrderError, BillingError)):
        retry_after_ms = getattr(exc, "retry_after_ms", None)
        payload = {"detail": str(exc), "code": exc.code}
        if retry_after_ms:
            payload["retry_after_ms"] = retry_after_ms
        response = Response(
            payload,
            status=getattr(exc, "status_code", 400) or 400,
        )
        if retry_after_ms:
            response["Retry-After"] = str(max(1, (int(retry_after_ms) + 999) // 1000))
        return response
    raise exc


def _order_ingreso_id(order: dict) -> int | None:
    ingreso_id = order.get("ingresoId")
    if ingreso_id:
        try:
            return int(ingreso_id)
        except (TypeError, ValueError):
            return None
    source_reference = str(order.get("sourceReference") or "").strip()
    digits = "".join(char for char in source_reference if char.isdigit())
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


class DeliveryOrderPermissionMixin:
    permission_classes = [permissions.IsAuthenticated, MappedPermissionGuard]


class DeliveryOrdersView(DeliveryOrderPermissionMixin, APIView):

    def get(self, request):
        try:
            return Response(list_delivery_orders(request.query_params))
        except Exception as exc:
            return _error_response(exc)

    def post(self, request):
        try:
            return Response(
                create_delivery_order(request.data or {}, _actor_id(request)),
                status=status.HTTP_201_CREATED,
            )
        except Exception as exc:
            return _error_response(exc)


class DeliveryOrderDetailView(DeliveryOrderPermissionMixin, APIView):

    def get(self, request, order_id):
        try:
            return Response(get_delivery_order(order_id))
        except Exception as exc:
            return _error_response(exc)


class DeliveryOrderExitRemitoPdfView(DeliveryOrderPermissionMixin, APIView):

    def get(self, request, order_id):
        try:
            order = get_delivery_order(order_id, include_events=False)
        except Exception as exc:
            return _error_response(exc)
        if order.get("deliveryType") != "service_release":
            return Response({"detail": "La orden no corresponde a una salida de servicio técnico."}, status=409)
        ingreso_id = _order_ingreso_id(order)
        if not ingreso_id:
            return Response({"detail": "La orden no tiene un ingreso técnico vinculado."}, status=404)
        pdf_bytes, filename = render_remito_salida_pdf(
            ingreso_id,
            printed_by=getattr(getattr(request, "user", None), "nombre", ""),
        )
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="{filename}"'
        return response


class DeliveryOrderPreparedView(DeliveryOrderPermissionMixin, APIView):

    def post(self, request, order_id):
        try:
            return Response(mark_prepared(order_id, _actor_id(request), (request.data or {}).get("remitoNumber")))
        except Exception as exc:
            return _error_response(exc)


class DeliveryOrderDeliveredView(DeliveryOrderPermissionMixin, APIView):

    def post(self, request, order_id):
        try:
            return Response(mark_delivered(order_id, _actor_id(request), (request.data or {}).get("remitoNumber")))
        except Exception as exc:
            return _error_response(exc)


class DeliveryOrderInvoicedView(DeliveryOrderPermissionMixin, APIView):

    def post(self, request, order_id):
        try:
            return Response(mark_invoiced(order_id, _actor_id(request), (request.data or {}).get("invoiceNumber")))
        except Exception as exc:
            return _error_response(exc)


class DeliveryOrderCancelView(DeliveryOrderPermissionMixin, APIView):

    def post(self, request, order_id):
        try:
            return Response(cancel_order(order_id, _actor_id(request), (request.data or {}).get("note")))
        except Exception as exc:
            return _error_response(exc)


class DeliveryOrderRemitoLocationView(DeliveryOrderPermissionMixin, APIView):

    def patch(self, request, order_id):
        try:
            return Response(update_remito_location(order_id, _actor_id(request), (request.data or {}).get("remitoLocation")))
        except Exception as exc:
            return _error_response(exc)


class DeliveryOrderItemArticleView(DeliveryOrderPermissionMixin, APIView):

    def patch(self, request, order_id, item_id):
        try:
            return Response(update_item_article(order_id, item_id, _actor_id(request), request.data or {}))
        except Exception as exc:
            return _error_response(exc)


class DeliveryOrderItemPartidasView(DeliveryOrderPermissionMixin, APIView):

    def patch(self, request, order_id, item_id):
        payload = request.data or {}
        partidas = payload.get("partidas") if isinstance(payload, dict) else None
        try:
            return Response(update_item_partidas(order_id, item_id, _actor_id(request), partidas or []))
        except Exception as exc:
            return _error_response(exc)


class DeliveryOrderBejermanRemitoView(DeliveryOrderPermissionMixin, APIView):

    def post(self, request):
        payload = request.data or {}
        try:
            return Response(generate_bejerman_remito(payload.get("orderIds") or [], _actor_id(request), payload))
        except Exception as exc:
            return _error_response(exc)


class DeliveryOrderBejermanArticlesView(DeliveryOrderPermissionMixin, APIView):

    def get(self, request):
        try:
            return Response(
                list_bejerman_articles(
                    request.query_params.get("q") or request.query_params.get("search"),
                    request.query_params.get("limit") or 20,
                )
            )
        except Exception:
            return Response({"items": [], "unavailable": True})


class DeliveryOrderBejermanRemitoHistoryView(DeliveryOrderPermissionMixin, APIView):

    def get(self, request):
        return Response({"items": list_remito_history(request.query_params.get("limit") or 20)})


class DeliveryOrderBejermanRemitoPdfView(DeliveryOrderPermissionMixin, APIView):

    def get(self, request, group_id):
        try:
            bytes_, content_type, filename = get_remito_group_pdf(group_id)
        except Exception as exc:
            return _error_response(exc)
        response = HttpResponse(bytes_, content_type=content_type or "application/pdf")
        response["Content-Disposition"] = f'inline; filename="{filename}"'
        return response


def _remito_print_wait_page(group_id: str) -> str:
    pdf_url = f"/api/ordenes-entrega/remito-bejerman/{group_id}/pdf/"
    return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Remito Bejerman</title>
  <style>
    body {{ margin: 0; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f8f3; color: #10200f; }}
    main {{ max-width: 680px; margin: 72px auto; padding: 32px; background: white; border: 1px solid #c8d1c1; border-radius: 8px; box-shadow: 0 18px 50px rgba(24, 43, 20, .08); }}
    h1 {{ margin: 0 0 12px; font-size: 24px; }}
    p {{ line-height: 1.55; }}
    .actions {{ display: none; gap: 12px; flex-wrap: wrap; margin-top: 20px; }}
    .actions.visible {{ display: flex; }}
    button, a {{ border: 1px solid #9aa894; border-radius: 8px; background: #10200f; color: white; padding: 10px 14px; font: inherit; text-decoration: none; cursor: pointer; }}
    a {{ background: white; color: #10200f; }}
  </style>
</head>
<body>
  <main>
    <h1>Preparando remito</h1>
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
        : 'El remito ya fue emitido. Esperando PDF (' + attempts + '/' + maxAttempts + ')...';

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
          fail('Tu sesión no tiene permiso para ver este remito.');
          return;
        }}
      }} catch {{
        // Bejerman puede tardar unos segundos en exponer el PDF.
      }}

      if (attempts >= maxAttempts) {{
        fail('El remito fue emitido, pero el PDF todavía no está listo. Reintente en unos segundos.');
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


class DeliveryOrderBejermanRemitoPrintView(DeliveryOrderPermissionMixin, APIView):

    def get(self, request, group_id):
        normalized_group_id = str(group_id or "").strip()
        if not normalized_group_id:
            return Response({"detail": "Remito inválido."}, status=400)
        return HttpResponse(
            _remito_print_wait_page(normalized_group_id),
            content_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )


class FacturacionCompanyOptionsView(DeliveryOrderPermissionMixin, APIView):

    def get(self, request):
        return Response({"items": list_facturacion_company_options()})


class FacturacionClienteDocumentosView(DeliveryOrderPermissionMixin, APIView):

    def get(self, request):
        customer_code = request.query_params.get("customerCode") or request.query_params.get("clienteCodigo")
        try:
            return Response(list_facturacion_from_bejerman(customer_code, request.query_params))
        except Exception as exc:
            return _error_response(exc)


class FacturacionDocumentoPdfView(DeliveryOrderPermissionMixin, APIView):

    def get(self, request, document_id):
        customer_code = request.query_params.get("customerCode") or request.query_params.get("clienteCodigo")
        try:
            bytes_, content_type = get_facturacion_pdf(customer_code, document_id)
        except Exception as exc:
            return _error_response(exc)
        response = HttpResponse(bytes_, content_type=content_type or "application/pdf")
        response["Content-Disposition"] = f'inline; filename="facturacion-{document_id}.pdf"'
        return response


__all__ = [
    "DeliveryOrdersView",
    "DeliveryOrderDetailView",
    "DeliveryOrderExitRemitoPdfView",
    "DeliveryOrderPreparedView",
    "DeliveryOrderDeliveredView",
    "DeliveryOrderInvoicedView",
    "DeliveryOrderCancelView",
    "DeliveryOrderRemitoLocationView",
    "DeliveryOrderItemArticleView",
    "DeliveryOrderItemPartidasView",
    "DeliveryOrderBejermanRemitoView",
    "DeliveryOrderBejermanRemitoHistoryView",
    "DeliveryOrderBejermanArticlesView",
    "DeliveryOrderBejermanRemitoPdfView",
    "DeliveryOrderBejermanRemitoPrintView",
    "FacturacionCompanyOptionsView",
    "FacturacionClienteDocumentosView",
    "FacturacionDocumentoPdfView",
]
