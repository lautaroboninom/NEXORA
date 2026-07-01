from __future__ import annotations

from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from ..delivery_orders import DeliveryOrderError
from ..permissions import MappedPermissionGuard
from ..route_sheet import (
    RouteSheetError,
    cancel_route_stop,
    complete_route_stop,
    create_route_location,
    create_route_stop,
    list_route_locations,
    list_route_stops,
    list_suggested_delivery_orders,
    postpone_route_stop,
    reorder_route_stops,
    update_route_location,
    update_route_stop,
)


def _actor_id(request):
    return getattr(getattr(request, "user", None), "id", None)


def _error_response(exc: Exception):
    if isinstance(exc, (RouteSheetError, DeliveryOrderError)):
        return Response(
            {"detail": str(exc), "code": exc.code},
            status=getattr(exc, "status_code", 400) or 400,
        )
    raise exc


class RouteSheetPermissionMixin:
    permission_classes = [permissions.IsAuthenticated, MappedPermissionGuard]


class RouteSheetView(RouteSheetPermissionMixin, APIView):
    def get(self, request):
        try:
            return Response(list_route_stops(request.query_params))
        except Exception as exc:
            return _error_response(exc)

    def post(self, request):
        try:
            return Response(create_route_stop(request.data or {}, _actor_id(request)), status=status.HTTP_201_CREATED)
        except Exception as exc:
            return _error_response(exc)


class RouteSheetDetailView(RouteSheetPermissionMixin, APIView):
    def patch(self, request, stop_id):
        try:
            return Response(update_route_stop(stop_id, request.data or {}, _actor_id(request)))
        except Exception as exc:
            return _error_response(exc)


class RouteSheetCompleteView(RouteSheetPermissionMixin, APIView):
    def post(self, request, stop_id):
        try:
            return Response(complete_route_stop(stop_id, _actor_id(request), request.data or {}))
        except Exception as exc:
            return _error_response(exc)


class RouteSheetPostponeView(RouteSheetPermissionMixin, APIView):
    def post(self, request, stop_id):
        try:
            return Response(postpone_route_stop(stop_id, _actor_id(request), request.data or {}))
        except Exception as exc:
            return _error_response(exc)


class RouteSheetCancelView(RouteSheetPermissionMixin, APIView):
    def post(self, request, stop_id):
        try:
            return Response(cancel_route_stop(stop_id, _actor_id(request), request.data or {}))
        except Exception as exc:
            return _error_response(exc)


class RouteSheetReorderView(RouteSheetPermissionMixin, APIView):
    def post(self, request):
        try:
            return Response(reorder_route_stops(request.data or {}, _actor_id(request)))
        except Exception as exc:
            return _error_response(exc)


class RouteSheetLocationsView(RouteSheetPermissionMixin, APIView):
    def get(self, request):
        try:
            return Response(
                list_route_locations(
                    request.query_params.get("q") or request.query_params.get("search") or "",
                    request.query_params.get("limit") or 30,
                    request.query_params.get("customerId") or request.query_params.get("customer_id"),
                )
            )
        except Exception as exc:
            return _error_response(exc)

    def post(self, request):
        try:
            return Response(create_route_location(request.data or {}), status=status.HTTP_201_CREATED)
        except Exception as exc:
            return _error_response(exc)


class RouteSheetLocationDetailView(RouteSheetPermissionMixin, APIView):
    def patch(self, request, location_id):
        try:
            return Response(update_route_location(location_id, request.data or {}))
        except Exception as exc:
            return _error_response(exc)


class RouteSheetSuggestedDeliveryOrdersView(RouteSheetPermissionMixin, APIView):
    def get(self, request):
        try:
            return Response(list_suggested_delivery_orders(request.query_params))
        except Exception as exc:
            return _error_response(exc)
