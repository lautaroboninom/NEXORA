from __future__ import annotations

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
    generate_bejerman_remito,
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
    resolve_registered_ingreso_remito_for_document_id,
    resolve_remito_group_for_document_id,
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
    mark_not_billable,
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
from .pdf_wait_pages import render_bejerman_pdf_wait_page


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
        if role not in {"jefe", "admin", "supervisor", "ventas"}:
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
            return Response(get_delivery_order(order_id, actor_user_id=_actor_id(request), refresh_article_flags=True))
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


class DeliveryOrderNotBillableView(DeliveryOrderPermissionMixin, APIView):

    def post(self, request, order_id):
        try:
            return Response(mark_not_billable(order_id, _actor_id(request), (request.data or {}).get("note")))
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
    "CobranzasRemitosView",
    "CobranzasRemitoPdfView",
    "ServiceOrderBillingListView",
    "ServiceOrderBillingInvoiceView",
    "ServiceOrderBillingPdfView",
]
