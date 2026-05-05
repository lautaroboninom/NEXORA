from django.http import HttpResponse
from rest_framework import permissions
from django.db import transaction
from rest_framework.response import Response
from rest_framework.views import APIView

from .helpers import _set_audit_user, exec_void, q, require_roles
from ..bejerman_sync import enqueue_stock_transfer_for_ingreso
from ..pdf import render_remito_salida_pdf, render_remito_derivacion_pdf
from ..notifications import notify_ingreso_liberado


class RemitoSalidaPdfView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ingreso_id: int):
        require_roles(request, ["jefe", "admin", "recepcion","jefe_veedor"])
        _set_audit_user(request)

        cur_row = q("SELECT resolucion, estado FROM ingresos WHERE id=%s", [ingreso_id], one=True)
        if not cur_row:
            return Response(status=404)
        # Autocompletar resolución si está 'reparado' y aún sin resolución
        if not cur_row["resolucion"] and (cur_row["estado"] or "").lower() == 'reparado':
            exec_void(
                """
                UPDATE ingresos
                   SET resolucion = 'reparado'
                 WHERE id=%s AND (resolucion IS NULL OR btrim(resolucion)='')
                """,
                [ingreso_id],
            )
            cur_row["resolucion"] = 'reparado'
        if not cur_row["resolucion"] and cur_row["estado"] != 'liberado':
            return Response({"detail": "No se puede liberar sin resolución"}, status=409)

        # Marcar 'liberado' y registrar evento para fecha_listo
        exec_void(
            """
          UPDATE ingresos
             SET estado = 'liberado'
           WHERE id=%s AND estado NOT IN ('entregado','baja')
        """,
            [ingreso_id],
        )
        try:
            uid = getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)
            # Aislar en savepoint; si falla no deja la conexión abortada
            with transaction.atomic():
                exec_void(
                    """
                    INSERT INTO ingreso_events (ticket_id, a_estado, usuario_id, comentario)
                    SELECT %s, 'liberado', %s, 'Orden de salida impresa'
                    WHERE NOT EXISTS (
                        SELECT 1 FROM ingreso_events
                         WHERE ingreso_id=%s AND a_estado='liberado'
                    )
                    """,
                    [ingreso_id, uid, ingreso_id],
                )
        except Exception:
            # No bloquear la impresión del remito si falla la auditoría de eventos
            pass

        if (cur_row["estado"] or "").lower() not in ("entregado", "baja"):
            enqueue_stock_transfer_for_ingreso(ingreso_id)

        try:
            notify_ingreso_liberado(ingreso_id)
        except Exception:
            pass

        pdf_bytes, fname = render_remito_salida_pdf(ingreso_id, printed_by=getattr(request.user, "nombre", ""))
        resp = HttpResponse(pdf_bytes, content_type="application/pdf")
        resp["Content-Disposition"] = f'inline; filename="{fname}"'
        return resp


class RemitoDerivacionPdfView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ingreso_id: int, deriv_id: int):
        require_roles(request, ["jefe", "admin", "recepcion", "jefe_veedor", "tecnico"])
        _set_audit_user(request)

        row = q(
            "SELECT id FROM equipos_derivados WHERE id=%s AND ingreso_id=%s",
            [deriv_id, ingreso_id],
            one=True,
        )
        if not row:
            return Response({"detail": "Derivacion no encontrada"}, status=404)

        pdf_bytes, fname = render_remito_derivacion_pdf(ingreso_id, deriv_id, printed_by=getattr(request.user, "nombre", ""))
        resp = HttpResponse(pdf_bytes, content_type="application/pdf")
        resp["Content-Disposition"] = f'inline; filename="{fname}"'
        return resp


__all__ = ['RemitoSalidaPdfView', 'RemitoDerivacionPdfView']
