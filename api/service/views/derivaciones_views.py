from django.conf import settings
from contextlib import nullcontext

from django.db import connection, transaction
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import permissions
import logging

from .helpers import _email_append_footer_text, _set_audit_user, exec_void, q
from ..derivations import (
    ACTIVE_DERIVATION_STATES,
    CLOSED_INGRESO_STATES,
    active_derivation_payload,
    get_active_derivation,
)
from ..permissions import require_any_permission, require_permission
from ..notifications import notify_derivacion_devuelta
logger = logging.getLogger(__name__)


def _avisar_derivacion_devuelta(ingreso_id: int, estado_operativo: str) -> None:
    """Envía avisos una vez confirmada la devolución en la base."""
    info = None
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
    except Exception:
        logger.exception(
            "No se pudieron obtener los datos para avisar la devolución",
            extra={"ingreso_id": ingreso_id},
        )

    email = (info or {}).get("tech_email")
    if email:
        ns_text = " ".join(
            p for p in [info.get("numero_interno", ""), info.get("numero_serie", "")] if p
        ).strip()
        subj = f"Aviso: equipo derivado devuelto - OS #{ingreso_id}"
        txt = (
            f"Hola {info.get('tech_nombre','')},\n\n"
            f"El equipo derivado fue devuelto y conserva el estado operativo '{estado_operativo}'.\n\n"
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
            logger.exception(
                "No se pudo enviar el correo de derivación devuelta",
                extra={"ingreso_id": ingreso_id},
            )

    try:
        notify_derivacion_devuelta(ingreso_id)
    except Exception:
        logger.exception(
            "No se pudo crear la notificación de derivación devuelta",
            extra={"ingreso_id": ingreso_id},
        )


class DerivarIngresoView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, ingreso_id: int):
        require_permission(request, "action.ingreso.manage_derivations")
        _set_audit_user(request)
        # Serializar concurrencia por ingreso para evitar inserts duplicados en carreras
        try:
            with (transaction.atomic() if connection.in_atomic_block else nullcontext()):
                exec_void("SELECT pg_advisory_xact_lock(%s)", [ingreso_id])
        except Exception:
            logger.warning(
                "No se pudo tomar el bloqueo de concurrencia para la derivación",
                extra={"ingreso_id": ingreso_id},
                exc_info=True,
            )

        data = request.data or {}
        # `external_service_id` queda sólo como alias de compatibilidad para
        # clientes anteriores; las escrituras nuevas usan `proveedor_id`.
        proveedor_id = data.get("proveedor_id") or data.get("external_service_id")
        if not proveedor_id:
            return Response({"detail": "proveedor_id requerido"}, status=400)

        ing = q(
            "SELECT id, estado::text AS estado FROM ingresos WHERE id=%s FOR UPDATE",
            [ingreso_id],
            one=True,
        )
        if not ing:
            return Response({"detail": "Ingreso no encontrado"}, status=404)
        estado_operativo = str(ing.get("estado") or "").strip().lower()
        if estado_operativo in CLOSED_INGRESO_STATES:
            return Response(
                {"detail": "No se puede derivar una OS liberada, entregada o cerrada."},
                status=409,
            )

        prov = q("SELECT id FROM proveedores_externos WHERE id=%s", [proveedor_id], one=True)
        if not prov:
            return Response({"detail": "Proveedor inválido"}, status=400)

        # Regla: no puede existir más de una derivación activa por ingreso.
        open_any = get_active_derivation(ingreso_id, for_update=True)
        if open_any:
            payload = active_derivation_payload(open_any)
            return Response(
                {
                    "detail": "Ya existe una derivación activa para este ingreso.",
                    "deriv_id": int(payload["derivacion_id"]),
                    **payload,
                },
                status=409,
            )

        from .helpers import exec_returning
        new_id = exec_returning(
            """
            INSERT INTO equipos_derivados (ingreso_id, proveedor_id, remit_deriv, fecha_deriv, comentarios, estado)
            VALUES (%s, %s, %s, COALESCE(%s, CURRENT_DATE), %s, 'derivado')
            RETURNING id
            """,
            [ingreso_id, proveedor_id, data.get("remit_deriv"), data.get("fecha_deriv"), data.get("comentarios")],
        )

        derivacion = get_active_derivation(ingreso_id)
        payload = active_derivation_payload(derivacion)
        return Response(
            {
                "ok": True,
                "deriv_id": new_id,
                "estado": estado_operativo,
                **payload,
            }
        )


class DevolverDerivacionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, ingreso_id: int, deriv_id: int):
        require_permission(request, "action.ingreso.manage_derivations")
        _set_audit_user(request)

        # Tomar primero la OS, igual que al derivar: evita el orden inverso
        # de bloqueos entre ingresos y equipos_derivados en solicitudes simultáneas.
        ingreso = q(
            "SELECT estado::text AS estado FROM ingresos WHERE id=%s FOR UPDATE",
            [ingreso_id],
            one=True,
        ) or {}

        row = q(
            """
            SELECT ed.id,
                   ed.estado::text AS estado,
                   ed.fecha_entrega,
                   ed.proveedor_id,
                   COALESCE(pe.nombre, '') AS proveedor
              FROM equipos_derivados ed
              LEFT JOIN proveedores_externos pe ON pe.id = ed.proveedor_id
             WHERE ed.id=%s AND ed.ingreso_id=%s
             FOR UPDATE OF ed
            """,
            [deriv_id, ingreso_id],
            one=True,
        )
        if not row:
            try:
                logger.warning(f"Devuelto: derivación no encontrada ingreso_id={ingreso_id} deriv_id={deriv_id}")
            except Exception:
                pass
            return Response({"detail": "Derivación no encontrada"}, status=404)

        estado_operativo = str(ingreso.get("estado") or "").strip().lower()
        estado_derivacion = str(row.get("estado") or "").strip().lower()
        if row.get("fecha_entrega") is not None or estado_derivacion not in ACTIVE_DERIVATION_STATES:
            return Response(
                {
                    "ok": True,
                    "already_returned": True,
                    "estado": estado_operativo,
                    **active_derivation_payload(get_active_derivation(ingreso_id)),
                }
            )

        data = request.data or {}
        fecha = data.get("fecha_entrega") or None

        exec_void(
            """
            UPDATE equipos_derivados
               SET fecha_entrega = COALESCE(%s, CURRENT_DATE),
                   estado = 'devuelto'
             WHERE id = %s
               AND fecha_entrega IS NULL
               AND estado IN ('derivado','en_servicio')
            """,
            [fecha, deriv_id],
        )

        # Correo y campana son efectos secundarios: se ejecutan sólo después de
        # confirmar la devolución. Así nunca pueden abortar ni enmascarar el UPDATE.
        transaction.on_commit(
            lambda: _avisar_derivacion_devuelta(ingreso_id, estado_operativo)
        )

        return Response(
            {
                "ok": True,
                "estado": estado_operativo,
                **active_derivation_payload(None),
            }
        )


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
        # El dueño va por el helper y no escrito a mano: `propietario_nombre` es una columna
        # de compat y sin la guarda de esquema este endpoint tira 500 en una base donde el
        # bloque de `sql/schema.sql` no corrió, mientras el resto degrada limpio.
        from .ingresos_views import _owner_list_select_sql

        rows = q(
            f"""
            SELECT ed.id AS deriv_id,
                   ed.ingreso_id,
                   ed.proveedor_id,
                   pe.nombre AS proveedor,
                   ed.remit_deriv,
                   ed.fecha_deriv,
                   ed.fecha_entrega,
                   t.estado::text AS estado,
                   ed.estado::text AS derivacion_estado,
                   TRUE AS derivacion_activa,
                   ed.id AS derivacion_id,
                   ed.proveedor_id AS derivacion_proveedor_id,
                   pe.nombre AS derivacion_proveedor,
                   ed.fecha_deriv AS derivacion_fecha,
                   c.razon_social,
                   {_owner_list_select_sql()}
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
            WHERE ed.estado IN ('derivado','en_servicio') AND ed.fecha_entrega IS NULL
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
