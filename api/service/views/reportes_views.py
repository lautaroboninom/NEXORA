from django.http import HttpResponse
from rest_framework import permissions
from django.db import transaction
from rest_framework.response import Response
from rest_framework.views import APIView

from .helpers import _set_audit_user, exec_void, q, require_roles
from ..rejected_budget import get_rejected_quote_summary, get_stored_rejected_budget_fields, has_rejected_budget_charge_schema
from ..bejerman_sync import (
    enqueue_client_ready_transfer_for_ingreso,
    enqueue_stock_transfer_for_ingreso,
    ingreso_is_internal_equipment,
)
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
        estado_actual = (cur_row["estado"] or "").lower()
        es_venta_pendiente = estado_actual == "vendido_pendiente_entrega"
        # Autocompletar resolución si está 'reparado' y aún sin resolución
        if not cur_row["resolucion"] and estado_actual == 'reparado':
            exec_void(
                """
                UPDATE ingresos
                   SET resolucion = 'reparado'
                 WHERE id=%s AND (resolucion IS NULL OR btrim(resolucion)='')
                """,
                [ingreso_id],
            )
            cur_row["resolucion"] = 'reparado'
        if not cur_row["resolucion"] and cur_row["estado"] != 'liberado' and not es_venta_pendiente:
            return Response({"detail": "No se puede liberar sin resolución"}, status=409)

        if cur_row["resolucion"] == "presupuesto_rechazado" and has_rejected_budget_charge_schema():
            stored_fields = get_stored_rejected_budget_fields(ingreso_id)
            if stored_fields.get("presupuesto_rechazado_cobro_neto") is None:
                return Response(
                    {"detail": "No se puede imprimir la orden de salida sin definir el cobro neto del presupuesto rechazado."},
                    status=409,
                )
            latest_rejected_quote = get_rejected_quote_summary(ingreso_id)
            latest_rejected_quote_id = latest_rejected_quote.get("quote_id") if latest_rejected_quote else None
            if latest_rejected_quote_id != stored_fields.get("presupuesto_rechazado_quote_id"):
                exec_void(
                    """
                    UPDATE ingresos
                       SET presupuesto_rechazado_quote_id=%s
                     WHERE id=%s
                    """,
                    [latest_rejected_quote_id, ingreso_id],
                )

        # Marcar 'liberado' y registrar evento para fecha_listo
        exec_void(
            """
          UPDATE ingresos
             SET estado = 'liberado'
           WHERE id=%s AND estado NOT IN ('entregado','baja','vendido_pendiente_entrega','vendido_entregado')
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
                      AND NOT EXISTS (
                        SELECT 1 FROM ingresos
                         WHERE id=%s AND estado IN ('vendido_pendiente_entrega','vendido_entregado')
                    )
                    """,
                    [ingreso_id, uid, ingreso_id, ingreso_id],
                )
        except Exception:
            # No bloquear la impresión del remito si falla la auditoría de eventos
            pass

        if estado_actual not in ("entregado", "baja", "vendido_pendiente_entrega", "vendido_entregado"):
            try:
                with transaction.atomic():
                    if ingreso_is_internal_equipment(ingreso_id):
                        enqueue_stock_transfer_for_ingreso(ingreso_id)
                    else:
                        enqueue_client_ready_transfer_for_ingreso(ingreso_id)
            except Exception:
                pass

        try:
            if not es_venta_pendiente:
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
