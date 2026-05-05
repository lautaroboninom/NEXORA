from django.conf import settings
from django.db import transaction
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import permissions
import logging

from .helpers import _email_append_footer_text, _set_audit_user, exec_void, q
from ..permissions import require_any_permission, require_permission
from ..notifications import notify_derivacion_devuelta
logger = logging.getLogger(__name__)


class DerivarIngresoView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, ingreso_id: int):
        require_permission(request, "action.ingreso.manage_derivations")
        _set_audit_user(request)
        # Serializar concurrencia por ingreso para evitar inserts duplicados en carreras
        try:
            exec_void("SELECT pg_advisory_xact_lock(%s)", [ingreso_id])
        except Exception:
            pass

        data = request.data or {}
        proveedor_id = data.get("proveedor_id") or data.get("external_service_id")
        if not proveedor_id:
            return Response({"detail": "proveedor_id requerido"}, status=400)

        ing = q("SELECT id FROM ingresos WHERE id=%s", [ingreso_id], one=True)
        if not ing:
            return Response({"detail": "Ingreso no encontrado"}, status=404)

        prov = q("SELECT id FROM proveedores_externos WHERE id=%s", [proveedor_id], one=True)
        if not prov:
            return Response({"detail": "Proveedor externo inválido"}, status=400)

        # Regla: no puede existir mas de una derivacion ABIERTA por ingreso
        open_any = q(
            "SELECT id FROM equipos_derivados WHERE ingreso_id=%s AND estado='derivado' AND fecha_entrega IS NULL ORDER BY id DESC LIMIT 1",
            [ingreso_id],
            one=True,
        )
        if open_any and open_any.get("id"):
            return Response({"detail": "Ya existe una derivacion abierta para este ingreso", "deriv_id": int(open_any["id"])}, status=409)

        # Idempotency: si ya existe una derivacion igual (misma combinacion) abierta, devolver esa
        data_norm = {
            "remit": (data.get("remit_deriv") or "").strip(),
            "fecha": data.get("fecha_deriv"),
            "coment": (data.get("comentarios") or "").strip(),
        }
        existing = q(
            """
            SELECT id
              FROM equipos_derivados
             WHERE ingreso_id=%s AND proveedor_id=%s
               AND COALESCE(TRIM(remit_deriv),'') = COALESCE(TRIM(%s),'')
               AND fecha_deriv = COALESCE(%s, CURRENT_DATE)
               AND COALESCE(TRIM(comentarios),'') = COALESCE(TRIM(%s),'')
               AND estado = 'derivado' AND fecha_entrega IS NULL
             ORDER BY id DESC
             LIMIT 1
            """,
            [ingreso_id, proveedor_id, data_norm["remit"], data_norm["fecha"], data_norm["coment"]],
            one=True,
        )
        if existing and existing.get("id"):
            return Response({"ok": True, "deriv_id": int(existing["id"])})

        from .helpers import exec_returning
        new_id = exec_returning(
            """
            INSERT INTO equipos_derivados (ingreso_id, proveedor_id, remit_deriv, fecha_deriv, comentarios, estado)
            VALUES (%s, %s, %s, COALESCE(%s, CURRENT_DATE), %s, 'derivado')
            RETURNING id
            """,
            [ingreso_id, proveedor_id, data.get("remit_deriv"), data.get("fecha_deriv"), data.get("comentarios")],
        )

        exec_void("UPDATE ingresos SET estado='derivado' WHERE id=%s AND estado <> 'derivado'", [ingreso_id])
        return Response({"ok": True, "deriv_id": new_id})


class DevolverDerivacionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, ingreso_id: int, deriv_id: int):
        require_permission(request, "action.ingreso.manage_derivations")
        _set_audit_user(request)

        row = q(
            "SELECT id FROM equipos_derivados WHERE id=%s AND ingreso_id=%s",
            [deriv_id, ingreso_id],
            one=True,
        )
        if not row:
            try:
                logger.warning(f"Devuelto: derivacion no encontrada ingreso_id={ingreso_id} deriv_id={deriv_id}")
            except Exception:
                pass
            return Response({"detail": "Derivacion no encontrada"}, status=404)

        data = request.data or {}
        fecha = data.get("fecha_entrega") or None

        exec_void(
            """
            UPDATE equipos_derivados
               SET fecha_entrega = COALESCE(%s, CURRENT_DATE),
                   estado = 'devuelto'
             WHERE id = %s
            """,
            [fecha, deriv_id],
        )

        exec_void("UPDATE ingresos SET estado='ingresado' WHERE id=%s AND estado <> 'ingresado'", [ingreso_id])
        try:
            exec_void(
                """
                UPDATE ingreso_events SET comentario='Devolucion de externo'
                WHERE id = (
                  SELECT id FROM ingreso_events
                   WHERE ingreso_id=%s AND a_estado='ingresado'
                   ORDER BY ts DESC, id DESC
                   LIMIT 1
                )
                """,
                [ingreso_id],
            )
        except Exception:
            pass

        try:
            info = q(
                """
                SELECT u.email AS tech_email, COALESCE(u.nombre,'') AS tech_nombre,
                       c.razon_social,
                       d.numero_serie,
                       COALESCE(b.nombre,'') AS marca,
                       COALESCE(m.nombre,'') AS modelo,
                       COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                       COALESCE(d.numero_interno,'') AS numero_interno
                  FROM ingresos t
                  JOIN devices d   ON d.id = t.device_id
                  JOIN customers c ON c.id = d.customer_id
                  LEFT JOIN marcas b ON b.id = d.marca_id
                  LEFT JOIN models m ON m.id = d.model_id
                  LEFT JOIN users  u ON u.id = t.asignado_a
                 WHERE t.id=%s
                """,
                [ingreso_id],
                one=True,
            )
            email = (info or {}).get("tech_email")
            if email:
                ns_text = " ".join(
                    p for p in [info.get("numero_interno", ""), info.get("numero_serie", "")] if p
                ).strip()
                subj = f"Aviso: equipo devuelto de externo - OS #{ingreso_id}"
                txt = (
                    f"Hola {info.get('tech_nombre','')},\n\n"
                    f"El equipo derivado fue devuelto del servicio externo y se reencoló como 'ingresado'.\n\n"
                    f"Cliente: {info.get('razon_social','')}\n"
                    f"Equipo: {info.get('marca','')} {info.get('modelo','')}\n"
                    f"Número de serie: {ns_text}\n\n"
                    f"Hoja de servicio: /ingresos/{ingreso_id}\n"
                )
                try:
                    txt = _email_append_footer_text(txt)
                    from django.core.mail import send_mail
                    send_mail(subj, txt, settings.DEFAULT_FROM_EMAIL, [email], fail_silently=True)
                except Exception:
                    pass
            try:
                notify_derivacion_devuelta(ingreso_id)
            except Exception:
                pass
        except Exception:
            pass

        return Response({"ok": True})


class DerivacionesPorIngresoView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ingreso_id: int):
        require_any_permission(request, ["page.logistics", "action.ingreso.manage_derivations"])
        rows = q(
            """
            SELECT ed.id,
                   ed.ingreso_id,
                   ed.proveedor_id,
                   pe.nombre AS proveedor,
                   ed.remit_deriv,
                   ed.fecha_deriv,
                   ed.fecha_entrega,
                   ed.comentarios,
                   ed.estado
            FROM equipos_derivados ed
            LEFT JOIN proveedores_externos pe ON pe.id = ed.proveedor_id
            WHERE ed.ingreso_id = %s
            ORDER BY ed.fecha_deriv DESC, ed.id DESC
            """,
            [ingreso_id],
        ) or []
        return Response(rows)


class EquiposDerivadosView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        require_permission(request, "page.logistics")
        rows = q(
            """
            SELECT ed.id AS deriv_id,
                   ed.ingreso_id,
                   ed.proveedor_id,
                   pe.nombre AS proveedor,
                   ed.remit_deriv,
                   ed.fecha_deriv,
                   ed.fecha_entrega,
                   ed.estado,
                   c.razon_social,
                   d.numero_serie,
                   COALESCE(b.nombre,'') AS marca,
                   COALESCE(m.nombre,'') AS modelo,
                   COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                   COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,'')) AS equipo_variante,
                    COALESCE(d.numero_interno,'') AS numero_interno
            FROM equipos_derivados ed
            JOIN ingresos t   ON t.id = ed.ingreso_id
            JOIN devices  d   ON d.id = t.device_id
            JOIN customers c  ON c.id = d.customer_id
            LEFT JOIN marcas b ON b.id = d.marca_id
            LEFT JOIN models m ON m.id = d.model_id
            LEFT JOIN proveedores_externos pe ON pe.id = ed.proveedor_id
            WHERE ed.estado = 'derivado' AND ed.fecha_entrega IS NULL
            ORDER BY ed.fecha_deriv DESC, ed.id DESC
            """
        ) or []
        return Response(rows)


__all__ = [
    'DerivarIngresoView',
    'DerivacionesPorIngresoView',
    'DevolverDerivacionView',
    'EquiposDerivadosView',
]
