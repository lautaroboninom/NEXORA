from __future__ import annotations

import json
from html import escape

from django.db import connection
from django.http import HttpResponse
from rest_framework.exceptions import PermissionDenied
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from ..drive_delivery_sync import DriveDeliverySyncError, sync_delivery_orders_to_drive
from ..permissions import MappedPermissionGuard
from ..pdf import render_remito_salida_pdf
from ..bejerman_delivery import (
    BillingError,
    generate_bejerman_remito,
    get_delivery_order_invoice_pdf,
    get_facturacion_pdf,
    get_remito_group_pdf,
    list_bejerman_article_stock,
    list_bejerman_articles,
    list_bejerman_depositos,
    list_facturacion_company_options,
    list_facturacion_from_bejerman,
    list_rental_available_equipment,
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
    update_delivery_order,
    update_item_article,
    update_item_partidas,
    update_remito_location,
)
from ..service_order_billing import (
    ServiceOrderBillingError,
    list_service_orders_to_bill,
    register_service_order_invoice,
    render_service_order_billing_pdf,
)


def _actor_id(request):
    return getattr(getattr(request, "user", None), "id", None)


def _error_response(exc: Exception):
    if isinstance(exc, (DeliveryOrderError, BillingError, ServiceOrderBillingError)):
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


class DeliveryOrderDriveSyncView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        role = str(getattr(request.user, "rol", "") or "").strip().lower()
        if role != "admin" and role != "ventas":
            raise PermissionDenied("Solo admin o ventas pueden sincronizar órdenes de entrega con Google Drive.")
        try:
            return Response(sync_delivery_orders_to_drive())
        except DriveDeliverySyncError as exc:
            return Response(
                {"detail": str(exc), "code": exc.code},
                status=exc.status_code,
            )


class DeliveryOrderDetailView(DeliveryOrderPermissionMixin, APIView):

    def get(self, request, order_id):
        try:
            return Response(get_delivery_order(order_id))
        except Exception as exc:
            return _error_response(exc)

    def patch(self, request, order_id):
        try:
            return Response(update_delivery_order(order_id, request.data or {}, _actor_id(request)))
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
                    actor_user_id=_actor_id(request),
                    company_key=request.query_params.get("companyKey") or request.query_params.get("company_key"),
                )
            )
        except Exception:
            return Response({"items": [], "unavailable": True})


class DeliveryOrderBejermanDepositsView(DeliveryOrderPermissionMixin, APIView):

    def get(self, request):
        return Response(
            list_bejerman_depositos(
                request.query_params.get("companyKey") or request.query_params.get("company_key"),
                actor_user_id=_actor_id(request),
            )
        )


class DeliveryOrderBejermanArticleStockView(DeliveryOrderPermissionMixin, APIView):

    def get(self, request):
        article_code = (request.query_params.get("articleCode") or request.query_params.get("article_code") or "").strip()
        deposit_code = (request.query_params.get("depositCode") or request.query_params.get("deposit_code") or "").strip()
        if not article_code:
            return Response({"detail": "Código de artículo requerido", "code": "ARTICLE_CODE_REQUIRED"}, status=400)
        try:
            return Response(
                list_bejerman_article_stock(
                    article_code,
                    request.query_params.get("limit") or 100,
                    actor_user_id=_actor_id(request),
                    company_key=request.query_params.get("companyKey") or request.query_params.get("company_key"),
                    deposit_code=deposit_code,
                    delivery_type=request.query_params.get("deliveryType") or request.query_params.get("delivery_type"),
                )
            )
        except Exception:
            return Response(
                {
                    "items": [],
                    "depositCode": deposit_code or "",
                    "unavailable": True,
                    "warning": "No fue posible verificar stock Bejerman.",
                }
            )


class DeliveryOrderRentalEquipmentView(DeliveryOrderPermissionMixin, APIView):

    def get(self, request):
        try:
            return Response(
                list_rental_available_equipment(
                    request.query_params.get("q") or request.query_params.get("search"),
                    request.query_params.get("limit") or 80,
                    actor_user_id=_actor_id(request),
                    company_key=request.query_params.get("companyKey") or request.query_params.get("company_key"),
                )
            )
        except Exception:
            return Response(
                {
                    "items": [],
                    "depositCode": "STL",
                    "unavailable": True,
                    "warning": "No fue posible verificar equipos de alquiler disponibles.",
                }
            )


class DeliveryOrderBejermanRemitoHistoryView(DeliveryOrderPermissionMixin, APIView):

    def get(self, request):
        return Response({"items": list_remito_history(request.query_params.get("limit") or 20)})


class DeliveryOrderBejermanRemitoPdfView(DeliveryOrderPermissionMixin, APIView):

    def get(self, request, group_id):
        try:
            bytes_, content_type, filename = get_remito_group_pdf(group_id, actor_user_id=_actor_id(request))
        except Exception as exc:
            return _error_response(exc)
        response = HttpResponse(bytes_, content_type=content_type or "application/pdf")
        response["Content-Disposition"] = f'inline; filename="{filename}"'
        return response


