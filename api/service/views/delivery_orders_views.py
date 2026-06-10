from __future__ import annotations

from django.http import HttpResponse
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from ..permissions import MappedPermissionGuard
from ..bejerman_delivery import (
    BillingError,
    generate_bejerman_remito,
    get_facturacion_pdf,
    get_remito_group_pdf,
    list_facturacion_company_options,
    list_facturacion_from_bejerman,
)
from ..delivery_orders import (
    DeliveryOrderError,
    cancel_order,
    create_delivery_order,
    get_delivery_order,
    list_delivery_orders,
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
        return Response(
            {"detail": str(exc), "code": exc.code},
            status=getattr(exc, "status_code", 400) or 400,
        )
    raise exc


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


class DeliveryOrderBejermanRemitoPdfView(DeliveryOrderPermissionMixin, APIView):

    def get(self, request, group_id):
        try:
            bytes_, content_type, filename = get_remito_group_pdf(group_id)
        except Exception as exc:
            return _error_response(exc)
        response = HttpResponse(bytes_, content_type=content_type or "application/pdf")
        response["Content-Disposition"] = f'inline; filename="{filename}"'
        return response


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
    "DeliveryOrderPreparedView",
    "DeliveryOrderDeliveredView",
    "DeliveryOrderInvoicedView",
    "DeliveryOrderCancelView",
    "DeliveryOrderRemitoLocationView",
    "DeliveryOrderItemArticleView",
    "DeliveryOrderItemPartidasView",
    "DeliveryOrderBejermanRemitoView",
    "DeliveryOrderBejermanRemitoPdfView",
    "FacturacionCompanyOptionsView",
    "FacturacionClienteDocumentosView",
    "FacturacionDocumentoPdfView",
]
