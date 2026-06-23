from django.http import HttpResponse
from rest_framework import permissions
from django.db import transaction
from rest_framework.response import Response
from rest_framework.views import APIView

from .helpers import _set_audit_user, exec_void, q, require_roles
from ..rejected_budget import get_rejected_quote_summary, get_stored_rejected_budget_fields, has_rejected_budget_charge_schema
from ..bejerman_sync import (
    enqueue_client_ready_transfer_for_ingreso,
    enqueue_demo_ready_transfer_for_ingreso,
    enqueue_stock_transfer_for_ingreso,
    ingreso_is_demo_return,
    ingreso_is_internal_equipment,
)
from ..delivery_orders import ensure_service_release_order_for_ingreso
from ..pdf import render_remito_salida_pdf, render_remito_derivacion_pdf
from ..notifications import notify_ingreso_liberado
from ..service_order_billing import notify_service_order_ready_to_bill


def _parse_ingreso_ids(raw_ids: str, *, max_ids: int = 100) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    for raw in (raw_ids or "").split(","):
        value = (raw or "").strip()
        if not value:
            continue
        try:
            ingreso_id = int(value)
        except (TypeError, ValueError):
            raise ValueError("Parámetro 'ids' inválido")
        if ingreso_id <= 0:
            raise ValueError("Parámetro 'ids' inválido")
        if ingreso_id in seen:
            continue
        seen.add(ingreso_id)
        ids.append(ingreso_id)
    if not ids:
        raise ValueError("Parámetro 'ids' requerido")
    if len(ids) > max_ids:
        raise ValueError(f"Demasiados ingresos (máximo {max_ids})")
    return ids


def _merge_pdf_documents(documents: list[bytes]) -> bytes:
    pdfs = [bytes(doc or b"") for doc in documents if doc]
    if not pdfs:
        return b""
    if len(pdfs) == 1:
        return pdfs[0]

    import fitz

    merged = fitz.open()
    try:
        for pdf in pdfs:
            src = fitz.open(stream=pdf, filetype="pdf")
            try:
                merged.insert_pdf(src)
            finally:
                src.close()
        return merged.tobytes(garbage=4, deflate=True)
    finally:
        merged.close()


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
                    uid = getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)
                    if ingreso_is_internal_equipment(ingreso_id):
                        enqueue_stock_transfer_for_ingreso(ingreso_id, actor_user_id=uid)
                    elif ingreso_is_demo_return(ingreso_id):
                        enqueue_demo_ready_transfer_for_ingreso(ingreso_id, actor_user_id=uid)
                    else:
                        enqueue_client_ready_transfer_for_ingreso(ingreso_id, actor_user_id=uid)
            except Exception:
                pass

        try:
            if not es_venta_pendiente:
                notify_ingreso_liberado(ingreso_id)
        except Exception:
            pass

        try:
            if not es_venta_pendiente:
                with transaction.atomic():
                    ensure_service_release_order_for_ingreso(ingreso_id, getattr(getattr(request, "user", None), "id", None))
        except Exception:
            pass

        try:
            if not es_venta_pendiente:
                notify_service_order_ready_to_bill(
                    ingreso_id,
                    request=request,
                    actor_name=getattr(getattr(request, "user", None), "nombre", ""),
                )
        except Exception:
            pass

        pdf_bytes, fname = render_remito_salida_pdf(ingreso_id, printed_by=getattr(request.user, "nombre", ""))
        resp = HttpResponse(pdf_bytes, content_type="application/pdf")
        resp["Content-Disposition"] = f'inline; filename="{fname}"'
        return resp


class RemitosSalidaBulkPdfView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_roles(request, ["jefe", "admin", "recepcion", "jefe_veedor"])
        try:
            ingreso_ids = _parse_ingreso_ids(request.GET.get("ids") or "")
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)

        documents = []
        single_view = RemitoSalidaPdfView()
        for ingreso_id in ingreso_ids:
            response = single_view.get(request, ingreso_id)
            if getattr(response, "status_code", 500) != 200:
                detail = ""
                try:
                    detail = response.data.get("detail") if isinstance(response.data, dict) else ""
                except Exception:
                    detail = ""
                suffix = f": {detail}" if detail else ""
                return Response(
                    {"detail": f"No se pudo generar el remito de salida de OS {ingreso_id}{suffix}"},
                    status=getattr(response, "status_code", 400),
                )
            documents.append(response.content)

        merged_pdf = _merge_pdf_documents(documents)
        resp = HttpResponse(merged_pdf, content_type="application/pdf")
        resp["Content-Disposition"] = f'inline; filename="Remitos_salida_{len(ingreso_ids)}_OS.pdf"'
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


__all__ = ['RemitoSalidaPdfView', 'RemitosSalidaBulkPdfView', 'RemitoDerivacionPdfView']
