from __future__ import annotations

from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from ..bejerman_purchase_entries import (
    BejermanPurchaseError,
    add_purchase_line,
    add_purchase_scan,
    create_purchase_entry,
    delete_purchase_line,
    delete_purchase_scan,
    discard_purchase_entry,
    emit_purchase_entry,
    get_purchase_entry,
    list_purchase_articles,
    list_purchase_entries,
    list_purchase_history,
    list_purchase_providers,
    update_purchase_entry,
    update_purchase_line,
    update_purchase_scan,
    validate_purchase_entry,
)
from ..permissions import MappedPermissionGuard


def _actor_id(request):
    return getattr(getattr(request, "user", None), "id", None)


def _error_response(exc: Exception):
    if isinstance(exc, BejermanPurchaseError):
        return Response({"detail": str(exc), "code": exc.code}, status=exc.status_code or 400)
    raise exc


class BejermanPurchasePermissionMixin:
    permission_classes = [permissions.IsAuthenticated, MappedPermissionGuard]


class BejermanPurchaseProvidersView(BejermanPurchasePermissionMixin, APIView):
    def get(self, request):
        try:
            return Response(list_purchase_providers(request.query_params, actor_user_id=_actor_id(request)))
        except Exception as exc:
            return _error_response(exc)


class BejermanPurchaseArticlesView(BejermanPurchasePermissionMixin, APIView):
    def get(self, request):
        try:
            return Response(list_purchase_articles(request.query_params, actor_user_id=_actor_id(request)))
        except Exception:
            return Response({"items": [], "unavailable": True})


class BejermanPurchaseEntriesView(BejermanPurchasePermissionMixin, APIView):
    def get(self, request):
        try:
            return Response(list_purchase_entries(request.query_params))
        except Exception as exc:
            return _error_response(exc)

    def post(self, request):
        try:
            return Response(create_purchase_entry(request.data or {}, _actor_id(request)), status=status.HTTP_201_CREATED)
        except Exception as exc:
            return _error_response(exc)


class BejermanPurchaseEntryDetailView(BejermanPurchasePermissionMixin, APIView):
    def get(self, request, entry_id: str):
        try:
            return Response(get_purchase_entry(entry_id, include_events=True))
        except Exception as exc:
            return _error_response(exc)

    def patch(self, request, entry_id: str):
        try:
            return Response(update_purchase_entry(entry_id, request.data or {}, _actor_id(request)))
        except Exception as exc:
            return _error_response(exc)

    def delete(self, request, entry_id: str):
        try:
            return Response(discard_purchase_entry(entry_id, _actor_id(request)))
        except Exception as exc:
            return _error_response(exc)


class BejermanPurchaseEntryLinesView(BejermanPurchasePermissionMixin, APIView):
    def post(self, request, entry_id: str):
        try:
            return Response(add_purchase_line(entry_id, request.data or {}, _actor_id(request)), status=status.HTTP_201_CREATED)
        except Exception as exc:
            return _error_response(exc)


class BejermanPurchaseEntryLineDetailView(BejermanPurchasePermissionMixin, APIView):
    def patch(self, request, entry_id: str, line_id: str):
        try:
            return Response(update_purchase_line(entry_id, line_id, request.data or {}, _actor_id(request)))
        except Exception as exc:
            return _error_response(exc)

    def delete(self, request, entry_id: str, line_id: str):
        try:
            return Response(delete_purchase_line(entry_id, line_id, _actor_id(request)))
        except Exception as exc:
            return _error_response(exc)


class BejermanPurchaseEntryLineScansView(BejermanPurchasePermissionMixin, APIView):
    def post(self, request, entry_id: str, line_id: str):
        try:
            return Response(add_purchase_scan(entry_id, line_id, request.data or {}, _actor_id(request)), status=status.HTTP_201_CREATED)
        except Exception as exc:
            return _error_response(exc)


class BejermanPurchaseEntryScanDetailView(BejermanPurchasePermissionMixin, APIView):
    def patch(self, request, entry_id: str, scan_id: str):
        try:
            return Response(update_purchase_scan(entry_id, scan_id, request.data or {}, _actor_id(request)))
        except Exception as exc:
            return _error_response(exc)

    def delete(self, request, entry_id: str, scan_id: str):
        try:
            return Response(delete_purchase_scan(entry_id, scan_id, _actor_id(request)))
        except Exception as exc:
            return _error_response(exc)


class BejermanPurchaseEntryValidateView(BejermanPurchasePermissionMixin, APIView):
    def post(self, request, entry_id: str):
        payload = request.data or {}
        try:
            return Response(
                validate_purchase_entry(
                    entry_id,
                    check_remote=bool(payload.get("checkRemote")),
                    mark_validated=True,
                    actor_user_id=_actor_id(request),
                )
            )
        except Exception as exc:
            return _error_response(exc)


class BejermanPurchaseEntryEmitView(BejermanPurchasePermissionMixin, APIView):
    def post(self, request, entry_id: str):
        try:
            return Response(emit_purchase_entry(entry_id, _actor_id(request)))
        except Exception as exc:
            return _error_response(exc)


class BejermanPurchaseHistoryView(BejermanPurchasePermissionMixin, APIView):
    def get(self, request):
        try:
            return Response(list_purchase_history(request.query_params, actor_user_id=_actor_id(request)))
        except Exception as exc:
            return _error_response(exc)