def _remito_print_document_type(group_id: str) -> str:
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT comprobante_tipo, response_summary
                  FROM bejerman_remito_groups
                 WHERE id = %s
                """,
                [group_id],
            )
            row = cur.fetchone()
    except Exception:
        row = None
    if not row:
        return ""
    summary = row[1] if isinstance(row[1], dict) else {}
    profile = summary.get("profile") if isinstance(summary.get("profile"), dict) else {}
    return str(profile.get("type") or summary.get("comprobanteTipo") or row[0] or "").strip().upper()


def _remito_print_wait_page(group_id: str, document_type: str | None = None) -> str:
    pdf_url = f"/api/ordenes-entrega/remito-bejerman/{group_id}/pdf/"
    clean_type = str(document_type or "").strip().upper()
    document_name = f"remito {clean_type}" if clean_type else "remito"
    return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Remito Bejerman</title>
  <style>
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f8fafc; color: #111827; }}
    main {{ width: min(680px, calc(100vw - 32px)); padding: 32px; background: white; border: 1px solid #d1d5db; border-radius: 8px; box-shadow: 0 18px 50px rgba(17, 24, 39, .08); text-align: center; }}
    h1 {{ margin: 0 0 12px; font-size: 24px; }}
    p {{ line-height: 1.55; }}
    .spinner {{ display: inline-block; width: 30px; height: 30px; margin-bottom: 14px; border: 3px solid #bae6fd; border-top-color: #0369a1; border-radius: 999px; animation: spin .8s linear infinite; }}
    .detail {{ margin-top: 8px; color: #4b5563; font-size: 14px; }}
    .actions {{ display: none; gap: 12px; flex-wrap: wrap; margin-top: 20px; }}
    .actions.visible {{ display: flex; justify-content: center; }}
    button, a {{ border: 1px solid #9ca3af; border-radius: 8px; background: #111827; color: white; padding: 10px 14px; font: inherit; text-decoration: none; cursor: pointer; }}
    a {{ background: white; color: #111827; }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  </style>
</head>
<body>
  <main>
    <span class="spinner" aria-hidden="true"></span>
    <h1>Preparando {escape(document_name)}</h1>
    <p id="status">Esperando el PDF emitido por Bejerman...</p>
    <p id="detail" class="detail">Esta pestaña se reemplazará por el PDF cuando esté disponible.</p>
    <div id="actions" class="actions">
      <button id="retry" type="button">Reintentar</button>
      <a href="{escape(pdf_url)}" target="_self">Abrir PDF directo</a>
      <button id="close" type="button">Cerrar pestaña</button>
    </div>
  </main>
  <script>
    const pdfUrl = {json.dumps(pdf_url)};
    const documentName = {json.dumps(document_name)};
    const statusEl = document.getElementById('status');
    const detailEl = document.getElementById('detail');
    const actionsEl = document.getElementById('actions');
    const retryEl = document.getElementById('retry');
    const closeEl = document.getElementById('close');
    let attempts = 0;

    function isPdf(bytes) {{
      return bytes.length >= 5 && bytes[0] === 37 && bytes[1] === 80 && bytes[2] === 68 && bytes[3] === 70 && bytes[4] === 45;
    }}

    function showActions(visible) {{
      actionsEl.classList.toggle('visible', visible);
    }}

    function fail(message, detail) {{
      statusEl.textContent = message;
      detailEl.textContent = detail || 'Puede reintentar o volver a la pantalla de órdenes.';
      showActions(true);
    }}

    function retryDelayFrom(response, payload) {{
      const retryAfter = Number(response.headers.get('Retry-After') || 0);
      if (retryAfter > 0) return retryAfter * 1000;
      const retryAfterMs = Number(payload && payload.retry_after_ms || 0);
      if (retryAfterMs > 0) return retryAfterMs;
      return Math.min(900 + attempts * 250, 2000);
    }}

    async function readResponsePayload(response) {{
      const contentType = response.headers.get('content-type') || '';
      if (contentType.includes('application/json')) {{
        return await response.json().catch(() => ({{}}));
      }}
      const text = await response.text().catch(() => '');
      return {{ detail: text }};
    }}

    function scheduleNext(delayMs) {{
      window.setTimeout(pollPdf, Math.max(1000, Number(delayMs) || 1500));
    }}

    async function pollPdf() {{
      attempts += 1;
      showActions(false);
      statusEl.textContent = attempts === 1
        ? 'Buscando el PDF del ' + documentName + ' emitido por Bejerman...'
        : 'El ' + documentName + ' ya fue emitido. Esperando PDF...';
      detailEl.textContent = 'Consultando Bejerman sin bloquear NEXORA.';

      try {{
        const separator = pdfUrl.includes('?') ? '&' : '?';
        const controller = new AbortController();
        const timeout = window.setTimeout(() => controller.abort(), 25000);
        const response = await fetch(pdfUrl + separator + 't=' + Date.now(), {{
          cache: 'no-store',
          credentials: 'same-origin',
          signal: controller.signal,
        }});
        window.clearTimeout(timeout);

        if (response.ok) {{
          const buffer = await response.arrayBuffer();
          const bytes = new Uint8Array(buffer);
          if (isPdf(bytes)) {{
            const blob = new Blob([bytes], {{ type: 'application/pdf' }});
            window.location.replace(URL.createObjectURL(blob));
            return;
          }}
          fail('NEXORA recibió una respuesta que no es un PDF válido.', 'Reintente la impresión desde esta pestaña.');
          return;
        }}

        const payload = await readResponsePayload(response);
        const detail = payload && payload.detail ? String(payload.detail) : '';

        if (response.status === 202) {{
          statusEl.textContent = 'El ' + documentName + ' está emitido. Bejerman todavía está preparando el PDF.';
          detailEl.textContent = detail || 'Esperando la disponibilidad del archivo para imprimir.';
          scheduleNext(retryDelayFrom(response, payload));
          return;
        }}
        if (response.status === 401 || response.status === 403) {{
          fail('Tu sesión no tiene permiso para ver este remito.');
          return;
        }}
        if (response.status === 409) {{
          fail(detail || 'No se pudo preparar el PDF del remito.');
          return;
        }}
        fail(detail || 'No se pudo obtener el PDF del remito.', 'Reintente la impresión. Si el problema persiste, revise el estado de Bejerman.');
        return;
      }} catch (error) {{
        detailEl.textContent = error && error.name === 'AbortError'
          ? 'La consulta del PDF tardó demasiado. Se reintentará automáticamente.'
          : 'No se pudo consultar el PDF en este intento. Se reintentará automáticamente.';
      }}

      scheduleNext(Math.min(900 + attempts * 250, 2000));
    }}

    retryEl.addEventListener('click', () => {{
      attempts = 0;
      void pollPdf();
    }});
    closeEl.addEventListener('click', () => window.close());
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
            _remito_print_wait_page(normalized_group_id, _remito_print_document_type(normalized_group_id)),
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
            return Response(list_facturacion_from_bejerman(customer_code, request.query_params, actor_user_id=_actor_id(request)))
        except Exception as exc:
            return _error_response(exc)


class FacturacionDocumentoPdfView(DeliveryOrderPermissionMixin, APIView):

    def get(self, request, document_id):
        customer_code = request.query_params.get("customerCode") or request.query_params.get("clienteCodigo")
        try:
            bytes_, content_type = get_facturacion_pdf(customer_code, document_id, actor_user_id=_actor_id(request))
        except Exception as exc:
            return _error_response(exc)
        response = HttpResponse(bytes_, content_type=content_type or "application/pdf")
        response["Content-Disposition"] = f'inline; filename="facturacion-{document_id}.pdf"'
        return response


class DeliveryOrderInvoicePdfView(DeliveryOrderPermissionMixin, APIView):

    def get(self, request, order_id):
        try:
            bytes_, content_type, filename = get_delivery_order_invoice_pdf(order_id, actor_user_id=_actor_id(request))
        except Exception as exc:
            return _error_response(exc)
        response = HttpResponse(bytes_, content_type=content_type or "application/pdf")
        response["Content-Disposition"] = f'inline; filename="{filename}"'
        return response


class ServiceOrderBillingListView(DeliveryOrderPermissionMixin, APIView):

    def get(self, request):
        try:
            return Response(list_service_orders_to_bill(request.query_params))
        except Exception as exc:
            return _error_response(exc)


class ServiceOrderBillingInvoiceView(DeliveryOrderPermissionMixin, APIView):

    def post(self, request, ingreso_id):
        payload = request.data or {}
        invoice_number = payload.get("facturaNumero") or payload.get("invoiceNumber") or ""
        try:
            return Response(register_service_order_invoice(ingreso_id, invoice_number, _actor_id(request)))
        except Exception as exc:
            return _error_response(exc)


class ServiceOrderBillingPdfView(DeliveryOrderPermissionMixin, APIView):

    def get(self, request, ingreso_id):
        try:
            bytes_, filename = render_service_order_billing_pdf(ingreso_id)
        except Exception as exc:
            return _error_response(exc)
        response = HttpResponse(bytes_, content_type="application/pdf")
        response["Content-Disposition"] = f'inline; filename="{filename}"'
        return response


__all__ = [
    "DeliveryOrdersView",
    "DeliveryOrderDriveSyncView",
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
    "DeliveryOrderBejermanDepositsView",
    "DeliveryOrderBejermanArticleStockView",
    "DeliveryOrderRentalEquipmentView",
    "DeliveryOrderBejermanRemitoPdfView",
    "DeliveryOrderBejermanRemitoPrintView",
    "DeliveryOrderInvoicePdfView",
    "FacturacionCompanyOptionsView",
    "FacturacionClienteDocumentosView",
    "FacturacionDocumentoPdfView",
    "ServiceOrderBillingListView",
    "ServiceOrderBillingInvoiceView",
    "ServiceOrderBillingPdfView",
]
