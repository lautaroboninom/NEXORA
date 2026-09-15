from __future__ import annotations

import logging
from urllib.parse import quote

from django.db import connection
from django.http import HttpResponse
from rest_framework.exceptions import PermissionDenied
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from ..drive_delivery_sync import DriveDeliverySyncError, sync_delivery_orders_to_drive
from ..permissions import MappedPermissionGuard
from ..pdf import render_remito_salida_pdf
from ..bejerman_documents import cobranzas_remito_pdf_metadata, retry_after_header_value
from ..bejerman_sdk import decode_document_id
from ..bejerman_delivery import (
    BillingError,
    check_delivery_remito_stock_sync,
    get_delivery_order_invoice_pdf,
    get_delivery_order_remito_pdf,
    get_facturacion_pdf,
    get_remito_group_pdf,
    list_bejerman_article_stock,
    list_bejerman_articles,
    list_bejerman_depositos,
    list_facturacion_company_options,
    list_facturacion_from_bejerman,
    list_remitos_from_bejerman,
    list_rental_available_equipment,
    registered_remito_no_pdf_error,
    register_delivery_order_remitos,
    resolve_registered_ingreso_remito_for_document_id,
    resolve_remito_group_for_document_id,
)
from ..bejerman_remito_lookup import search_remitos_by_serial
from ..delivery_remito_jobs import enqueue_remito_job, get_remito_job
from ..delivery_orders import (
    DeliveryOrderError,
    cancel_order,
    correct_delivery_order_commercial_terms,
    create_delivery_order,
    get_delivery_order,
    list_delivery_orders,
    list_remito_history,
    mark_delivered,
    mark_invoiced,
    mark_invoiced_batch,
    mark_not_billable,
    mark_prepared,
    postpone_delivery_order_billing,
    reactivate_delivery_order_billing,
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
from .pdf_wait_pages import render_bejerman_pdf_wait_page


logger = logging.getLogger(__name__)


def _actor_id(request):
    return getattr(getattr(request, "user", None), "id", None)


def _error_response(exc: Exception):
    if isinstance(exc, (DeliveryOrderError, BillingError, ServiceOrderBillingError)):
        retry_after_ms = getattr(exc, "retry_after_ms", None)
        payload = {"detail": str(exc), "code": exc.code}
        extra_payload = getattr(exc, "payload", None)
        if isinstance(extra_payload, dict):
            payload.update(extra_payload)
        if retry_after_ms:
            payload["retry_after_ms"] = retry_after_ms
        response = Response(
            payload,
            status=getattr(exc, "status_code", 400) or 400,
        )
        if retry_after_ms:
            response["Retry-After"] = retry_after_header_value(retry_after_ms)
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
        if role not in {"jefe", "jefe_suplente", "admin", "supervisor", "ventas"}:
            raise PermissionDenied("Solo jefe, admin, supervisor o ventas pueden sincronizar órdenes de entrega con Google Drive.")
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
            # Abrir el detalle no debe esperar a Bejerman: el flag de partida se
            # completa si el catálogo ya está cacheado, y si no queda sin
            # resolver. Preparar y emitir el remito lo resuelven en firme
            # (strict=True), que es donde el dato realmente hace falta.
            return Response(
                get_delivery_order(
                    order_id,
                    actor_user_id=_actor_id(request),
                    refresh_article_flags=True,
                    refresh_article_flags_allow_fetch=False,
                )
            )
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


class BejermanRemitoSerialSearchView(DeliveryOrderPermissionMixin, APIView):
    """Busca en Bejerman el remito ya emitido para un equipo, por serie o por MG.

    Da vuelta la carga manual: en vez de pedir el número de comprobante, que el operador
    no tiene, se pregunta por el equipo que salió.
    """

    def get(self, request):
        params = request.query_params
        try:
            return Response(
                search_remitos_by_serial(
                    params.get("serial") or params.get("q"),
                    flow=params.get("flow") or "service_release",
                    company_key=params.get("companyKey"),
                    ingreso_id=params.get("ingresoId"),
                    since=params.get("since"),
                )
            )
        except Exception as exc:
            return _error_response(exc)


class DeliveryOrderRemitosView(DeliveryOrderPermissionMixin, APIView):

    def post(self, request, order_id):
        try:
            return Response(register_delivery_order_remitos(order_id, _actor_id(request), request.data or {}))
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


class DeliveryOrdersBatchInvoicedView(DeliveryOrderPermissionMixin, APIView):

    def post(self, request):
        try:
            payload = request.data or {}
            return Response(
                mark_invoiced_batch(
                    payload.get("orderIds"),
                    _actor_id(request),
                    payload.get("invoiceNumber"),
                )
            )
        except Exception as exc:
            return _error_response(exc)


class DeliveryOrderNotBillableView(DeliveryOrderPermissionMixin, APIView):

    def post(self, request, order_id):
        try:
            return Response(mark_not_billable(order_id, _actor_id(request), (request.data or {}).get("note")))
        except Exception as exc:
            return _error_response(exc)


class DeliveryOrderBillingPostponementView(DeliveryOrderPermissionMixin, APIView):

    def post(self, request, order_id):
        try:
            return Response(
                postpone_delivery_order_billing(order_id, _actor_id(request), (request.data or {}).get("returnDate"))
            )
        except Exception as exc:
            return _error_response(exc)


class DeliveryOrderBillingReactivationView(DeliveryOrderPermissionMixin, APIView):

    def post(self, request, order_id):
        try:
            return Response(reactivate_delivery_order_billing(order_id, _actor_id(request)))
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


class DeliveryOrderCommercialCorrectionView(DeliveryOrderPermissionMixin, APIView):

    def patch(self, request, order_id):
        try:
            return Response(correct_delivery_order_commercial_terms(order_id, request.data or {}, _actor_id(request)))
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
    """Encola la emisión y contesta enseguida.

    Antes esto emitía dentro del propio request, que podía tardar minutos. El
    navegador congela las pestañas en segundo plano, así que la pestaña que estaba
    esperando la respuesta se dormía y no la recogía nunca: el remito salía pero en
    pantalla seguía diciendo "Emitiendo". Ahora el resultado queda guardado y se
    consulta con `DeliveryOrderBejermanRemitoJobView`; el `jobId` sobrevive a que la
    pestaña se congele, se cierre o se recargue.

    Emitir sigue siendo `POST`, así que un cliente viejo que no conozca `jobId` no
    dispara nada raro: recibe 202 y no encuentra el remito, en vez de emitir dos veces.
    """

    def post(self, request):
        payload = request.data or {}
        try:
            job = enqueue_remito_job(payload.get("orderIds") or [], _actor_id(request), payload)
        except Exception as exc:
            return _error_response(exc)
        return Response(job, status=status.HTTP_202_ACCEPTED)


class DeliveryOrderBejermanRemitoJobView(DeliveryOrderPermissionMixin, APIView):
    """Estado de una emisión encolada. Es lo que sondea la pestaña que espera."""

    def get(self, request, job_id: str):
        try:
            return Response(get_remito_job(job_id, _actor_id(request)))
        except Exception as exc:
            return _error_response(exc)


class DeliveryOrderBejermanRemitoPreflightView(DeliveryOrderPermissionMixin, APIView):
    """Avisa que el stock todavía no está sincronizado antes de emitir el remito.

    El control duro sigue viviendo en la emisión; esto sólo lo adelanta para que
    el operador no espere el envío a Bejerman para enterarse.
    """

    def post(self, request):
        payload = request.data or {}
        try:
            return Response(
                check_delivery_remito_stock_sync(
                    payload.get("orderIds") or [],
                    _actor_id(request),
                    retry=bool(payload.get("retry")),
                )
            )
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
                    order_id=request.query_params.get("orderId") or request.query_params.get("order_id"),
                )
            )
        except Exception:
            logger.warning("delivery_order_article_stock_unavailable", exc_info=True)
            return Response(
                {
                    "items": [],
                    "depositCode": deposit_code or "",
                    "companyKey": (
                        request.query_params.get("companyKey") or request.query_params.get("company_key") or ""
                    ).strip().upper(),
                    "totalAvailableQuantity": None,
                    "unavailable": True,
                    "code": "BEJERMAN_ARTICLE_STOCK_UNAVAILABLE",
                    "warning": "No fue posible verificar stock y partidas en Bejerman. Reintentá en unos instantes.",
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


def _remito_print_wait_page(group_id: str, document_type: str | None = None, *, pdf_url_override: str = "") -> str:
    pdf_url = str(pdf_url_override or "").strip() or f"/api/ordenes-entrega/remito-bejerman/{group_id}/pdf/"
    clean_type = str(document_type or "").strip().upper()
    document_name = f"remito {clean_type}" if clean_type else "remito"
    return render_bejerman_pdf_wait_page(pdf_url=pdf_url, document_name=document_name)


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


class DeliveryOrderRemitoPdfView(DeliveryOrderPermissionMixin, APIView):

    def get(self, request, order_id):
        try:
            bytes_, content_type, filename = get_delivery_order_remito_pdf(order_id, actor_user_id=_actor_id(request))
        except Exception as exc:
            return _error_response(exc)
        response = HttpResponse(bytes_, content_type=content_type or "application/pdf")
        response["Content-Disposition"] = f'inline; filename="{filename}"'
        return response


class DeliveryOrderRemitoPrintView(DeliveryOrderPermissionMixin, APIView):

    def get(self, request, order_id):
        normalized_order_id = str(order_id or "").strip()
        if not normalized_order_id:
            return Response({"detail": "Orden inválida."}, status=400)
        try:
            order = get_delivery_order(normalized_order_id, include_events=False)
        except Exception as exc:
            return _error_response(exc)
        document_type = str(order.get("remitoNumber") or "").strip().split(" ", 1)[0].upper()
        return HttpResponse(
            _remito_print_wait_page(
                "",
                document_type,
                pdf_url_override=f"/api/ordenes-entrega/{quote(normalized_order_id, safe='')}/remito/pdf/",
            ),
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


class CobranzasRemitosView(DeliveryOrderPermissionMixin, APIView):

    def get(self, request):
        customer_code = request.query_params.get("customerCode") or request.query_params.get("clienteCodigo")
        company_key = request.query_params.get("companyKey") or request.query_params.get("company_key")
        try:
            return Response(
                list_remitos_from_bejerman(
                    customer_code,
                    request.query_params,
                    actor_user_id=_actor_id(request),
                    company_key=company_key,
                )
            )
        except Exception as exc:
            return _error_response(exc)


def _cobranzas_remito_pdf_url(document_id: str, request) -> str:
    metadata = cobranzas_remito_pdf_metadata(
        document_id,
        customer_code=request.query_params.get("customerCode") or request.query_params.get("clienteCodigo"),
        company_key=request.query_params.get("companyKey") or request.query_params.get("company_key"),
    )
    return metadata.get("pdfUrl") or f"/api/cobranzas/remitos/{quote(str(document_id or '').strip(), safe='')}/pdf/"


def _cobranzas_remito_document_type(document_id: str) -> str:
    try:
        decoded = decode_document_id(document_id)
    except Exception:
        return ""
    return str(decoded.get("t") or decoded.get("type") or "").strip().upper()


class CobranzasRemitoPdfView(DeliveryOrderPermissionMixin, APIView):

    def get(self, request, document_id):
        customer_code = request.query_params.get("customerCode") or request.query_params.get("clienteCodigo")
        company_key = request.query_params.get("companyKey") or request.query_params.get("company_key")
        try:
            local_group = resolve_remito_group_for_document_id(document_id, company_key=company_key)
            if local_group:
                bytes_, content_type, filename = get_remito_group_pdf(local_group["id"], actor_user_id=_actor_id(request))
            else:
                registered_remito = resolve_registered_ingreso_remito_for_document_id(document_id, company_key=company_key)
                if registered_remito:
                    raise registered_remito_no_pdf_error(
                        registered_remito.get("remito_number") or registered_remito.get("manual_remito_number")
                    )
                bytes_, content_type = get_facturacion_pdf(
                    customer_code,
                    document_id,
                    interactive=True,
                    actor_user_id=_actor_id(request),
                    company_key=company_key,
                )
                filename = f"remito-{document_id}.pdf"
        except Exception as exc:
            return _error_response(exc)
        response = HttpResponse(bytes_, content_type=content_type or "application/pdf")
        response["Content-Disposition"] = f'inline; filename="{filename}"'
        return response


class CobranzasRemitoPrintView(DeliveryOrderPermissionMixin, APIView):

    def get(self, request, document_id):
        normalized_document_id = str(document_id or "").strip()
        if not normalized_document_id:
            return Response({"detail": "Remito inválido."}, status=400)
        document_type = _cobranzas_remito_document_type(normalized_document_id)
        return HttpResponse(
            _remito_print_wait_page(
                "",
                document_type,
                pdf_url_override=_cobranzas_remito_pdf_url(normalized_document_id, request),
            ),
            content_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )


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
    "DeliveryOrderNotBillableView",
    "DeliveryOrderBillingPostponementView",
    "DeliveryOrderBillingReactivationView",
    "DeliveryOrderCancelView",
    "DeliveryOrderRemitoLocationView",
    "DeliveryOrderCommercialCorrectionView",
    "DeliveryOrderItemArticleView",
    "DeliveryOrderItemPartidasView",
    "DeliveryOrderBejermanRemitoView",
    "DeliveryOrderBejermanRemitoPreflightView",
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
    "CobranzasRemitosView",
    "CobranzasRemitoPdfView",
    "ServiceOrderBillingListView",
    "ServiceOrderBillingInvoiceView",
    "ServiceOrderBillingPdfView",
]
