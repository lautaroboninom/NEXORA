from django.db import connection, transaction
import time
import json
import logging
from datetime import date, datetime
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework import permissions
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from django.http import HttpResponse
from io import BytesIO
from openpyxl import Workbook
from django.conf import settings
from django.core.mail import send_mail, get_connection, EmailMessage

from .helpers import (
    _fetchall_dicts,
    _get_motivo_enum_values,
    _get_motivo_enum_values_raw,
    _map_motivo_to_db_label,
    _motivo_is_cotizacion_equipo,
    _norm_txt,
    _rol,
    _set_audit_user,
    _frontend_url,
    _email_append_footer_text,
    exec_returning,
    exec_void,
    last_insert_id,
    os_label,
    q,
    require_roles,
    require_permission,
    ensure_default_locations,
)
from .preventivos_sync import sync_plan_from_ingreso_fecha_servicio
from .mg_state import resolve_mg_flags

logger = logging.getLogger(__name__)
from ..serializers import (
    IngresoDetailSerializer,
    IngresoDetailWithAccesoriosSerializer,
    IngresoListItemSerializer,
)
from ..permissions import require_any_permission, user_has_any_permission, user_has_permission
from ..warranty import compute_warranty
from ..notifications import (
    notify_estado_patrimonial,
    notify_ingreso_asignado,
    notify_reparacion_lista_remito,
    notify_solicitud_asignacion,
    notify_solicitud_baja,
)


# Helpers locales (sin mover mÃ³dulos por ahora)
def _equipolabel_row(r):
    try:
        tipo = (r.get("tipo_equipo") or "").strip()
        marca = (r.get("marca") or "").strip()
        modelo = (r.get("modelo") or "").strip()
        variante = (r.get("equipo_variante") or r.get("variante") or r.get("modelo_variante") or r.get("variante_nombre") or "").strip()
        modelo_comp = (f"{modelo} {variante}" if modelo else variante).strip()
        parts = [p for p in [tipo, marca, modelo_comp] if p]
        return " | ".join(parts) if parts else "-"
    except Exception:
        return "-"


def _ns_label(r):
    try:
        interno = (r.get("numero_interno") or "").strip()
        serie = (r.get("numero_serie") or "").strip()
        return interno or serie or "-"
    except Exception:
        return "-"


def _parse_datetime_or_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, 0, 0, 0)
    s = str(value).strip()
    if not s:
        return None
    s_norm = s[:-1] + "+00:00" if s.endswith("Z") else s
    dt = parse_datetime(s_norm) or parse_datetime(s)
    if dt:
        return dt
    d = parse_date(s)
    if d:
        return datetime(d.year, d.month, d.day, 0, 0, 0)
    for sep in ("/", "-", "."):
        parts = s.split(sep)
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            if len(parts[0]) == 4:
                year, month, day = parts
            elif len(parts[2]) == 4:
                day, month, year = parts
            else:
                continue
            try:
                return datetime(int(year), int(month), int(day), 0, 0, 0)
            except ValueError:
                return None
    return None


def _has_mg_schema() -> bool:
    try:
        if connection.vendor == "postgresql":
            row = q(
                """
                SELECT 1
                  FROM information_schema.columns
                 WHERE table_name='devices'
                   AND column_name='mg_estado'
                   AND table_schema = ANY(current_schemas(true))
                 LIMIT 1
                """,
                one=True,
            )
            return bool(row)
        row = q(
            """
            SELECT 1
              FROM information_schema.columns
             WHERE table_name='devices'
               AND column_name='mg_estado'
             LIMIT 1
            """,
            one=True,
        )
        return bool(row)
    except Exception:
        return False


def _has_table_column(table_name: str, column_name: str) -> bool:
    try:
        if connection.vendor == "postgresql":
            row = q(
                """
                SELECT 1
                  FROM information_schema.columns
                 WHERE table_name=%s
                   AND column_name=%s
                   AND table_schema = ANY(current_schemas(true))
                 LIMIT 1
                """,
                [table_name, column_name],
                one=True,
            )
            return bool(row)
        row = q(
            """
            SELECT 1
              FROM information_schema.columns
             WHERE table_name=%s
               AND column_name=%s
             LIMIT 1
            """,
            [table_name, column_name],
            one=True,
        )
        return bool(row)
    except Exception:
        return False


def _has_mg_sale_extended_schema() -> bool:
    return _has_table_column("devices", "mg_venta_customer_id") and _has_table_column(
        "devices", "mg_venta_numero_alternativo"
    )


def _has_historical_corrections_schema() -> bool:
    return (
        _table_exists("ingreso_historical_corrections")
        and _has_table_column("ingreso_historical_corrections", "accion")
        and _has_table_column("ingreso_historical_corrections", "fecha_efectiva")
        and _has_table_column("ingreso_historical_corrections", "motivo")
        and _has_table_column("ingreso_historical_corrections", "payload")
    )


def _ingresos_has_permite_reparacion_col() -> bool:
    try:
        if connection.vendor == "postgresql":
            row = q(
                """
                SELECT 1
                  FROM information_schema.columns
                 WHERE table_name='ingresos'
                   AND column_name='permite_reparacion'
                   AND table_schema = ANY(current_schemas(true))
                 LIMIT 1
                """,
                one=True,
            )
            return bool(row)
        row = q(
            """
            SELECT 1
              FROM information_schema.columns
             WHERE table_name='ingresos'
               AND column_name='permite_reparacion'
             LIMIT 1
            """,
            one=True,
        )
        return bool(row)
    except Exception:
        return False


def _is_cotizacion_bloqueada_para_reparar(motivo, permite_reparacion) -> bool:
    return _motivo_is_cotizacion_equipo(motivo) and not bool(permite_reparacion)


def _mg_owner_customer_id():
    row = q(
        "SELECT id FROM customers WHERE LOWER(razon_social) LIKE %s ORDER BY id ASC LIMIT 1",
        ["%mg%bio%"],
        one=True,
    )
    return row.get("id") if row else None


def _send_mail_with_fallback(subject, body, recipients):
    debug = {}
    try:
        debug.update({
            "backend": getattr(settings, "EMAIL_BACKEND", None),
            "host": getattr(settings, "EMAIL_HOST", None),
            "port": getattr(settings, "EMAIL_PORT", None),
            "use_tls": getattr(settings, "EMAIL_USE_TLS", None),
            "use_ssl": getattr(settings, "EMAIL_USE_SSL", None),
            "from": getattr(settings, "DEFAULT_FROM_EMAIL", None),
            "recipients": list(recipients or []),
        })
    except Exception:
        pass
    if not recipients:
        logger.warning("email no recipients configured")
        return False, debug
    try:
        sent = send_mail(subject, body, getattr(settings, "DEFAULT_FROM_EMAIL", None), recipients, fail_silently=False)
        ok = bool(sent and sent > 0)
        return ok, debug
    except Exception as e:
        try:
            debug["error"] = str(e)
            debug["exception"] = e.__class__.__name__
        except Exception:
            pass
        try:
            port_cfg = int(getattr(settings, "EMAIL_PORT", 0) or 0)
        except Exception:
            port_cfg = 0
        if port_cfg == 587:
            try:
                conn = get_connection(
                    backend=getattr(settings, "EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend"),
                    host=getattr(settings, "EMAIL_HOST", None),
                    port=465,
                    username=getattr(settings, "EMAIL_HOST_USER", None),
                    password=getattr(settings, "EMAIL_HOST_PASSWORD", None),
                    use_tls=False,
                    use_ssl=True,
                    fail_silently=False,
                )
                msg = EmailMessage(subject, body, getattr(settings, "DEFAULT_FROM_EMAIL", None), recipients, connection=conn)
                sent2 = msg.send()
                ok2 = bool(sent2 and sent2 > 0)
                debug["fallback"] = {"mode": "ssl_465", "sent": ok2}
                return ok2, debug
            except Exception as e2:
                try:
                    debug.setdefault("fallback", {})["error"] = str(e2)
                    debug.setdefault("fallback", {})["exception"] = e2.__class__.__name__
                except Exception:
                    pass
        return False, debug


def _dash_location_id():
    try:
        ensure_default_locations()
    except Exception:
        pass
    try:
        row = q("SELECT id FROM locations WHERE nombre='-' LIMIT 1", [], one=True)
        return row and row.get("id")
    except Exception:
        return None


def _taller_location_id():
    try:
        ensure_default_locations()
    except Exception:
        pass
    try:
        row = q(
            "SELECT id FROM locations WHERE LOWER(nombre)=LOWER(%s) LIMIT 1",
            ["Taller"],
            one=True,
        )
        return row and row.get("id")
    except Exception:
        return None


def _pending_baja_request(ingreso_id: int):
    try:
        return q(
            """
            SELECT r.id,
                   r.usuario_id AS uid,
                   COALESCE(u.nombre, '') AS nombre,
                   COALESCE(r.motivo, '') AS motivo,
                   r.created_at
              FROM ingreso_baja_requests r
              LEFT JOIN users u ON u.id = r.usuario_id
             WHERE r.ingreso_id = %s
               AND r.accepted_at IS NULL
               AND r.canceled_at IS NULL
             ORDER BY r.created_at DESC, r.id DESC
             LIMIT 1
            """,
            [ingreso_id],
            one=True,
        )
    except Exception:
        return None


def _table_exists(table_name: str) -> bool:
    try:
        row = q(
            """
            SELECT 1
              FROM information_schema.tables
             WHERE table_schema = ANY(current_schemas(true))
               AND table_name = %s
             LIMIT 1
            """,
            [table_name],
            one=True,
        )
        return bool(row)
    except Exception:
        return False


def _parse_bool_or_default(raw, default=True) -> bool:
    if raw is None:
        return bool(default)
    if isinstance(raw, bool):
        return raw
    val = str(raw).strip().lower()
    if val in ("1", "true", "yes", "y", "si", "sí", "on"):
        return True
    if val in ("0", "false", "no", "n", "off"):
        return False
    return bool(default)


def _parse_historical_effective_datetime(raw):
    dt = _parse_datetime_or_date(raw)
    if not dt:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _notify_estado_patrimonial(request, ingreso_id: int, evento: str, fecha_evento=None):
    evento_norm = (evento or "").strip().lower()
    if evento_norm not in ("baja", "alta"):
        return
    try:
        info = q(
            """
            SELECT c.razon_social AS cliente,
                   COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                   COALESCE(b.nombre,'') AS marca,
                   COALESCE(m.nombre,'') AS modelo,
                   COALESCE(d.numero_serie,'') AS numero_serie,
                   COALESCE(d.numero_interno,'') AS numero_interno,
                   COALESCE(t.informe_preliminar,'') AS informe_preliminar,
                   COALESCE(t.descripcion_problema,'') AS descripcion_problema,
                   COALESCE(t.trabajos_realizados,'') AS trabajos_realizados,
                   COALESCE(t.comentarios,'') AS comentarios,
                   COALESCE(t.resolucion,'') AS resolucion,
                   COALESCE(CAST(t.motivo AS TEXT), '') AS motivo,
                   COALESCE(loc.nombre,'') AS ubicacion_nombre
              FROM ingresos t
              JOIN devices d   ON d.id = t.device_id
              JOIN customers c ON c.id = d.customer_id
              LEFT JOIN marcas b ON b.id = d.marca_id
              LEFT JOIN models m ON m.id = d.model_id
              LEFT JOIN locations loc ON loc.id = t.ubicacion_id
             WHERE t.id=%s
            """,
            [ingreso_id],
            one=True,
        ) or {}
        os_txt = os_label(ingreso_id)
        cliente = info.get("cliente") or ""
        equipo = " | ".join(
            [
                p
                for p in [
                    info.get("tipo_equipo") or "",
                    info.get("marca") or "",
                    info.get("modelo") or "",
                ]
                if p
            ]
        )
        numero_serie = info.get("numero_serie") or ""
        numero_interno = info.get("numero_interno") or ""
        informe_preliminar = (info.get("informe_preliminar") or "").strip()
        descripcion_problema = (info.get("descripcion_problema") or "").strip()
        trabajos_realizados = (info.get("trabajos_realizados") or "").strip()
        comentarios = (info.get("comentarios") or "").strip()
        resolucion = (info.get("resolucion") or "").strip()
        motivo = (info.get("motivo") or "").strip()
        ubicacion = info.get("ubicacion_nombre") or ""
        actor_nombre = getattr(request.user, "nombre", None) or getattr(request.user, "username", "") or ""
        actor_email = getattr(request.user, "email", "") or ""
        if fecha_evento is None:
            fecha_evento_txt = timezone.localtime().strftime("%Y-%m-%d %H:%M")
        else:
            fecha_evento_txt = timezone.localtime(fecha_evento).strftime("%Y-%m-%d %H:%M")
        actor_line = actor_nombre or "-"
        if actor_email:
            actor_line = f"{actor_line} ({actor_email})" if actor_line else actor_email
        resolucion_labels = {
            "reparado": "Reparado",
            "no_reparado": "No reparado",
            "no_se_encontro_falla": "No se encontro falla",
            "presupuesto_rechazado": "Presupuesto rechazado",
            "cambio": "Cambio",
        }
        resolucion_label = resolucion_labels.get(
            resolucion,
            resolucion.replace("_", " ").capitalize() if resolucion else "",
        )

        if evento_norm == "baja":
            subject = f"Notificacion de baja de equipo - {os_txt} - {cliente or 'Sin cliente'}"
            intro = (
                "Se registro la baja de un equipo en el sistema de reparaciones. "
                "Por favor reflejar la baja en el sistema patrimonial."
            )
            fecha_label = "Fecha de baja"
            diag_title = "Diagnostico / motivo de baja:"
        else:
            subject = f"Notificacion de alta de equipo - {os_txt} - {cliente or 'Sin cliente'}"
            intro = (
                "Se registro el alta de un equipo en el sistema de reparaciones. "
                "Por favor reflejar el alta en el sistema patrimonial."
            )
            fecha_label = "Fecha de alta"
            diag_title = "Diagnostico / estado actual:"

        lines = [
            intro,
            "",
            "Detalle del ingreso:",
            f"- OS: {os_txt}",
            f"- Cliente: {cliente or '-'}",
            f"- Equipo: {equipo or '-'}",
            f"- Numero de serie: {numero_serie or '-'}",
            f"- Numero interno: {numero_interno or '-'}",
            f"- Ubicacion actual: {ubicacion or '-'}",
            f"- {fecha_label}: {fecha_evento_txt}",
            f"- Registrado por: {actor_line or '-'}",
        ]
        diag_lines = [
            f"- Diagnostico / descripcion del problema: {descripcion_problema or '-'}",
            f"- Trabajos realizados: {trabajos_realizados or '-'}",
            f"- Comentarios: {comentarios or '-'}",
        ]
        if informe_preliminar:
            diag_lines.append(f"- Informe preliminar: {informe_preliminar}")
        if resolucion_label:
            diag_lines.append(f"- Resolucion: {resolucion_label}")
        if motivo:
            diag_lines.append(f"- Motivo de ingreso: {motivo}")
        lines.append("")
        lines.append(diag_title)
        lines.extend(diag_lines)
        try:
            url = _frontend_url(request, f"/ingresos/{ingreso_id}") + "?tab=principal"
            lines.append("")
            lines.append(f"Hoja de servicio: {url}")
        except Exception:
            pass
        body = "\n".join(lines)
        try:
            body = _email_append_footer_text(body)
        except Exception:
            pass
        recips = getattr(settings, "BAJA_NOTIFY_RECIPIENTS", []) or []
        if not isinstance(recips, (list, tuple)):
            recips = [str(recips)] if recips else []
        recips = [r for r in recips if r]
        try:
            notify_estado_patrimonial(ingreso_id, evento_norm, emails=recips)
        except Exception:
            pass
        if not recips:
            logger.warning(
                "%s_notify_email no recipients configured",
                evento_norm,
                extra={"ingreso_id": ingreso_id},
            )
            return

        def _send():
            try:
                sent, _dbg = _send_mail_with_fallback(subject, body, recips)
                logger.info(
                    "%s_notify_email sent=%s ingreso_id=%s recipients=%s backend=%s",
                    evento_norm,
                    bool(sent),
                    ingreso_id,
                    recips,
                    getattr(settings, "EMAIL_BACKEND", ""),
                )
            except Exception:
                logger.exception(
                    "%s_notify_email failed",
                    evento_norm,
                    extra={"ingreso_id": ingreso_id, "recipients": recips},
                )

        try:
            conn = transaction.get_connection()
            if getattr(conn, "in_atomic_block", False):
                transaction.on_commit(_send)
            else:
                _send()
        except Exception:
            _send()
    except Exception:
        logger.exception(
            "%s_notify_email prepare failed",
            evento_norm,
            extra={"ingreso_id": ingreso_id},
        )


class MisPendientesView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def get(self, request):
        require_roles(request, ["jefe", "tecnico", "jefe_veedor"])
        with connection.cursor() as cur:
            _set_audit_user(request)
            cur.execute(
                """
                SELECT t.id,
                       t.estado,
                       t.presupuesto_estado,
                       t.motivo,
                       c.razon_social,
                       d.numero_serie,
                       COALESCE(d.numero_interno,'') AS numero_interno,
                       COALESCE(b.nombre,'') AS marca,
                       COALESCE(m.nombre,'') AS modelo,
                       COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                       COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,'')) AS equipo_variante,
                       t.asignado_a,
                       COALESCE(u.nombre,'') AS tecnico,
                       COALESCE(u.nombre,'') AS tecnico_nombre,
                       COALESCE(u.nombre,'') AS asignado_a_nombre,
                       t.fecha_ingreso,
                       CASE WHEN ed.estado = 'devuelto' THEN true ELSE false END AS derivado_devuelto
                FROM ingresos t
                JOIN devices d ON d.id=t.device_id
                JOIN customers c ON c.id=d.customer_id
                LEFT JOIN marcas b ON b.id=d.marca_id
                LEFT JOIN models m ON m.id=d.model_id
                LEFT JOIN users u ON u.id=t.asignado_a
                LEFT JOIN locations loc ON loc.id = t.ubicacion_id
                LEFT JOIN (
                  SELECT e.*, ROW_NUMBER() OVER (
                    PARTITION BY e.ingreso_id ORDER BY e.fecha_deriv DESC, e.id DESC
                  ) AS rn
                  FROM equipos_derivados e
                ) ed ON ed.ingreso_id = t.id AND ed.rn = 1
                WHERE t.asignado_a = %s
                  AND LOWER(loc.nombre) = LOWER(%s)
                  AND t.estado NOT IN ('entregado','liberado','alquilado','baja')
                ORDER BY
                   (CASE WHEN ed.estado = 'devuelto' THEN 1 ELSE 0 END) DESC,
                   (t.motivo = 'urgente control') DESC,
                   t.fecha_ingreso ASC;
                """,
                [
                    getattr(getattr(request, "user", None), "id", None)
                    or getattr(request, "user_id", None),
                    "taller",
                ],
            )
            data = _fetchall_dicts(cur)
        return Response(IngresoListItemSerializer(data, many=True).data)


class PendientesPresupuestoView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def get(self, request):
        with connection.cursor() as cur:
            _set_audit_user(request)
            cur.execute(
                """
                SELECT t.id, t.estado, t.presupuesto_estado,
                       c.razon_social,
                       d.numero_serie,
                       COALESCE(d.numero_interno,'') AS numero_interno,
                       COALESCE(b.nombre,'') AS marca,
                       COALESCE(m.nombre,'') AS modelo,
                       COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                       COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,'')) AS equipo_variante,
                       t.fecha_ingreso,
                       t.fecha_servicio,
                       q.fecha_emitido AS presupuesto_fecha_emision
                FROM ingresos t
                JOIN devices d ON d.id=t.device_id
                JOIN customers c ON c.id=d.customer_id
                LEFT JOIN marcas b ON b.id=d.marca_id
                LEFT JOIN models m ON m.id=d.model_id
                LEFT JOIN quotes q ON q.id = (
                  SELECT q2.id FROM quotes q2
                  WHERE q2.ingreso_id = t.id
                  ORDER BY (q2.fecha_emitido IS NOT NULL) DESC, q2.fecha_emitido DESC, q2.id DESC
                  LIMIT 1
                )
                LEFT JOIN locations loc ON loc.id = t.ubicacion_id
                WHERE COALESCE(t.presupuesto_estado, 'pendiente') = 'pendiente'
                  AND LOWER(loc.nombre) = LOWER(%s)
                  AND t.estado NOT IN ('ingresado','entregado','liberado','alquilado','baja')
                ORDER BY COALESCE(t.fecha_servicio, t.fecha_ingreso) ASC;
                """,
                ["taller"],
            )
            data = _fetchall_dicts(cur)
        return Response(IngresoListItemSerializer(data, many=True).data)


class PresupuestadosView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def get(self, request):
        with connection.cursor() as cur:
            _set_audit_user(request)
            cur.execute(
                """
                SELECT
                  t.id,
                  t.estado,
                  CASE
                    WHEN t.presupuesto_estado IS NOT NULL THEN t.presupuesto_estado::text
                    WHEN q.estado::text IN ('emitido','enviado','presupuestado') THEN 'presupuestado'
                    ELSE q.estado::text
                  END AS presupuesto_estado,
                  c.razon_social,
                  d.numero_serie,
                  COALESCE(d.numero_interno,'') AS numero_interno,
                  COALESCE(b.nombre,'') AS marca,
                  COALESCE(m.nombre,'') AS modelo,
                  COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                  COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,'')) AS equipo_variante,
                  t.fecha_ingreso,
                  q.id AS presupuesto_id,
                  q.id AS presupuesto_numero,
                  -- Monto mostrado: Subtotal del presupuesto (mano de obra + repuestos), sin IVA
                  COALESCE((
                    SELECT ROUND(SUM(qi.qty * qi.precio_u), 2)
                    FROM quote_items qi
                    WHERE qi.quote_id = q.id
                  ), 0) AS presupuesto_monto,
                  COALESCE(q.moneda, 'ARS') AS presupuesto_moneda,
                  q.fecha_emitido AS presupuesto_fecha_emision,
                  NULL AS presupuesto_fecha_envio 
                FROM ingresos t
                JOIN devices d ON d.id = t.device_id
                JOIN customers c ON c.id = d.customer_id
                LEFT JOIN marcas b ON b.id = d.marca_id
                LEFT JOIN models m ON m.id = d.model_id
                LEFT JOIN quotes q ON q.id = (
                  SELECT q2.id FROM quotes q2
                  WHERE q2.ingreso_id = t.id
                  ORDER BY (q2.fecha_emitido IS NOT NULL) DESC, q2.fecha_emitido DESC, q2.id DESC
                  LIMIT 1
                )
                LEFT JOIN locations loc ON loc.id = t.ubicacion_id
                WHERE (
                        q.estado::text IN ('emitido','enviado','presupuestado')
                        OR t.presupuesto_estado = 'presupuestado'
                      )
                  AND LOWER(loc.nombre) = LOWER(%s)
                  AND t.estado NOT IN ('entregado','liberado','alquilado','baja')
                ORDER BY COALESCE(q.fecha_emitido, t.fecha_ingreso) ASC;
                """,
                ["taller"],
            )
            return Response(_fetchall_dicts(cur))


class PresupuestadosExportView(APIView):
    """
    Exporta a Excel (.xlsx) filas de 'presupuestados' dadas por sus IDs.

    Parametros query:
      - ids: lista separada por comas de IDs de ingresos. Ej: ?ids=10,11,15

    Columnas:
      OS, Cliente, Equipo, N/S, Monto sin IVA, Fecha emision
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ids_raw = (request.GET.get("ids") or "").strip()
        if not ids_raw:
            return Response({"detail": "Parametro 'ids' requerido"}, status=400)

        try:
            ids = [int(x) for x in ids_raw.split(",") if x.strip()]
        except Exception:
            return Response({"detail": "Parametro 'ids' invÃ¡lido"}, status=400)

        # Evitar excesos accidentales
        if len(ids) > 1000:
            return Response({"detail": "Demasiados IDs (maximo 1000)"}, status=400)

        # Traer datos necesarios. Reutilizamos estructura del listado 'presupuestados'.
        with connection.cursor() as cur:
            _set_audit_user(request)
            cur.execute(
                """
                SELECT
                  t.id,
                  c.razon_social AS cliente,
                  COALESCE(b.nombre,'') AS marca,
                  COALESCE(m.nombre,'') AS modelo,
                  COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                  COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,'')) AS equipo_variante,
                  d.numero_serie,
                  COALESCE(d.numero_interno,'') AS numero_interno,
                  q.fecha_emitido AS fecha_emision,
                  COALESCE((
                    SELECT ROUND(SUM(qi.qty * qi.precio_u), 2)
                      FROM quote_items qi
                     WHERE qi.quote_id = q.id
                  ), 0) AS subtotal_sin_iva
                FROM ingresos t
                JOIN devices d   ON d.id = t.device_id
                JOIN customers c ON c.id = d.customer_id
                LEFT JOIN marcas b ON b.id = d.marca_id
                LEFT JOIN models m ON m.id = d.model_id
                LEFT JOIN quotes q ON q.id = (
                  SELECT q2.id FROM quotes q2
                  WHERE q2.ingreso_id = t.id
                  ORDER BY (q2.fecha_emitido IS NOT NULL) DESC, q2.fecha_emitido DESC, q2.id DESC
                  LIMIT 1
                )
               WHERE t.id = ANY(%s)
               ORDER BY COALESCE(q.fecha_emitido, t.fecha_ingreso, NOW()) ASC
                """,
                [ids],
            )
            rows = _fetchall_dicts(cur)

        # Construir Excel
        wb = Workbook()
        ws = wb.active
        ws.title = "Presupuestados"

        headers = [
            "OS",
            "Cliente",
            "Equipo",
            "N/S",
            "Monto sin IVA",
            "Fecha emision",
        ]
        ws.append(headers)

        # formato de equipo y N/S via helpers _equipolabel_row y _ns_label

        for r in rows:
            os_txt = os_label(r.get("id"))
            equipo = _equipolabel_row(r)
            ns_val = _ns_label(r)
            monto = r.get("subtotal_sin_iva")
            fecha = r.get("fecha_emision")
            # Serializar fecha a texto corto si existe
            if fecha is not None:
                try:
                    fecha_txt = fecha.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    fecha_txt = str(fecha)
            else:
                fecha_txt = "-"

            ws.append([
                os_txt,
                r.get("cliente") or "-",
                equipo,
                ns_val,
                float(monto) if (monto is not None) else None,
                fecha_txt,
            ])

        # Ajuste simple de ancho de columnas (opcional)
        try:
            widths = [10, 40, 40, 20, 18, 20]
            for idx, w in enumerate(widths, start=1):
                col = ws.column_dimensions[chr(64 + idx)]
                col.width = w
        except Exception:
            pass

        # Serializar a binario y responder
        bio = BytesIO()
        wb.save(bio)
        bio.seek(0)

        fname = f"presupuestados_{timezone.now().strftime('%Y%m%d_%H%M')}.xlsx"
        resp = HttpResponse(
            bio.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        resp["Content-Disposition"] = f"attachment; filename=\"{fname}\""
        return resp


class MarcarReparadoView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def post(self, request, ingreso_id: int):
        require_roles(request, ["tecnico", "jefe", "jefe_veedor"])
        has_permite_reparacion = _ingresos_has_permite_reparacion_col()
        permite_reparacion_sql = (
            "COALESCE(permite_reparacion, TRUE) AS permite_reparacion"
            if has_permite_reparacion
            else "TRUE AS permite_reparacion"
        )
        # Solo el tecnico asignado puede marcar como reparado (salvo jefes)
        try:
            if _rol(request) in ("tecnico", "jefe_veedor"):
                row = q(
                    f"""
                    SELECT asignado_a, motivo, {permite_reparacion_sql}
                      FROM ingresos
                     WHERE id=%s
                    """,
                    [ingreso_id],
                    one=True,
                )
                uid = getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)
                if not row or int(row.get("asignado_a") or 0) != int(uid or 0):
                    raise PermissionDenied("Solo el tecnico asignado puede marcar como reparado")
                if _is_cotizacion_bloqueada_para_reparar(row.get("motivo"), row.get("permite_reparacion")):
                    raise ValidationError("Ingreso en cotización: primero habilite la reparación.")
        except PermissionDenied:
            raise
        except ValidationError:
            raise
        except Exception:
            # En duda, permitir solo a roles superiores
            if _rol(request) in ("tecnico", "jefe_veedor"):
                raise PermissionDenied("Solo el tecnico asignado puede marcar como reparado")
        if _rol(request) not in ("tecnico", "jefe_veedor"):
            row = q(
                f"""
                SELECT motivo, {permite_reparacion_sql}
                  FROM ingresos
                 WHERE id=%s
                """,
                [ingreso_id],
                one=True,
            )
            if not row:
                raise ValidationError("Ingreso no encontrado")
            if _is_cotizacion_bloqueada_para_reparar(row.get("motivo"), row.get("permite_reparacion")):
                raise ValidationError("Ingreso en cotización: primero habilite la reparación.")
        # Leer estado/presupuesto previos para decidir envio (idempotencia)
        try:
            _prev_row = q(
                "SELECT estado::text AS estado, COALESCE(presupuesto_estado::text,'') AS presupuesto_estado FROM ingresos WHERE id=%s",
                [ingreso_id],
                one=True,
            ) or {}
            _prev_estado = (_prev_row.get("estado") or "").lower()
            _prev_presu = (_prev_row.get("presupuesto_estado") or "").lower()
        except Exception:
            _prev_estado = ""
            _prev_presu = ""
        _set_audit_user(request)
        exec_void("UPDATE ingresos SET estado='reparado' WHERE id=%s", [ingreso_id])

        # Movimiento automÃ¡tico a EstanterÃ­Â­a de Alquiler si el equipo es "MG ####"
        auto_moved = False
        auto_moved_to = None
        new_ubic_id = None
        try:
            # Leer nÃºmeros para detecciÃ³n MG y ubicaciÃ³n actual
            info = q(
                """
                SELECT COALESCE(d.numero_serie,'') AS numero_serie,
                       COALESCE(d.numero_interno,'') AS numero_interno,
                       t.ubicacion_id,
                       COALESCE(loc.nombre,'') AS ubicacion_nombre
                  FROM ingresos t
                  LEFT JOIN devices d ON d.id = t.device_id
                  LEFT JOIN locations loc ON loc.id = t.ubicacion_id
                 WHERE t.id=%s
                """,
                [ingreso_id],
                one=True,
            ) or {}
            ns = (info.get("numero_serie") or "").strip()
            ni = (info.get("numero_interno") or "").strip()
            import re
            pat = re.compile(r"\bMG \d{4}\b", re.IGNORECASE)
            is_mg = bool(pat.search(ns) or pat.search(ni)) or (ns.strip().upper().startswith("MG ") or ni.strip().upper().startswith("MG "))
            if is_mg:
                # Asegurar existencia de ubicaciones por defecto y buscar ID canÃ³nico
                try:
                    ensure_default_locations()
                except Exception:
                    pass
                target_name = "EstanterÃ­Â­a de Alquiler"
                loc_row = q(
                    "SELECT id, nombre FROM locations WHERE LOWER(nombre)=LOWER(%s) LIMIT 1",
                    [target_name],
                    one=True,
                )
                if loc_row:
                    target_id = loc_row.get("id")
                    cur_id = info.get("ubicacion_id")
                    if target_id and int(cur_id or 0) != int(target_id):
                        exec_void("UPDATE ingresos SET ubicacion_id=%s WHERE id=%s", [target_id, ingreso_id])
                        auto_moved = True
                        auto_moved_to = loc_row.get("nombre") or target_name
                        new_ubic_id = target_id
        except Exception:
            # No bloquear el flujo por errores en movimiento automÃ¡tico
            pass
        # Disparar correo si paso a 'reparado' y presupuesto esta 'aprobado'
        _should_send_mail = (_prev_estado != "reparado" and _prev_presu == "aprobado")
        if _should_send_mail:
            try:
                _info = q(
                    """
                    SELECT c.razon_social AS cliente,
                           COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                           COALESCE(b.nombre,'') AS marca,
                           COALESCE(m.nombre,'') AS modelo,
                           COALESCE(d.numero_serie,'') AS numero_serie,
                           COALESCE(d.numero_interno,'') AS numero_interno
                      FROM ingresos t
                      JOIN devices d   ON d.id = t.device_id
                      JOIN customers c ON c.id = d.customer_id
                      LEFT JOIN marcas b ON b.id = d.marca_id
                      LEFT JOIN models m ON m.id = d.model_id
                     WHERE t.id=%s
                    """,
                    [ingreso_id],
                    one=True,
                ) or {}
            except Exception:
                _info = {}

            _os_txt = os_label(ingreso_id)
            _tech_name = getattr(request.user, "nombre", "")
            _cliente = _info.get("cliente") or ""
            _equipo = " | ".join([p for p in [_info.get("tipo_equipo") or "", _info.get("marca") or "", _info.get("modelo") or ""] if p])
            _ns = _info.get("numero_serie") or ""

            _subject = f"Equipo reparado - falta imprimir remito - {_os_txt} - {_cliente}"
            _lines = [
                "El equipo fue marcado como reparado.",
                "Solo falta imprimir la orden de salida (remito).",
                f"Marcado por: {_tech_name or '-'}",
                f"OS: {_os_txt}",
                f"Cliente: {_cliente}",
                f"Equipo: {_equipo or '-'}",
                f"N/S: {_ns or '-'}",
            ]
            try:
                _url = _frontend_url(request, f"/ingresos/{ingreso_id}") + "?tab=principal"
                _lines.append("")
                _lines.append(f"Abrir hoja: {_url}")
            except Exception:
                pass
            _body = "\n".join(_lines)
            try:
                _body = _email_append_footer_text(_body)
            except Exception:
                pass

            _recips = getattr(settings, "ASSIGNMENT_REQUEST_RECIPIENTS", []) or []
            if not isinstance(_recips, (list, tuple)):
                _recips = [str(_recips)] if _recips else []
            if not _recips:
                _fb1 = getattr(settings, "COMPANY_FOOTER_EMAIL_2", None)
                _fb2 = getattr(settings, "COMPANY_FOOTER_EMAIL", None)
                _recips = [x for x in [_fb1, _fb2] if x]
            try:
                notify_reparacion_lista_remito(ingreso_id, emails=_recips)
            except Exception:
                pass

            def _send_repair_email():
                if not _recips:
                    logger.warning(
                        "repair_completed_email no recipients configured",
                        extra={"ingreso_id": ingreso_id},
                    )
                    return
                try:
                    _ok, _dbg = _send_mail_with_fallback(_subject, _body, _recips)
                    logger.info(
                        "repair_completed_email sent=%s ingreso_id=%s recipients=%s backend=%s",
                        bool(_ok),
                        ingreso_id,
                        _recips,
                        getattr(settings, "EMAIL_BACKEND", ""),
                    )
                except Exception:
                    logger.exception(
                        "repair_completed_email failed",
                        extra={"ingreso_id": ingreso_id, "recipients": _recips},
                    )

            try:
                try:
                    _conn = transaction.get_connection()
                    if getattr(_conn, "in_atomic_block", False):
                        transaction.on_commit(_send_repair_email)
                    else:
                        _send_repair_email()
                except Exception:
                    _send_repair_email()
            except Exception:
                pass

        resp = {"ok": True}
        try:
            if auto_moved:
                resp.update({
                    "auto_moved": True,
                    "auto_moved_to": auto_moved_to or "EstanterÃ­Â­a de Alquiler",
                    "ubicacion_id": new_ubic_id,
                    "ubicacion_nombre": auto_moved_to or "EstanterÃ­Â­a de Alquiler",
                })
        except Exception:
            pass
        return Response(resp)


class MarcarParaRepararView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def post(self, request, ingreso_id: int):
        require_roles(request, ["jefe"])
        has_permite_reparacion = _ingresos_has_permite_reparacion_col()
        permite_reparacion_sql = (
            "COALESCE(permite_reparacion, TRUE) AS permite_reparacion"
            if has_permite_reparacion
            else "TRUE AS permite_reparacion"
        )
        row = q(
            f"""
            SELECT
              estado::text AS estado,
              COALESCE(presupuesto_estado::text,'') AS presupuesto_estado,
              asignado_a,
              motivo,
              {permite_reparacion_sql}
            FROM ingresos
            WHERE id=%s
            """,
            [ingreso_id],
            one=True,
        )
        if not row:
            raise ValidationError("Ingreso no encontrado")
        estado_cur = (row.get("estado") or "").lower()
        presu_cur = (row.get("presupuesto_estado") or "").lower()
        asignado_a = row.get("asignado_a")
        if _rol(request) == "jefe_veedor":
            uid = getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)
            if int(asignado_a or 0) != int(uid or 0):
                raise PermissionDenied("Solo el tecnico asignado puede editar diagnostico y reparacion")
        if not asignado_a:
            raise ValidationError("Antes de reparar, asigna un tÃ©cnico al ingreso")
        if estado_cur in ("reparado", "liberado", "entregado", "baja", "alquilado", "controlado_sin_defecto"):
            raise ValidationError("No se puede marcar para reparar desde el estado actual")
        if _is_cotizacion_bloqueada_para_reparar(row.get("motivo"), row.get("permite_reparacion")):
            raise ValidationError("Ingreso en cotización: primero habilite la reparación.")

        _set_audit_user(request)
        try:
            exec_void(
                """
                UPDATE ingresos
                   SET estado='reparar',
                       presupuesto_estado = CASE
                         WHEN presupuesto_estado='presupuestado' THEN 'aprobado'
                         ELSE presupuesto_estado
                       END
                 WHERE id=%s
                """,
                [ingreso_id],
            )
        except Exception:
            exec_void("UPDATE ingresos SET estado='reparar' WHERE id=%s", [ingreso_id])

        if presu_cur == "presupuestado":
            try:
                qid_row = q("SELECT id FROM quotes WHERE ingreso_id=%s ORDER BY id DESC LIMIT 1", [ingreso_id], one=True)
                qid = qid_row and qid_row.get("id")
                if qid:
                    exec_void(
                        """
                        UPDATE quotes
                           SET estado='aprobado',
                               fecha_aprobado=COALESCE(fecha_aprobado, now())
                         WHERE id=%s
                        """,
                        [qid],
                    )
            except Exception:
                pass

        email_sent = False
        try:
            info = q(
                """
                SELECT
                  u.email,
                  COALESCE(u.nombre,'') AS tecnico_nombre,
                  c.razon_social AS cliente,
                  COALESCE(b.nombre,'') AS marca,
                  COALESCE(m.nombre,'') AS modelo,
                  COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                  COALESCE(d.numero_serie,'') AS numero_serie,
                  COALESCE(d.numero_interno,'') AS numero_interno
                FROM ingresos t
                LEFT JOIN users   u ON u.id = t.asignado_a
                JOIN devices      d ON d.id = t.device_id
                JOIN customers    c ON c.id = d.customer_id
                LEFT JOIN marcas  b ON b.id = d.marca_id
                LEFT JOIN models  m ON m.id = d.model_id
                WHERE t.id=%s
                """,
                [ingreso_id],
                one=True,
            ) or {}
            to_email = (info.get("email") or "").strip()
            if to_email:
                os_txt = os_label(ingreso_id)
                try:
                    link = _frontend_url(request, f"/ingresos/{ingreso_id}") + "?tab=diagnostico"
                except Exception:
                    link = ""
                subject = f"{os_txt} - Autorizado para reparar"
                lines = [
                    f"Hola {info.get('tecnico_nombre') or ''},",
                    "",
                    f"Podes reparar la {os_txt}.",
                    "",
                    "Detalle del equipo:",
                    f"- Cliente: {info.get('cliente') or '-'}",
                    f"- Marca/Modelo: {info.get('marca') or '-'} / {info.get('modelo') or '-'}",
                    f"- Tipo: {info.get('tipo_equipo') or '-'}",
                    f"- Numero de serie: {info.get('numero_interno') or info.get('numero_serie') or '-'}",
                ]
                if link:
                    lines.append("")
                    lines.append(f"Abrir hoja de servicio: {link}")
                try:
                    lines.append("")
                    lines.append("Aviso automatico - no responder a este correo.")
                except Exception:
                    pass
                body = "\n".join(lines)
                try:
                    body = _email_append_footer_text(body)
                except Exception:
                    pass
                sent = send_mail(subject, body, getattr(settings, "DEFAULT_FROM_EMAIL", None), [to_email], fail_silently=True)
                email_sent = bool(sent and sent > 0)
        except Exception:
            pass

        resp = {
            "ok": True,
            "estado": "reparar",
            "presupuesto_estado": "aprobado" if presu_cur == "presupuestado" else presu_cur,
            "email_sent": bool(email_sent),
        }
        return Response(resp)


class HabilitarReparacionCotizacionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, ingreso_id: int):
        require_roles(request, ["jefe"])
        if not _ingresos_has_permite_reparacion_col():
            return Response(
                {"detail": "Schema no aplicado: falta columna ingresos.permite_reparacion."},
                status=400,
            )

        row = q(
            """
            SELECT motivo, COALESCE(permite_reparacion, TRUE) AS permite_reparacion
              FROM ingresos
             WHERE id=%s
            """,
            [ingreso_id],
            one=True,
        )
        if not row:
            raise ValidationError("Ingreso no encontrado")

        motivo = row.get("motivo")
        if not _motivo_is_cotizacion_equipo(motivo):
            return Response(
                {
                    "ok": True,
                    "motivo": motivo,
                    "permite_reparacion": True,
                    "already_enabled": True,
                    "not_required": True,
                }
            )

        if bool(row.get("permite_reparacion")):
            return Response(
                {
                    "ok": True,
                    "motivo": motivo,
                    "permite_reparacion": True,
                    "already_enabled": True,
                }
            )

        _set_audit_user(request)
        exec_void(
            "UPDATE ingresos SET permite_reparacion=TRUE WHERE id=%s",
            [ingreso_id],
        )
        return Response(
            {
                "ok": True,
                "motivo": motivo,
                "permite_reparacion": True,
                "already_enabled": False,
            }
        )


class EntregarIngresoView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def post(self, request, ingreso_id: int):
        require_roles(request, ["jefe", "jefe_veedor", "admin", "recepcion"])
        data = request.data or {}
        remito = (data.get("remito_salida") or "").strip()
        if not remito:
            return Response({"detail": "remito_salida requerido"}, status=400)
        factura = (data.get("factura_numero") or "").strip() or None
        fecha_entrega = data.get("fecha_entrega") or None
        retira_persona = (data.get("retira_persona") or "").strip()
        estado_to = "entregado"
        set_alquilado = False
        # Si es CAMBIO, verificar serie contra la cargada al cerrar
        serial_confirm = (data.get("serial_confirm") or "").strip()
        try:
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT resolucion FROM ingresos WHERE id=%s",
                    [ingreso_id],
                )
                row = cur.fetchone()
                cur_resol = (row[0] if row else None) or ""
                if cur_resol == "cambio":
                    cur.execute(
                        """
                        SELECT 1 FROM information_schema.columns
                         WHERE table_name='ingresos' AND column_name='serial_cambio'
                           AND table_schema = ANY(current_schemas(true))
                         LIMIT 1
                        """
                    )
                    has_col = cur.fetchone() is not None
                    if has_col:
                        cur.execute("SELECT serial_cambio FROM ingresos WHERE id=%s", [ingreso_id])
                        r2 = cur.fetchone()
                        db_serial = ((r2[0] if r2 else None) or "").strip()
                        if db_serial:
                            if not serial_confirm:
                                return Response({"detail": "serial_confirm requerido para entregar con resolucion 'cambio'"}, status=400)
                            if (serial_confirm or "").strip().upper() != db_serial.upper():
                                return Response({"detail": "La serie de confirmaci0n no coincide con la Serie (Cambio)"}, status=409)
        except Exception:
            # fallback: no bloquear la entrega por errores de verificacion
            pass
        try:
            info = q(
                """
                SELECT t.alquilado,
                       COALESCE(d.numero_serie,'') AS numero_serie,
                       COALESCE(d.numero_interno,'') AS numero_interno,
                       COALESCE(loc.nombre,'') AS ubicacion_nombre
                  FROM ingresos t
                  LEFT JOIN devices d ON d.id = t.device_id
                  LEFT JOIN locations loc ON loc.id = t.ubicacion_id
                 WHERE t.id=%s
                """,
                [ingreso_id],
                one=True,
            ) or {}
            ns = (info.get("numero_serie") or "").strip()
            ni = (info.get("numero_interno") or "").strip()
            import re
            pat = re.compile(r"\bMG \d{4}\b", re.IGNORECASE)
            is_mg = bool(pat.search(ns) or pat.search(ni)) or (
                ns.strip().upper().startswith("MG ") or ni.strip().upper().startswith("MG ")
            )
            loc_norm = _norm_txt(info.get("ubicacion_nombre") or "")
            target_loc_norm = _norm_txt("Estanteria de Alquiler")
            if bool(info.get("alquilado")) or is_mg or loc_norm == target_loc_norm:
                estado_to = "alquilado"
                set_alquilado = True
        except Exception:
            # No bloquear la entrega por errores de deteccion
            pass
        comentarios_update = None
        if retira_persona:
            try:
                row = q("SELECT comentarios FROM ingresos WHERE id=%s", [ingreso_id], one=True) or {}
                existing = (row.get("comentarios") or "").strip()
            except Exception:
                existing = ""
            entrega_line = f"Entrega: retira {retira_persona}"
            comentarios_update = f"{existing}\n{entrega_line}" if existing else entrega_line

        _set_audit_user(request)
        with transaction.atomic():
            with connection.cursor() as cur:
                dash_id = _dash_location_id()
                sets = [
                    "estado=%s",
                    "remito_salida=%s",
                    "factura_numero=%s",
                    "fecha_entrega=COALESCE(%s, now())",
                    "ubicacion_id = COALESCE(%s, ubicacion_id)",
                ]
                params = [estado_to, remito, factura, fecha_entrega, dash_id]
                if set_alquilado:
                    sets.append("alquilado=true")
                if comentarios_update is not None:
                    sets.append("comentarios=%s")
                    params.append(comentarios_update)
                params.append(ingreso_id)
                cur.execute(
                    "UPDATE ingresos SET " + ", ".join(sets) + " WHERE id=%s",
                    params,
                )
        try:
            uid = getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)
            with transaction.atomic():
                exec_void(
                    """
                    INSERT INTO ingreso_events (ticket_id, a_estado, usuario_id, comentario)
                    SELECT %s, %s, %s, %s
                    WHERE NOT EXISTS (
                        SELECT 1 FROM ingreso_events
                         WHERE ingreso_id=%s AND a_estado=%s
                    )
                    """,
                    [
                        ingreso_id,
                        estado_to,
                        uid,
                        "Alquiler registrado" if estado_to == "alquilado" else "Entrega registrada",
                        ingreso_id,
                        estado_to,
                    ],
                )
        except Exception:
            # No bloquear la entrega si falla la auditoria de eventos
            pass
        return Response({"ok": True})


class DarBajaIngresoView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def post(self, request, ingreso_id: int):
        require_roles(request, ["jefe", "jefe_veedor", "admin", "recepcion"])
        _set_audit_user(request)
        marked_baja = False
        with transaction.atomic():
            with connection.cursor() as cur:
                dash_id = _dash_location_id()
                cur.execute("SELECT estado FROM ingresos WHERE id=%s", [ingreso_id])
                row = cur.fetchone()
                if not row:
                    return Response({"detail": "Ingreso no encontrado"}, status=404)
                estado_actual = (row[0] or "").lower()
                if estado_actual == "baja":
                    return Response({"ok": True, "estado": "baja"})
                cur.execute(
                    """
                    UPDATE ingresos
                       SET estado='baja',
                           ubicacion_id = COALESCE(%s, ubicacion_id)
                     WHERE id=%s
                    """,
                    [dash_id, ingreso_id],
                )
                marked_baja = True
        if marked_baja:
            try:
                with transaction.atomic():
                    exec_void(
                        """
                        UPDATE ingreso_baja_requests
                           SET accepted_at = COALESCE(accepted_at, NOW())
                         WHERE ingreso_id = %s
                           AND accepted_at IS NULL
                           AND canceled_at IS NULL
                        """,
                        [ingreso_id],
                    )
            except Exception:
                # Tabla opcional / no bloquear la baja real
                pass
        try:
            exec_void(
                """
                INSERT INTO ingreso_events (ticket_id, a_estado, comentario)
                VALUES (%s, 'baja', 'Marcado como baja desde la hoja de servicio')
                """,
                [ingreso_id],
            )
        except Exception:
            # No bloquear si la tabla o inserciÃ³n falla
            pass
        if marked_baja:
            try:
                info = q(
                    """
                    SELECT c.razon_social AS cliente,
                           COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                           COALESCE(b.nombre,'') AS marca,
                           COALESCE(m.nombre,'') AS modelo,
                           COALESCE(d.numero_serie,'') AS numero_serie,
                           COALESCE(d.numero_interno,'') AS numero_interno,
                           COALESCE(t.informe_preliminar,'') AS informe_preliminar,
                           COALESCE(t.descripcion_problema,'') AS descripcion_problema,
                           COALESCE(t.trabajos_realizados,'') AS trabajos_realizados,
                           COALESCE(t.comentarios,'') AS comentarios,
                           COALESCE(t.resolucion,'') AS resolucion,
                           COALESCE(CAST(t.motivo AS TEXT), '') AS motivo,
                           COALESCE(loc.nombre,'') AS ubicacion_nombre
                      FROM ingresos t
                      JOIN devices d   ON d.id = t.device_id
                      JOIN customers c ON c.id = d.customer_id
                      LEFT JOIN marcas b ON b.id = d.marca_id
                      LEFT JOIN models m ON m.id = d.model_id
                      LEFT JOIN locations loc ON loc.id = t.ubicacion_id
                     WHERE t.id=%s
                    """,
                    [ingreso_id],
                    one=True,
                ) or {}
                os_txt = os_label(ingreso_id)
                cliente = info.get("cliente") or ""
                equipo = " | ".join([p for p in [info.get("tipo_equipo") or "", info.get("marca") or "", info.get("modelo") or ""] if p])
                numero_serie = info.get("numero_serie") or ""
                numero_interno = info.get("numero_interno") or ""
                informe_preliminar = (info.get("informe_preliminar") or "").strip()
                descripcion_problema = (info.get("descripcion_problema") or "").strip()
                trabajos_realizados = (info.get("trabajos_realizados") or "").strip()
                comentarios = (info.get("comentarios") or "").strip()
                resolucion = (info.get("resolucion") or "").strip()
                motivo = (info.get("motivo") or "").strip()
                ubicacion = info.get("ubicacion_nombre") or ""
                actor_nombre = getattr(request.user, "nombre", None) or getattr(request.user, "username", "") or ""
                actor_email = getattr(request.user, "email", "") or ""
                fecha_baja = timezone.localtime().strftime("%Y-%m-%d %H:%M")
                actor_line = actor_nombre or "-"
                if actor_email:
                    actor_line = f"{actor_line} ({actor_email})" if actor_line else actor_email
                resolucion_labels = {
                    "reparado": "Reparado",
                    "no_reparado": "No reparado",
                    "no_se_encontro_falla": "No se encontrÃ³ falla",
                    "presupuesto_rechazado": "Presupuesto rechazado",
                    "cambio": "Cambio",
                }
                resolucion_label = resolucion_labels.get(
                    resolucion,
                    resolucion.replace("_", " ").capitalize() if resolucion else "",
                )
                subject = f"NotificaciÃ³n de baja de equipo - {os_txt} - {cliente or 'Sin cliente'}"
                lines = [
                    "Se registrÃ³ la baja de un equipo en el sistema de reparaciones. Por favor reflejar la baja en el sistema patrimonial.",
                    "",
                    "Detalle del ingreso:",
                    f"- OS: {os_txt}",
                    f"- Cliente: {cliente or '-'}",
                    f"- Equipo: {equipo or '-'}",
                    f"- NÃºmero de serie: {numero_serie or '-'}",
                    f"- NÃºmero interno: {numero_interno or '-'}",
                    f"- UbicaciÃ³n actual: {ubicacion or '-'}",
                    f"- Fecha de baja: {fecha_baja}",
                    f"- Registrado por: {actor_line or '-'}",
                ]
                diag_lines = []
                diag_lines.append(f"- DiagnÃ³stico / descripciÃ³n del problema: {descripcion_problema or '-'}")
                diag_lines.append(f"- Trabajos realizados: {trabajos_realizados or '-'}")
                diag_lines.append(f"- Comentarios: {comentarios or '-'}")
                if informe_preliminar:
                    diag_lines.append(f"- Informe preliminar: {informe_preliminar}")
                if resolucion_label:
                    diag_lines.append(f"- ResoluciÃ³n: {resolucion_label}")
                if motivo:
                    diag_lines.append(f"- Motivo de ingreso: {motivo}")
                lines.append("")
                lines.append("DiagnÃ³stico / motivo de baja:")
                lines.extend(diag_lines)
                try:
                    url = _frontend_url(request, f"/ingresos/{ingreso_id}") + "?tab=principal"
                    lines.append("")
                    lines.append(f"Hoja de servicio: {url}")
                except Exception:
                    pass
                body = "\n".join(lines)
                try:
                    body = _email_append_footer_text(body)
                except Exception:
                    pass
                recips = getattr(settings, "BAJA_NOTIFY_RECIPIENTS", []) or []
                if not isinstance(recips, (list, tuple)):
                    recips = [str(recips)] if recips else []
                recips = [r for r in recips if r]
                try:
                    notify_estado_patrimonial(ingreso_id, "baja", emails=recips)
                except Exception:
                    pass

                def _send_baja_email():
                    if not recips:
                        logger.warning(
                            "baja_notify_email no recipients configured",
                            extra={"ingreso_id": ingreso_id},
                        )
                        return
                    try:
                        _sent, _dbg = _send_mail_with_fallback(subject, body, recips)
                        logger.info(
                            "baja_notify_email sent=%s ingreso_id=%s recipients=%s backend=%s",
                            bool(_sent),
                            ingreso_id,
                            recips,
                            getattr(settings, "EMAIL_BACKEND", ""),
                        )
                    except Exception:
                        logger.exception(
                            "baja_notify_email failed",
                            extra={"ingreso_id": ingreso_id, "recipients": recips},
                        )

                try:
                    conn = transaction.get_connection()
                    if getattr(conn, "in_atomic_block", False):
                        transaction.on_commit(_send_baja_email)
                    else:
                        _send_baja_email()
                except Exception:
                    _send_baja_email()
            except Exception:
                logger.exception(
                    "baja_notify_email prepare failed",
                    extra={"ingreso_id": ingreso_id},
                )
        return Response({"ok": True, "estado": "baja"})


class DarAltaIngresoView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def post(self, request, ingreso_id: int):
        require_roles(request, ["jefe", "jefe_veedor", "admin", "recepcion"])
        _set_audit_user(request)
        marked_alta = False
        with transaction.atomic():
            with connection.cursor() as cur:
                taller_id = _taller_location_id()
                cur.execute("SELECT estado FROM ingresos WHERE id=%s", [ingreso_id])
                row = cur.fetchone()
                if not row:
                    return Response({"detail": "Ingreso no encontrado"}, status=404)
                estado_actual = (row[0] or "").lower()
                if estado_actual != "baja":
                    return Response({"detail": "Solo se puede dar de alta un ingreso en estado baja"}, status=400)
                cur.execute(
                    """
                    UPDATE ingresos
                       SET estado='ingresado',
                           ubicacion_id = COALESCE(%s, ubicacion_id)
                     WHERE id=%s
                    """,
                    [taller_id, ingreso_id],
                )
                marked_alta = True
        try:
            exec_void(
                """
                INSERT INTO ingreso_events (ticket_id, a_estado, comentario)
                VALUES (%s, 'ingresado', 'Marcado como alta desde la hoja de servicio')
                """,
                [ingreso_id],
            )
        except Exception:
            # No bloquear si la tabla o insercion falla
            pass
        if marked_alta:
            try:
                info = q(
                    """
                    SELECT c.razon_social AS cliente,
                           COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                           COALESCE(b.nombre,'') AS marca,
                           COALESCE(m.nombre,'') AS modelo,
                           COALESCE(d.numero_serie,'') AS numero_serie,
                           COALESCE(d.numero_interno,'') AS numero_interno,
                           COALESCE(t.informe_preliminar,'') AS informe_preliminar,
                           COALESCE(t.descripcion_problema,'') AS descripcion_problema,
                           COALESCE(t.trabajos_realizados,'') AS trabajos_realizados,
                           COALESCE(t.comentarios,'') AS comentarios,
                           COALESCE(t.resolucion,'') AS resolucion,
                           COALESCE(CAST(t.motivo AS TEXT), '') AS motivo,
                           COALESCE(loc.nombre,'') AS ubicacion_nombre
                      FROM ingresos t
                      JOIN devices d   ON d.id = t.device_id
                      JOIN customers c ON c.id = d.customer_id
                      LEFT JOIN marcas b ON b.id = d.marca_id
                      LEFT JOIN models m ON m.id = d.model_id
                      LEFT JOIN locations loc ON loc.id = t.ubicacion_id
                     WHERE t.id=%s
                    """,
                    [ingreso_id],
                    one=True,
                ) or {}
                os_txt = os_label(ingreso_id)
                cliente = info.get("cliente") or ""
                equipo = " | ".join([p for p in [info.get("tipo_equipo") or "", info.get("marca") or "", info.get("modelo") or ""] if p])
                numero_serie = info.get("numero_serie") or ""
                numero_interno = info.get("numero_interno") or ""
                informe_preliminar = (info.get("informe_preliminar") or "").strip()
                descripcion_problema = (info.get("descripcion_problema") or "").strip()
                trabajos_realizados = (info.get("trabajos_realizados") or "").strip()
                comentarios = (info.get("comentarios") or "").strip()
                resolucion = (info.get("resolucion") or "").strip()
                motivo = (info.get("motivo") or "").strip()
                ubicacion = info.get("ubicacion_nombre") or ""
                actor_nombre = getattr(request.user, "nombre", None) or getattr(request.user, "username", "") or ""
                actor_email = getattr(request.user, "email", "") or ""
                fecha_alta = timezone.localtime().strftime("%Y-%m-%d %H:%M")
                actor_line = actor_nombre or "-"
                if actor_email:
                    actor_line = f"{actor_line} ({actor_email})" if actor_line else actor_email
                resolucion_labels = {
                    "reparado": "Reparado",
                    "no_reparado": "No reparado",
                    "no_se_encontro_falla": "No se encontro falla",
                    "presupuesto_rechazado": "Presupuesto rechazado",
                    "cambio": "Cambio",
                }
                resolucion_label = resolucion_labels.get(
                    resolucion,
                    resolucion.replace("_", " ").capitalize() if resolucion else "",
                )
                subject = f"Notificacion de alta de equipo - {os_txt} - {cliente or 'Sin cliente'}"
                lines = [
                    "Se registro el alta de un equipo en el sistema de reparaciones. Por favor reflejar el alta en el sistema patrimonial.",
                    "",
                    "Detalle del ingreso:",
                    f"- OS: {os_txt}",
                    f"- Cliente: {cliente or '-'}",
                    f"- Equipo: {equipo or '-'}",
                    f"- Numero de serie: {numero_serie or '-'}",
                    f"- Numero interno: {numero_interno or '-'}",
                    f"- Ubicacion actual: {ubicacion or '-'}",
                    f"- Fecha de alta: {fecha_alta}",
                    f"- Registrado por: {actor_line or '-'}",
                ]
                diag_lines = []
                diag_lines.append(f"- Diagnostico / descripcion del problema: {descripcion_problema or '-'}")
                diag_lines.append(f"- Trabajos realizados: {trabajos_realizados or '-'}")
                diag_lines.append(f"- Comentarios: {comentarios or '-'}")
                if informe_preliminar:
                    diag_lines.append(f"- Informe preliminar: {informe_preliminar}")
                if resolucion_label:
                    diag_lines.append(f"- Resolucion: {resolucion_label}")
                if motivo:
                    diag_lines.append(f"- Motivo de ingreso: {motivo}")
                lines.append("")
                lines.append("Diagnostico / estado actual:")
                lines.extend(diag_lines)
                try:
                    url = _frontend_url(request, f"/ingresos/{ingreso_id}") + "?tab=principal"
                    lines.append("")
                    lines.append(f"Hoja de servicio: {url}")
                except Exception:
                    pass
                body = "\n".join(lines)
                try:
                    body = _email_append_footer_text(body)
                except Exception:
                    pass
                recips = getattr(settings, "BAJA_NOTIFY_RECIPIENTS", []) or []
                if not isinstance(recips, (list, tuple)):
                    recips = [str(recips)] if recips else []
                recips = [r for r in recips if r]
                try:
                    notify_estado_patrimonial(ingreso_id, "alta", emails=recips)
                except Exception:
                    pass

                def _send_alta_email():
                    if not recips:
                        logger.warning(
                            "alta_notify_email no recipients configured",
                            extra={"ingreso_id": ingreso_id},
                        )
                        return
                    try:
                        _sent, _dbg = _send_mail_with_fallback(subject, body, recips)
                        logger.info(
                            "alta_notify_email sent=%s ingreso_id=%s recipients=%s backend=%s",
                            bool(_sent),
                            ingreso_id,
                            recips,
                            getattr(settings, "EMAIL_BACKEND", ""),
                        )
                    except Exception:
                        logger.exception(
                            "alta_notify_email failed",
                            extra={"ingreso_id": ingreso_id, "recipients": recips},
                        )

                try:
                    conn = transaction.get_connection()
                    if getattr(conn, "in_atomic_block", False):
                        transaction.on_commit(_send_alta_email)
                    else:
                        _send_alta_email()
                except Exception:
                    _send_alta_email()
            except Exception:
                logger.exception(
                    "alta_notify_email prepare failed",
                    extra={"ingreso_id": ingreso_id},
                )
        return Response({"ok": True, "estado": "ingresado"})


class IngresoCorreccionesHistoricasView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request, ingreso_id: int):
        require_permission(request, "action.ingreso.force_historical")
        _set_audit_user(request)

        if not _has_historical_corrections_schema():
            return Response(
                {
                    "detail": (
                        "Schema de correcciones históricas no aplicado. "
                        "Ejecuta apply_historical_corrections_schema."
                    )
                },
                status=500,
            )

        data = request.data or {}
        accion = str(data.get("accion") or "").strip().lower()
        if accion not in ("entrega", "alta_alquiler", "baja_alquiler", "baja_ingreso", "alta_ingreso"):
            return Response({"detail": "accion inválida"}, status=400)

        motivo = (data.get("motivo") or "").strip()
        if not motivo:
            return Response({"detail": "motivo requerido"}, status=400)

        fecha_efectiva = _parse_historical_effective_datetime(data.get("fecha_efectiva"))
        if not fecha_efectiva:
            return Response({"detail": "fecha_efectiva inválida"}, status=400)
        if fecha_efectiva > timezone.now():
            return Response({"detail": "fecha_efectiva no puede ser futura"}, status=400)

        notificar = _parse_bool_or_default(data.get("notificar"), True)
        uid = getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)

        row = q(
            "SELECT id, estado, COALESCE(alquilado,false) AS alquilado FROM ingresos WHERE id=%s",
            [ingreso_id],
            one=True,
        )
        if not row:
            return Response({"detail": "Ingreso no encontrado"}, status=404)

        de_estado = (row.get("estado") or "").lower()
        nuevo_estado = de_estado
        comentario = f"Corrección histórica forzada: {accion}"
        sets = []
        params = []
        dash_id = _dash_location_id()
        taller_id = _taller_location_id()

        if accion == "entrega":
            remito_salida = (data.get("remito_salida") or "").strip()
            factura_numero = (data.get("factura_numero") or "").strip()
            fecha_entrega = _parse_historical_effective_datetime(data.get("fecha_entrega"))
            if not fecha_entrega:
                fecha_entrega = fecha_efectiva
            sets.extend(
                [
                    "estado=%s",
                    "remito_salida=NULLIF(%s,'')",
                    "factura_numero=NULLIF(%s,'')",
                    "fecha_entrega=%s",
                ]
            )
            params.extend(["entregado", remito_salida, factura_numero, fecha_entrega])
            if dash_id:
                sets.append("ubicacion_id = COALESCE(%s, ubicacion_id)")
                params.append(dash_id)
            nuevo_estado = "entregado"

        elif accion == "alta_alquiler":
            alquiler_a = (data.get("alquiler_a") or "").strip()
            alquiler_remito = (data.get("alquiler_remito") or "").strip()
            alquiler_fecha_raw = data.get("alquiler_fecha")
            if alquiler_fecha_raw in (None, ""):
                alquiler_fecha = timezone.localtime(fecha_efectiva).date()
            else:
                if isinstance(alquiler_fecha_raw, date):
                    alquiler_fecha = alquiler_fecha_raw
                else:
                    parsed = parse_date(str(alquiler_fecha_raw).strip())
                    if not parsed:
                        parsed_dt = _parse_datetime_or_date(alquiler_fecha_raw)
                        parsed = parsed_dt.date() if parsed_dt else None
                    if not parsed:
                        return Response({"detail": "alquiler_fecha inválida"}, status=400)
                    alquiler_fecha = parsed
            sets.extend(
                [
                    "alquilado=TRUE",
                    "estado=%s",
                    "alquiler_a=NULLIF(%s,'')",
                    "alquiler_remito=NULLIF(%s,'')",
                    "alquiler_fecha=%s",
                ]
            )
            params.extend(["alquilado", alquiler_a, alquiler_remito, alquiler_fecha])
            if dash_id:
                sets.append("ubicacion_id = COALESCE(%s, ubicacion_id)")
                params.append(dash_id)
            nuevo_estado = "alquilado"

        elif accion == "baja_alquiler":
            sets.extend(
                [
                    "alquilado=FALSE",
                    "alquiler_a=NULL",
                    "alquiler_remito=NULL",
                    "alquiler_fecha=NULL",
                ]
            )
            if de_estado == "alquilado":
                sets.append("estado=%s")
                params.append("ingresado")
                if taller_id:
                    sets.append("ubicacion_id = COALESCE(%s, ubicacion_id)")
                    params.append(taller_id)
                nuevo_estado = "ingresado"
            else:
                nuevo_estado = de_estado or "ingresado"

        elif accion == "baja_ingreso":
            sets.append("estado=%s")
            params.append("baja")
            if dash_id:
                sets.append("ubicacion_id = COALESCE(%s, ubicacion_id)")
                params.append(dash_id)
            nuevo_estado = "baja"

        elif accion == "alta_ingreso":
            sets.append("estado=%s")
            params.append("ingresado")
            if taller_id:
                sets.append("ubicacion_id = COALESCE(%s, ubicacion_id)")
                params.append(taller_id)
            nuevo_estado = "ingresado"

        if not sets:
            return Response({"detail": "No se pudo determinar la corrección a aplicar"}, status=400)

        params.append(ingreso_id)
        exec_void(f"UPDATE ingresos SET {', '.join(sets)} WHERE id=%s", params)

        try:
            exec_void(
                """
                INSERT INTO ingreso_events (ticket_id, de_estado, a_estado, usuario_id, ts, comentario)
                VALUES (%s, NULLIF(%s,'')::ticket_state, %s::ticket_state, %s, %s, %s)
                """,
                [ingreso_id, de_estado, nuevo_estado, uid, fecha_efectiva, comentario],
            )
        except Exception:
            # Fallback para esquemas sin casts/columnas extendidas.
            try:
                exec_void(
                    """
                    INSERT INTO ingreso_events (ticket_id, a_estado, usuario_id, ts, comentario)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    [ingreso_id, nuevo_estado, uid, fecha_efectiva, comentario],
                )
            except Exception:
                pass

        payload_json = json.dumps(data, ensure_ascii=False, default=str)
        if connection.vendor == "postgresql":
            exec_void(
                """
                INSERT INTO ingreso_historical_corrections
                  (ingreso_id, accion, fecha_efectiva, motivo, payload, notificar, usuario_id)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
                """,
                [ingreso_id, accion, fecha_efectiva, motivo, payload_json, notificar, uid],
            )
        else:
            exec_void(
                """
                INSERT INTO ingreso_historical_corrections
                  (ingreso_id, accion, fecha_efectiva, motivo, payload, notificar, usuario_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                [ingreso_id, accion, fecha_efectiva, motivo, payload_json, notificar, uid],
            )

        if accion in ("baja_ingreso", "alta_ingreso") and notificar:
            _notify_estado_patrimonial(
                request,
                ingreso_id,
                "baja" if accion == "baja_ingreso" else "alta",
                fecha_evento=fecha_efectiva,
            )

        return Response(
            {
                "ok": True,
                "accion": accion,
                "estado_anterior": de_estado,
                "estado": nuevo_estado,
                "fecha_efectiva": fecha_efectiva,
                "notificar": bool(notificar),
            }
        )


class ListosParaRetiroView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def get(self, request):
        require_permission(request, "page.liberados")
        with connection.cursor() as cur:
            _set_audit_user(request)
            # Fallback sin vista: equipos en 'taller' con estado 'liberado'.
            # Devuelve columnas esperadas por el front: id, estado, resolucion,
            # cliente, numero_serie, numero_interno (MG), marca, modelo, tipo_equipo,
            # fecha_ingreso, fecha_listo (desde ingreso_events), fecha_entrega.
            cur.execute(
                """
                SELECT
                  t.id,
                  t.estado,
                  t.presupuesto_estado,
                  t.resolucion,
                  c.razon_social,
                  d.numero_serie,
                  COALESCE(d.numero_interno,'') AS numero_interno,
                  COALESCE(b.nombre,'') AS marca,
                  COALESCE(m.nombre,'') AS modelo,
                  COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                  COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,'')) AS equipo_variante,
                  ev.ts AS fecha_listo
                FROM ingresos t
                JOIN devices   d ON d.id = t.device_id
                JOIN customers c ON c.id = d.customer_id
                LEFT JOIN marcas b ON b.id = d.marca_id
                LEFT JOIN models m ON m.id = d.model_id
                LEFT JOIN locations loc ON loc.id = t.ubicacion_id
                LEFT JOIN (
                  SELECT ingreso_id, MAX(ts) AS ts
                    FROM ingreso_events
                   WHERE a_estado = 'liberado'
                   GROUP BY ingreso_id
                ) ev ON ev.ingreso_id = t.id
                WHERE LOWER(loc.nombre) = LOWER(%s)
                  AND t.estado = 'liberado'
                ORDER BY
                  LOWER(COALESCE(c.razon_social, '')) ASC,
                  t.id DESC
                """,
                ["taller"],
            )
            return Response(_fetchall_dicts(cur))


class GeneralPorClienteView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def get(self, request, customer_id):
        with connection.cursor() as cur:
            _set_audit_user(request)
            cur.execute(
                """
                SELECT
                  t.id, t.estado, t.presupuesto_estado, t.fecha_ingreso, t.ubicacion_id,
                  q.fecha_emitido AS presupuesto_fecha_emision,
                  COALESCE(loc.nombre,'') AS ubicacion_nombre,
                  c.id AS customer_id, c.razon_social,
                  d.numero_serie,
                  COALESCE(d.numero_interno,'') AS numero_interno,
                  COALESCE(b.nombre,'') AS marca,
                  COALESCE(m.nombre,'') AS modelo,
                  COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                  COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,'')) AS equipo_variante
                FROM ingresos t
                JOIN devices   d ON d.id = t.device_id
                JOIN customers c ON c.id = d.customer_id
                LEFT JOIN marcas b ON b.id = d.marca_id
                LEFT JOIN models m ON m.id = d.model_id
                LEFT JOIN quotes q ON q.id = (
                  SELECT q2.id FROM quotes q2
                  WHERE q2.ingreso_id = t.id
                  ORDER BY (q2.fecha_emitido IS NOT NULL) DESC, q2.fecha_emitido DESC, q2.id DESC
                  LIMIT 1
                )
                LEFT JOIN locations loc ON loc.id = t.ubicacion_id
                WHERE c.id = %s
                  AND LOWER(loc.nombre) = LOWER(%s)
                  AND t.estado NOT IN ('entregado','alquilado','baja')
                ORDER BY t.fecha_ingreso DESC;
                """,
                [customer_id, "taller"],
            )
            return Response(_fetchall_dicts(cur))


class GeneralPorClienteExportView(APIView):
    """
    Exporta a Excel (.xlsx) el "general por cliente" (no entregados / no alquilados).

    GET /api/clientes/<customer_id>/general/export/
      Parametros opcionales:
        - ids: lista separada por comas para limitar la exportaciÃ³n a esos ingresos
        - ids: lista separada por comas para limitar la exportacion a esos ingresos
    Columnas del Excel:
      OS, Cliente, Equipo, N/S, Estado, Fecha ingreso
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, customer_id):
        ids_raw = (request.GET.get("ids") or "").strip()
        ids = None
        if ids_raw:
            try:
                ids = [int(x) for x in ids_raw.split(",") if x.strip()]
            except Exception:
                return Response({"detail": "Parametro 'ids' invÃ¡lido"}, status=400)
            if len(ids) > 1000:
                return Response({"detail": "Demasiados IDs (maximo 1000)"}, status=400)
        with connection.cursor() as cur:
            _set_audit_user(request)
            if ids:
                cur.execute(
                    """
                    SELECT
                      t.id,
                      c.razon_social AS cliente,
                      COALESCE(b.nombre,'') AS marca,
                      COALESCE(m.nombre,'') AS modelo,
                      COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                      COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,'')) AS equipo_variante,
                      d.numero_serie,
                      COALESCE(d.numero_interno,'') AS numero_interno,
                      t.estado,
                      t.presupuesto_estado,
                      t.fecha_ingreso
                    FROM ingresos t
                    JOIN devices   d ON d.id = t.device_id
                    JOIN customers c ON c.id = d.customer_id
                    LEFT JOIN marcas b ON b.id = d.marca_id
                    LEFT JOIN models m ON m.id = d.model_id
                    LEFT JOIN locations loc ON loc.id = t.ubicacion_id
                   WHERE c.id = %s
                     AND t.id = ANY(%s)
                     AND LOWER(loc.nombre) = LOWER(%s)
                     AND t.estado NOT IN ('entregado','alquilado','baja')
                   ORDER BY t.fecha_ingreso DESC, t.id DESC
                    """,
                    [customer_id, ids, "taller"],
                )
            else:
                cur.execute(
                    """
                    SELECT
                      t.id,
                      c.razon_social AS cliente,
                      COALESCE(b.nombre,'') AS marca,
                      COALESCE(m.nombre,'') AS modelo,
                      COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                      COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,'')) AS equipo_variante,
                      d.numero_serie,
                      COALESCE(d.numero_interno,'') AS numero_interno,
                      t.estado,
                      t.presupuesto_estado,
                      t.fecha_ingreso
                    FROM ingresos t
                    JOIN devices   d ON d.id = t.device_id
                    JOIN customers c ON c.id = d.customer_id
                    LEFT JOIN marcas b ON b.id = d.marca_id
                    LEFT JOIN models m ON m.id = d.model_id
                    LEFT JOIN locations loc ON loc.id = t.ubicacion_id
                   WHERE c.id = %s
                     AND LOWER(loc.nombre) = LOWER(%s)
                     AND t.estado NOT IN ('entregado','alquilado','baja')
                   ORDER BY t.fecha_ingreso DESC, t.id DESC
                    """,
                    [customer_id, "taller"],
                )
            rows = _fetchall_dicts(cur)

        wb = Workbook()
        ws = wb.active
        ws.title = "General por cliente"
        headers = ["OS", "Cliente", "Equipo", "N/S", "Estado", "Presupuesto", "Fecha ingreso"]
        ws.append(headers)

        # formato de equipo y N/S via helpers _equipolabel_row y _ns_label

        def _presu_label(v):
            if not v:
                return "-"
            try:
                s = str(v).strip()
            except Exception:
                return str(v)
            if not s:
                return "-"
            if s == "presupuestado":
                return "Presupuestado"
            if s == "no_aplica":
                return "No aplica"
            return s[:1].upper() + s[1:]

        for r in rows:
            os_txt = os_label(r.get("id"))
            equipo = _equipolabel_row(r)
            ns_val = _ns_label(r)
            estado = (r.get("estado") or "-")
            presu = _presu_label(r.get("presupuesto_estado"))
            fecha = r.get("fecha_ingreso")
            if fecha is not None:
                try:
                    fecha_txt = fecha.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    fecha_txt = str(fecha)
            else:
                fecha_txt = "-"

            ws.append([
                os_txt,
                r.get("cliente") or "-",
                equipo,
                ns_val,
                estado,
                presu,
                fecha_txt,
            ])

        try:
            widths = [10, 40, 40, 20, 16, 16, 20]
            for idx, w in enumerate(widths, start=1):
                col = ws.column_dimensions[chr(64 + idx)]
                col.width = w
        except Exception:
            pass

        bio = BytesIO()
        wb.save(bio)
        bio.seek(0)

        fname = f"general_cliente_{customer_id}_{timezone.now().strftime('%Y%m%d_%H%M')}.xlsx"
        resp = HttpResponse(
            bio.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        resp["Content-Disposition"] = f"attachment; filename=\"{fname}\""
        return resp


class AprobadosParaRepararView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def get(self, request):
        has_permite_reparacion = _ingresos_has_permite_reparacion_col()
        filtro_cotizacion = ""
        if has_permite_reparacion:
            filtro_cotizacion = """
                  AND NOT (
                        LOWER(CAST(t.motivo AS TEXT)) IN ('cotización de equipo', 'cotizacion de equipo')
                    AND COALESCE(t.permite_reparacion, TRUE) = FALSE
                  )
            """
        with connection.cursor() as cur:
            _set_audit_user(request)
            cur.execute(
                f"""
                SELECT
                  t.id,
                  t.estado,
                  t.presupuesto_estado,
                  c.razon_social,
                  d.numero_serie,
                  COALESCE(d.numero_interno,'') AS numero_interno,
                  COALESCE(b.nombre,'') AS marca,
                  COALESCE(m.nombre,'') AS modelo,
                  COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                  COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,'')) AS equipo_variante,
                  t.fecha_ingreso,
                  q.fecha_aprobado AS fecha_aprobacion
                FROM ingresos t
                JOIN devices d ON d.id=t.device_id
                JOIN customers c ON c.id=d.customer_id
                LEFT JOIN marcas b ON b.id=d.marca_id
                LEFT JOIN models m ON m.id=d.model_id
                LEFT JOIN quotes q ON q.id = (
                  SELECT q2.id FROM quotes q2
                  WHERE q2.ingreso_id = t.id
                  ORDER BY (q2.fecha_emitido IS NOT NULL) DESC, q2.fecha_emitido DESC, q2.id DESC
                  LIMIT 1
                )
                LEFT JOIN locations loc ON loc.id = t.ubicacion_id
                WHERE LOWER(loc.nombre) = LOWER(%s)
                  AND (
                        (t.presupuesto_estado = 'aprobado'
                        AND t.estado NOT IN ('reparado','entregado','derivado','liberado','alquilado','baja'))
                        OR t.estado = 'reparar'
                      )
                  AND t.estado NOT IN ('reparado','entregado','derivado','liberado','alquilado','baja')
                  {filtro_cotizacion}
                ORDER BY COALESCE(q.fecha_aprobado, t.fecha_ingreso) ASC;
            """,
                ["taller"],
            )
            data = _fetchall_dicts(cur)
        return Response(IngresoListItemSerializer(data, many=True).data)


class AprobadosYReparadosView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def get(self, request):
        with connection.cursor() as cur:
            _set_audit_user(request)
            cur.execute(
                """
              SELECT t.id, t.estado, t.presupuesto_estado,
                     c.razon_social,
                     d.numero_serie,
                     COALESCE(d.numero_interno,'') AS numero_interno,
                     COALESCE(b.nombre,'') AS marca,
                     COALESCE(m.nombre,'') AS modelo,
                     COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                     COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,'')) AS equipo_variante,
                     t.fecha_ingreso,
                     ev.fecha_reparado
              FROM ingresos t
              JOIN devices d ON d.id=t.device_id
              JOIN customers c ON c.id=d.customer_id
              LEFT JOIN marcas b ON b.id=d.marca_id
              LEFT JOIN models m ON m.id=d.model_id
              LEFT JOIN locations loc ON loc.id = t.ubicacion_id
              LEFT JOIN (
                SELECT ingreso_id, MAX(ts) AS fecha_reparado
                FROM ingreso_events
                WHERE a_estado='reparado'
                GROUP BY ingreso_id
              ) ev ON ev.ingreso_id = t.id
              WHERE LOWER(loc.nombre) = LOWER(%s)
                AND t.estado IN ('reparado')
              ORDER BY (ev.fecha_reparado IS NULL) ASC, ev.fecha_reparado ASC, t.fecha_ingreso ASC;
            """,
                ["taller"],
            )
            data = _fetchall_dicts(cur)
        return Response(IngresoListItemSerializer(data, many=True).data)


class AprobadosView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        with connection.cursor() as cur:
            _set_audit_user(request)
            cur.execute(
                """
                SELECT
                  t.id, t.estado, t.presupuesto_estado,
                  c.razon_social, d.numero_serie,
                  COALESCE(d.numero_interno,'') AS numero_interno,
                  COALESCE(b.nombre,'') AS marca,
                  COALESCE(m.nombre,'') AS modelo,
                  COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                  COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,'')) AS equipo_variante,
                  t.fecha_ingreso,
                  qa.fecha_aprobado
                FROM ingresos t
                JOIN devices d ON d.id = t.device_id
                JOIN customers c ON c.id = d.customer_id
                LEFT JOIN marcas b ON b.id = d.marca_id
                LEFT JOIN models m ON m.id = d.model_id
                LEFT JOIN locations loc ON loc.id = t.ubicacion_id
                LEFT JOIN (
                  SELECT DISTINCT ON (ingreso_id) ingreso_id, fecha_aprobado
                  FROM quotes
                  WHERE estado = 'aprobado'
                  ORDER BY ingreso_id, fecha_aprobado DESC
                ) qa ON qa.ingreso_id = t.id
                WHERE t.presupuesto_estado = 'aprobado'
                  AND t.estado NOT IN ('liberado','entregado','alquilado','baja')
                  AND LOWER(loc.nombre) = LOWER(%s)
                ORDER BY
                    COALESCE(qa.fecha_aprobado, t.fecha_ingreso) NULLS LAST,
                    t.fecha_ingreso,
                    t.id;
                """,
                ["taller"],
            )
            data = _fetchall_dicts(cur)
        return Response(IngresoListItemSerializer(data, many=True).data)

class LiberadosView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def get(self, request):
        require_permission(request, "page.liberados")
        with connection.cursor() as cur:
            _set_audit_user(request)
            cur.execute(
                """
                SELECT
                  t.id,
                  t.estado,
                  t.presupuesto_estado,
                  c.razon_social,
                  d.numero_serie,
                  COALESCE(d.numero_interno,'') AS numero_interno,
                  COALESCE(b.nombre,'') AS marca,
                  COALESCE(m.nombre,'') AS modelo,
                  COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                  COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,'')) AS equipo_variante,
                  t.fecha_ingreso,
                  t.fecha_entrega
                FROM ingresos t
                JOIN devices d ON d.id=t.device_id
                JOIN customers c ON c.id=d.customer_id
                LEFT JOIN marcas b ON b.id=d.marca_id
                LEFT JOIN models m ON m.id=d.model_id
                LEFT JOIN locations loc ON loc.id = t.ubicacion_id
                WHERE LOWER(loc.nombre) = LOWER(%s)
                  AND t.estado IN ('liberado')
                ORDER BY t.fecha_entrega DESC, t.id DESC;
                """,
                ["taller"],
            )
            return Response(_fetchall_dicts(cur))


class GeneralEquiposView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def get(self, request):
        require_any_permission(request, ["page.ingresos_history", "page.logistics"])
        ubic = (request.GET.get("ubicacion") or "").strip()
        ubic_id_raw = (request.GET.get("ubicacion_id") or "").strip()
        estado_raw = (request.GET.get("estado") or "").strip()
        excluir_estados_raw = (request.GET.get("excluir_estados") or "").strip()
        solo_taller_raw = (request.GET.get("solo_taller") or "").strip().lower()
        q_raw = (request.GET.get("q") or "").strip()
        delivered_raw = (request.GET.get("delivered") or "").strip().lower()
        from_raw = (request.GET.get("from") or "").strip()
        to_raw = (request.GET.get("to") or "").strip()
        fecha_ingreso_from_raw = (request.GET.get("fecha_ingreso_from") or "").strip()
        fecha_ingreso_to_raw = (request.GET.get("fecha_ingreso_to") or "").strip()
        fecha_liberacion_from_raw = (request.GET.get("fecha_liberacion_from") or "").strip()
        fecha_liberacion_to_raw = (request.GET.get("fecha_liberacion_to") or "").strip()
        fecha_entrega_from_raw = (request.GET.get("fecha_entrega_from") or "").strip()
        fecha_entrega_to_raw = (request.GET.get("fecha_entrega_to") or "").strip()
        os_raw = (request.GET.get("os") or "").strip()
        cliente_raw = (request.GET.get("cliente") or "").strip()
        tipo_equipo_raw = (request.GET.get("tipo_equipo") or "").strip()
        marca_raw = (request.GET.get("marca") or "").strip()
        modelo_raw = (request.GET.get("modelo") or "").strip()
        variante_raw = (request.GET.get("variante") or "").strip()
        estado_q_raw = (request.GET.get("estado_q") or "").strip()
        numero_serie_raw = (request.GET.get("numero_serie") or "").strip()
        numero_interno_raw = (request.GET.get("numero_interno") or "").strip()

        # paginaciÃ³n opcional (se mantiene respuesta original si no se especifica page_size)
        page_raw = (request.GET.get("page") or "").strip()
        page_size_raw = (request.GET.get("page_size") or "").strip()

        estados = [e.strip().lower() for e in estado_raw.split(",") if e.strip()] if estado_raw else []
        excluir_estados = [e.strip().lower() for e in excluir_estados_raw.split(",") if e.strip()] if excluir_estados_raw else []
        solo_taller_param_present = ("solo_taller" in request.GET)
        solo_taller = solo_taller_raw in ("1", "true", "yes", "y", "t")
        delivered = delivered_raw in ("1", "true", "yes", "y", "t")
        page = int(page_raw) if page_raw.isdigit() and int(page_raw) > 0 else 1
        try:
            page_size = int(page_size_raw) if page_size_raw else 0
        except Exception:
            page_size = 0
        if page_size < 0:
            page_size = 0
        if page_size > 0:
            # cota superior razonable
            page_size = min(page_size, 500)

        with connection.cursor() as cur:
            _set_audit_user(request)
            wh, params = [], []
            if delivered and not (fecha_entrega_from_raw or fecha_entrega_to_raw):
                fecha_entrega_from_raw = from_raw
                fecha_entrega_to_raw = to_raw

            def _apply_date_range(field_sql, from_val, to_val):
                if from_val:
                    try:
                        f = parse_date(from_val)
                        if f:
                            wh.append(f"DATE({field_sql}) >= %s")
                            params.append(f.isoformat())
                    except Exception:
                        pass
                if to_val:
                    try:
                        tdt = parse_date(to_val)
                        if tdt:
                            wh.append(f"DATE({field_sql}) <= %s")
                            params.append(tdt.isoformat())
                    except Exception:
                        pass
            def _apply_ilike(field_sql, value):
                if value:
                    wh.append(f"{field_sql} ILIKE %s")
                    params.append(f"%{value}%")
            if ubic:
                wh.append("LOWER(loc.nombre) = LOWER(%s)")
                params.append(ubic)
            else:
                # Compat: permitir filtrar por ubicacion_id (numÃ©rico)
                try:
                    if ubic_id_raw and str(int(ubic_id_raw)) == ubic_id_raw:
                        wh.append("t.ubicacion_id = %s")
                        params.append(int(ubic_id_raw))
                except Exception:
                    pass
            # Optional 'solo_taller' filter: only apply when explicitly requested
            if not (ubic or (ubic_id_raw and ubic_id_raw.isdigit())):
                if solo_taller_param_present and solo_taller:
                    wh.append("LOWER(loc.nombre) = LOWER(%s)")
                    params.append("taller")
            if estados:
                placeholders = ",".join(["%s"] * len(estados))
                wh.append(f"LOWER(TRIM(COALESCE(t.estado::text,''))) IN ({placeholders})")
                params.extend(estados)
            if excluir_estados:
                placeholders = ",".join(["%s"] * len(excluir_estados))
                wh.append(f"LOWER(TRIM(COALESCE(t.estado::text,''))) NOT IN ({placeholders})")
                params.extend(excluir_estados)
            # Busqueda flexible por varios campos (OS, cliente, equipo, estado, serie, interno, ubicacion).
            if q_raw:
                needle = q_raw.strip()
                if needle:
                    needle_ns = needle.replace(" ", "").upper()
                    import re as _re
                    m_mg = _re.match(r"^MG\s*(\d{4})$", needle, _re.IGNORECASE)
                    m_os = _re.match(r"^(?:OS\s*)?(\d+)$", needle, _re.IGNORECASE)
                    if m_mg:
                        mg_no_space = ("MG" + m_mg.group(1)).upper()
                        wh.append("("
                                  "REPLACE(UPPER(COALESCE(d.numero_interno,'')),' ','') = %s OR "
                                  "REPLACE(UPPER(COALESCE(d.numero_serie,'')),' ','') = %s)")
                        params.extend([mg_no_space, mg_no_space])
                    else:
                        like = f"%{needle}%"
                        like_ns = f"%{needle_ns}%"
                        clauses = []
                        if m_os:
                            clauses.append("t.id = %s")
                        clauses.extend([
                            "t.id::text ILIKE %s",
                            "COALESCE(c.razon_social,'') ILIKE %s",
                            "COALESCE(b.nombre,'') ILIKE %s",
                            "COALESCE(m.nombre,'') ILIKE %s",
                            "COALESCE(m.tipo_equipo,'') ILIKE %s",
                            "COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,''), '') ILIKE %s",
                            "COALESCE(t.estado::text,'') ILIKE %s",
                            "COALESCE(d.numero_serie,'') ILIKE %s",
                            "COALESCE(d.numero_interno,'') ILIKE %s",
                            "COALESCE(loc.nombre,'') ILIKE %s",
                            "REPLACE(UPPER(COALESCE(d.numero_interno,'')),' ','') LIKE %s",
                            "REPLACE(UPPER(COALESCE(d.numero_serie,'')),' ','') LIKE %s",
                        ])
                        wh.append("(" + " OR ".join(clauses) + ")")
                        if m_os:
                            params.append(int(m_os.group(1)))
                        params.extend([like] * 10 + [like_ns, like_ns])

            if os_raw:
                try:
                    import re as _re
                    m_os = _re.match(r"^(?:OS\s*)?(\d+)$", os_raw, _re.IGNORECASE)
                except Exception:
                    m_os = None
                if m_os:
                    wh.append("t.id = %s")
                    params.append(int(m_os.group(1)))
                else:
                    wh.append("t.id::text ILIKE %s")
                    params.append(f"%{os_raw}%")
            _apply_ilike("COALESCE(c.razon_social,'')", cliente_raw)
            _apply_ilike("COALESCE(m.tipo_equipo,'')", tipo_equipo_raw)
            _apply_ilike("COALESCE(b.nombre,'')", marca_raw)
            _apply_ilike("COALESCE(m.nombre,'')", modelo_raw)
            _apply_ilike(
                "COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,''), '')",
                variante_raw,
            )
            _apply_ilike("COALESCE(t.estado::text,'')", estado_q_raw)
            _apply_ilike("COALESCE(d.numero_serie,'')", numero_serie_raw)
            _apply_ilike("COALESCE(d.numero_interno,'')", numero_interno_raw)

            # filtros por fechas (rangos)
            _apply_date_range("t.fecha_ingreso", fecha_ingreso_from_raw, fecha_ingreso_to_raw)
            _apply_date_range("ev_lib.fecha_liberacion", fecha_liberacion_from_raw, fecha_liberacion_to_raw)
            _apply_date_range("t.fecha_entrega", fecha_entrega_from_raw, fecha_entrega_to_raw)

            if delivered:
                wh.append("t.fecha_entrega IS NOT NULL")

            where_sql = (" WHERE " + " AND ".join(wh)) if wh else ""

            order_sql = "ORDER BY t.id DESC"
            if delivered:
                order_sql = "ORDER BY t.id DESC"

            limit_sql = ""
            limit_params = []
            overfetch = 0
            if page_size > 0:
                overfetch = 1
                limit_sql = " LIMIT %s OFFSET %s"
                limit_params.extend([page_size + overfetch, max(0, (page - 1) * page_size)])

            sql = f"""
                SELECT
                  t.id, t.estado, t.presupuesto_estado, t.fecha_ingreso, ev_lib.fecha_liberacion, t.fecha_entrega, t.ubicacion_id,
                  q.fecha_emitido AS presupuesto_fecha_emision,
                  COALESCE(loc.nombre,'') AS ubicacion_nombre,
                  c.id AS customer_id, c.razon_social,
                  d.numero_serie,
                  COALESCE(d.numero_interno,'') AS numero_interno,
                  COALESCE(b.nombre,'') AS marca,
                  COALESCE(m.nombre,'') AS modelo,
                  COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                  COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,'')) AS equipo_variante
                FROM ingresos t
                JOIN devices   d ON d.id = t.device_id
                JOIN customers c ON c.id = d.customer_id
                LEFT JOIN marcas b ON b.id = d.marca_id
                LEFT JOIN models m ON m.id = d.model_id
                LEFT JOIN quotes q ON q.id = (
                  SELECT q2.id FROM quotes q2
                  WHERE q2.ingreso_id = t.id
                  ORDER BY (q2.fecha_emitido IS NOT NULL) DESC, q2.fecha_emitido DESC, q2.id DESC
                  LIMIT 1
                )
                LEFT JOIN (
                  SELECT ingreso_id, MAX(ts) AS fecha_liberacion
                  FROM ingreso_events
                  WHERE a_estado = 'liberado'
                  GROUP BY ingreso_id
                ) ev_lib ON ev_lib.ingreso_id = t.id
                LEFT JOIN locations loc ON loc.id = t.ubicacion_id
                {where_sql}
                {order_sql}
                {limit_sql}
            """
            cur.execute(sql, params + limit_params)
            rows = _fetchall_dicts(cur)
        # compat: si no hay paginaciÃ³n pedida, seguir devolviendo lista
        if page_size == 0:
            return Response(rows)
        has_next = False
        if len(rows) > page_size:
            has_next = True
            rows = rows[:page_size]
        return Response({
            "items": rows,
            "page": page,
            "page_size": page_size,
            "has_next": bool(has_next),
        })


class GeneralEquiposExportView(APIView):
    """
    Exporta el historico de ingresos en Excel (.xlsx) respetando los mismos
    filtros de GeneralEquiposView.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        ubic = (request.GET.get("ubicacion") or "").strip()
        ubic_id_raw = (request.GET.get("ubicacion_id") or "").strip()
        estado_raw = (request.GET.get("estado") or "").strip()
        excluir_estados_raw = (request.GET.get("excluir_estados") or "").strip()
        solo_taller_raw = (request.GET.get("solo_taller") or "").strip().lower()
        q_raw = (request.GET.get("q") or "").strip()
        delivered_raw = (request.GET.get("delivered") or "").strip().lower()
        from_raw = (request.GET.get("from") or "").strip()
        to_raw = (request.GET.get("to") or "").strip()
        fecha_ingreso_from_raw = (request.GET.get("fecha_ingreso_from") or "").strip()
        fecha_ingreso_to_raw = (request.GET.get("fecha_ingreso_to") or "").strip()
        fecha_liberacion_from_raw = (request.GET.get("fecha_liberacion_from") or "").strip()
        fecha_liberacion_to_raw = (request.GET.get("fecha_liberacion_to") or "").strip()
        fecha_entrega_from_raw = (request.GET.get("fecha_entrega_from") or "").strip()
        fecha_entrega_to_raw = (request.GET.get("fecha_entrega_to") or "").strip()
        os_raw = (request.GET.get("os") or "").strip()
        cliente_raw = (request.GET.get("cliente") or "").strip()
        tipo_equipo_raw = (request.GET.get("tipo_equipo") or "").strip()
        marca_raw = (request.GET.get("marca") or "").strip()
        modelo_raw = (request.GET.get("modelo") or "").strip()
        variante_raw = (request.GET.get("variante") or "").strip()
        estado_q_raw = (request.GET.get("estado_q") or "").strip()
        numero_serie_raw = (request.GET.get("numero_serie") or "").strip()
        numero_interno_raw = (request.GET.get("numero_interno") or "").strip()

        estados = [e.strip().lower() for e in estado_raw.split(",") if e.strip()] if estado_raw else []
        excluir_estados = [e.strip().lower() for e in excluir_estados_raw.split(",") if e.strip()] if excluir_estados_raw else []
        solo_taller_param_present = ("solo_taller" in request.GET)
        solo_taller = solo_taller_raw in ("1", "true", "yes", "y", "t")
        delivered = delivered_raw in ("1", "true", "yes", "y", "t")

        with connection.cursor() as cur:
            _set_audit_user(request)
            wh, params = [], []
            if delivered and not (fecha_entrega_from_raw or fecha_entrega_to_raw):
                fecha_entrega_from_raw = from_raw
                fecha_entrega_to_raw = to_raw

            def _apply_date_range(field_sql, from_val, to_val):
                if from_val:
                    try:
                        f = parse_date(from_val)
                        if f:
                            wh.append(f"DATE({field_sql}) >= %s")
                            params.append(f.isoformat())
                    except Exception:
                        pass
                if to_val:
                    try:
                        tdt = parse_date(to_val)
                        if tdt:
                            wh.append(f"DATE({field_sql}) <= %s")
                            params.append(tdt.isoformat())
                    except Exception:
                        pass

            def _apply_ilike(field_sql, value):
                if value:
                    wh.append(f"{field_sql} ILIKE %s")
                    params.append(f"%{value}%")

            if ubic:
                wh.append("LOWER(loc.nombre) = LOWER(%s)")
                params.append(ubic)
            else:
                try:
                    if ubic_id_raw and str(int(ubic_id_raw)) == ubic_id_raw:
                        wh.append("t.ubicacion_id = %s")
                        params.append(int(ubic_id_raw))
                except Exception:
                    pass

            if not (ubic or (ubic_id_raw and ubic_id_raw.isdigit())):
                if solo_taller_param_present and solo_taller:
                    wh.append("LOWER(loc.nombre) = LOWER(%s)")
                    params.append("taller")

            if estados:
                placeholders = ",".join(["%s"] * len(estados))
                wh.append(f"LOWER(TRIM(COALESCE(t.estado::text,''))) IN ({placeholders})")
                params.extend(estados)
            if excluir_estados:
                placeholders = ",".join(["%s"] * len(excluir_estados))
                wh.append(f"LOWER(TRIM(COALESCE(t.estado::text,''))) NOT IN ({placeholders})")
                params.extend(excluir_estados)

            if q_raw:
                needle = q_raw.strip()
                if needle:
                    needle_ns = needle.replace(" ", "").upper()
                    import re as _re
                    m_mg = _re.match(r"^MG\s*(\d{4})$", needle, _re.IGNORECASE)
                    m_os = _re.match(r"^(?:OS\s*)?(\d+)$", needle, _re.IGNORECASE)
                    if m_mg:
                        mg_no_space = ("MG" + m_mg.group(1)).upper()
                        wh.append("("
                                  "REPLACE(UPPER(COALESCE(d.numero_interno,'')),' ','') = %s OR "
                                  "REPLACE(UPPER(COALESCE(d.numero_serie,'')),' ','') = %s)")
                        params.extend([mg_no_space, mg_no_space])
                    else:
                        like = f"%{needle}%"
                        like_ns = f"%{needle_ns}%"
                        clauses = []
                        if m_os:
                            clauses.append("t.id = %s")
                        clauses.extend([
                            "t.id::text ILIKE %s",
                            "COALESCE(c.razon_social,'') ILIKE %s",
                            "COALESCE(b.nombre,'') ILIKE %s",
                            "COALESCE(m.nombre,'') ILIKE %s",
                            "COALESCE(m.tipo_equipo,'') ILIKE %s",
                            "COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,''), '') ILIKE %s",
                            "COALESCE(t.estado::text,'') ILIKE %s",
                            "COALESCE(d.numero_serie,'') ILIKE %s",
                            "COALESCE(d.numero_interno,'') ILIKE %s",
                            "COALESCE(loc.nombre,'') ILIKE %s",
                            "REPLACE(UPPER(COALESCE(d.numero_interno,'')),' ','') LIKE %s",
                            "REPLACE(UPPER(COALESCE(d.numero_serie,'')),' ','') LIKE %s",
                        ])
                        wh.append("(" + " OR ".join(clauses) + ")")
                        if m_os:
                            params.append(int(m_os.group(1)))
                        params.extend([like] * 10 + [like_ns, like_ns])

            if os_raw:
                try:
                    import re as _re
                    m_os = _re.match(r"^(?:OS\s*)?(\d+)$", os_raw, _re.IGNORECASE)
                except Exception:
                    m_os = None
                if m_os:
                    wh.append("t.id = %s")
                    params.append(int(m_os.group(1)))
                else:
                    wh.append("t.id::text ILIKE %s")
                    params.append(f"%{os_raw}%")

            _apply_ilike("COALESCE(c.razon_social,'')", cliente_raw)
            _apply_ilike("COALESCE(m.tipo_equipo,'')", tipo_equipo_raw)
            _apply_ilike("COALESCE(b.nombre,'')", marca_raw)
            _apply_ilike("COALESCE(m.nombre,'')", modelo_raw)
            _apply_ilike(
                "COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,''), '')",
                variante_raw,
            )
            _apply_ilike("COALESCE(t.estado::text,'')", estado_q_raw)
            _apply_ilike("COALESCE(d.numero_serie,'')", numero_serie_raw)
            _apply_ilike("COALESCE(d.numero_interno,'')", numero_interno_raw)

            _apply_date_range("t.fecha_ingreso", fecha_ingreso_from_raw, fecha_ingreso_to_raw)
            _apply_date_range("ev_lib.fecha_liberacion", fecha_liberacion_from_raw, fecha_liberacion_to_raw)
            _apply_date_range("t.fecha_entrega", fecha_entrega_from_raw, fecha_entrega_to_raw)

            if delivered:
                wh.append("t.fecha_entrega IS NOT NULL")

            where_sql = (" WHERE " + " AND ".join(wh)) if wh else ""

            sql = f"""
                SELECT
                  t.id, t.estado, t.presupuesto_estado, t.fecha_ingreso, ev_lib.fecha_liberacion, t.fecha_entrega, t.ubicacion_id,
                  COALESCE(loc.nombre,'') AS ubicacion_nombre,
                  c.id AS customer_id, c.razon_social,
                  d.numero_serie,
                  COALESCE(d.numero_interno,'') AS numero_interno,
                  COALESCE(b.nombre,'') AS marca,
                  COALESCE(m.nombre,'') AS modelo,
                  COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                  COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,'')) AS equipo_variante
                FROM ingresos t
                JOIN devices   d ON d.id = t.device_id
                JOIN customers c ON c.id = d.customer_id
                LEFT JOIN marcas b ON b.id = d.marca_id
                LEFT JOIN models m ON m.id = d.model_id
                LEFT JOIN (
                  SELECT ingreso_id, MAX(ts) AS fecha_liberacion
                  FROM ingreso_events
                  WHERE a_estado = 'liberado'
                  GROUP BY ingreso_id
                ) ev_lib ON ev_lib.ingreso_id = t.id
                LEFT JOIN locations loc ON loc.id = t.ubicacion_id
                {where_sql}
                ORDER BY t.id DESC
            """
            cur.execute(sql, params)
            rows = _fetchall_dicts(cur)

        wb = Workbook()
        ws = wb.active
        ws.title = "Historico ingresos"
        ws.append([
            "OS",
            "Cliente",
            "Tipo de equipo",
            "Marca",
            "Modelo",
            "Variante",
            "Estado",
            "N/S (serie)",
            "MG",
            "Fecha ingreso",
            "Fecha liberacion",
            "Fecha entrega",
        ])

        def _dt_txt(value):
            if value is None:
                return "-"
            try:
                return value.strftime("%Y-%m-%d %H:%M")
            except Exception:
                return str(value)

        for r in rows:
            ws.append([
                os_label(r.get("id")),
                r.get("razon_social") or "-",
                r.get("tipo_equipo") or "-",
                r.get("marca") or "-",
                r.get("modelo") or "-",
                r.get("equipo_variante") or "-",
                r.get("estado") or "-",
                r.get("numero_serie") or "-",
                r.get("numero_interno") or "-",
                _dt_txt(r.get("fecha_ingreso")),
                _dt_txt(r.get("fecha_liberacion")),
                _dt_txt(r.get("fecha_entrega")),
            ])

        try:
            widths = [10, 40, 20, 20, 24, 24, 18, 22, 14, 20, 20, 20]
            for idx, width in enumerate(widths, start=1):
                ws.column_dimensions[chr(64 + idx)].width = width
        except Exception:
            pass

        bio = BytesIO()
        wb.save(bio)
        bio.seek(0)
        fname = f"historico_ingresos_{timezone.now().strftime('%Y%m%d_%H%M')}.xlsx"
        resp = HttpResponse(
            bio.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        resp["Content-Disposition"] = f"attachment; filename=\"{fname}\""
        return resp


class IngresoAsignarTecnicoView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def patch(self, request, ingreso_id):
        require_roles(request, ["jefe", "admin"]) 
        tecnico_id = request.data.get("tecnico_id")
        if tecnico_id is None:
            return Response({"detail": "tecnico_id requerido"}, status=400)
        ok = q("SELECT id FROM users WHERE id=%s AND activo=true AND rol IN ('tecnico','jefe')",
                [tecnico_id], one=True)
        if not ok:
            return Response({"detail": "tÃ©cnico invÃ¡lido"}, status=400)
        try:
            prev = q("SELECT asignado_a FROM ingresos WHERE id=%s", [ingreso_id], one=True)
            logger.info(f"[AsignarTecnico] ingreso={ingreso_id} prev={prev and prev.get('asignado_a')} new={tecnico_id}")
        except Exception:
            logger.warning(f"[AsignarTecnico] ingreso={ingreso_id} could not read previous asignado_a")
        _set_audit_user(request)
        exec_void("UPDATE ingresos SET asignado_a=%s WHERE id=%s", [tecnico_id, ingreso_id])
        # Log post-commit para verificar persistencia real en DB
        try:
            def _post_commit_log():
                try:
                    row_pc = q("SELECT asignado_a FROM ingresos WHERE id=%s", [ingreso_id], one=True)
                    logger.info(f"[AsignarTecnico][post_commit] ingreso={ingreso_id} persisted={row_pc and row_pc.get('asignado_a')}")
                except Exception:
                    logger.warning(f"[AsignarTecnico][post_commit] ingreso={ingreso_id} read failed after commit")
            transaction.on_commit(_post_commit_log)
        except Exception:
            pass
        try:
            post = q("SELECT asignado_a FROM ingresos WHERE id=%s", [ingreso_id], one=True)
            logger.info(f"[AsignarTecnico] ingreso={ingreso_id} after_update={post and post.get('asignado_a')}")
        except Exception:
            logger.warning(f"[AsignarTecnico] ingreso={ingreso_id} could not read asignado_a after update")
        # Marcar solicitud aceptada si existe la tabla auxiliar (aislado en savepoint)
        try:
            with transaction.atomic():
                exec_void(
                    """
                    UPDATE ingreso_assignment_requests
                       SET accepted_at = now(), status = 'aceptado'
                     WHERE ingreso_id = %s
                       AND usuario_id = %s
                       AND accepted_at IS NULL
                       AND canceled_at IS NULL
                    """,
                    [ingreso_id, tecnico_id],
                )
        except Exception:
            # si la tabla no existe o falla, no romper la transacciÃ³n principal
            pass
        try:
            who = q("SELECT COALESCE(nombre,'') AS nombre, COALESCE(email,'') AS email FROM users WHERE id=%s", [tecnico_id], one=True)
            nombre = (who and who.get("nombre")) or ""
            email_to = (who and who.get("email")) or ""
        except Exception:
            nombre = ""
            email_to = ""
        email_sent = False
        email_debug = {}
        try:
            notify_on_assign = getattr(settings, "ASSIGNMENT_NOTIFY_ON_ASSIGN", "1")
            notify_on_assign = str(notify_on_assign).lower() in ("1", "true", "yes")
        except Exception:
            notify_on_assign = True
        try:
            prev_id = prev and prev.get("asignado_a")
        except Exception:
            prev_id = None
        if notify_on_assign and (str(prev_id or "") != str(tecnico_id)):
            try:
                notify_ingreso_asignado(ingreso_id, tecnico_id)
            except Exception:
                pass
        if notify_on_assign and email_to and (str(prev_id or "") != str(tecnico_id)):
            try:
                info = q(
                    """
                    SELECT c.razon_social AS cliente,
                           COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                           COALESCE(b.nombre,'') AS marca,
                           COALESCE(m.nombre,'') AS modelo,
                           COALESCE(d.numero_serie,'') AS numero_serie,
                           COALESCE(d.numero_interno,'') AS numero_interno
                      FROM ingresos t
                      JOIN devices d   ON d.id = t.device_id
                      JOIN customers c ON c.id = d.customer_id
                      LEFT JOIN marcas b ON b.id = d.marca_id
                      LEFT JOIN models m ON m.id = d.model_id
                     WHERE t.id=%s
                    """,
                    [ingreso_id],
                    one=True,
                ) or {}
                os_txt = os_label(ingreso_id)
                cliente = info.get("cliente") or ""
                equipo = " | ".join([p for p in [info.get("tipo_equipo") or "", info.get("marca") or "", info.get("modelo") or ""] if p])
                ns = info.get("numero_serie") or ""
                subject = f"NS: {ns or os_txt} fue reasignado a vos"
                lines = [
                    f"Te asignaron/reasignaron un ingreso.",
                    f"OS: {os_txt}",
                    f"Cliente: {cliente}",
                    f"Equipo: {equipo or '-'}",
                    f"N/S: {ns or '-'}",
                ]
                try:
                    url = _frontend_url(request, f"/ingresos/{ingreso_id}") + "?tab=principal"
                    lines.append("")
                    lines.append(f"Abrir hoja: {url}")
                except Exception:
                    pass
                body = "\n".join(lines)
                try:
                    body = _email_append_footer_text(body)
                except Exception:
                    pass
                cc_list = getattr(settings, "ASSIGNMENT_NOTIFY_CC", []) or []
                if not isinstance(cc_list, (list, tuple)):
                    cc_list = [str(cc_list)] if cc_list else []
                recips = [email_to] + [x for x in cc_list if x]
                try:
                    email_debug.update({
                        "backend": getattr(settings, "EMAIL_BACKEND", None),
                        "host": getattr(settings, "EMAIL_HOST", None),
                        "port": getattr(settings, "EMAIL_PORT", None),
                        "use_tls": getattr(settings, "EMAIL_USE_TLS", None),
                        "use_ssl": getattr(settings, "EMAIL_USE_SSL", None),
                        "from": getattr(settings, "DEFAULT_FROM_EMAIL", None),
                        "recipients": list(recips),
                    })
                except Exception:
                    pass
                if recips:
                    try:
                        _sent, _dbg = _send_mail_with_fallback(subject, body, recips)
                        email_sent = bool(_sent)
                        try:
                            email_debug.update(_dbg or {})
                        except Exception:
                            pass
                        logger.info("assign_notify sent=%s ingreso_id=%s to=%s", email_sent, ingreso_id, email_to)
                    except Exception as e:
                        try:
                            email_debug["error"] = str(e)
                            email_debug["exception"] = e.__class__.__name__
                        except Exception:
                            pass
                        logger.exception("assign_notify failed", extra={"ingreso_id": ingreso_id, "to": email_to, "recipients": recips})
                        email_sent = False
                else:
                    logger.warning("assign_notify no recipient email configured", extra={"ingreso_id": ingreso_id})
            except Exception as e:
                try:
                    email_debug["error"] = str(e)
                    email_debug["exception"] = e.__class__.__name__
                except Exception:
                    pass
                email_sent = False
        try:
            tecnico_id_int = int(tecnico_id)
        except Exception:
            tecnico_id_int = tecnico_id
        resp = {"ok": True, "asignado_a": tecnico_id_int, "asignado_a_nombre": nombre, "email_sent": bool(email_sent)}
        try:
            if getattr(settings, "DEBUG", False) or _rol(request) in ("jefe", "admin"):
                resp["email_debug"] = email_debug
        except Exception:
            pass
        return Response(resp)


class IngresoSolicitarAsignacionView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, ingreso_id: int):
        # Solo tÃ©cnicos pueden solicitar
        require_roles(request, ["tecnico"])
        uid = getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)
        if not uid:
            return Response({"detail": "Usuario invÃ¡lido"}, status=400)

        # Si ya estÃ¡ asignado a este tÃ©cnico, responder OK y no hacer nada
        try:
            cur = q("SELECT asignado_a FROM ingresos WHERE id=%s", [ingreso_id], one=True)
            if cur and int(cur.get("asignado_a") or 0) == int(uid):
                return Response({"ok": True, "already_assigned": True, "email_sent": False})
        except Exception:
            pass

        # Intentar registrar solicitud en tabla auxiliar (si existe)
        try:
            with transaction.atomic():
                exec_void(
                """
                INSERT INTO ingreso_assignment_requests(ingreso_id, usuario_id, status, created_at)
                VALUES (%s, %s, 'solicitado', NOW())
                """,
                [ingreso_id, uid],
                )
        except Exception:
            pass  # tabla puede no existir; continuar con notificaciÃ³n

        # Enviar notificaciÃ³n por email (reportar Ã©xito/fracaso)
        email_sent = False
        email_debug = {}
        try:
            # Datos del ingreso para el cuerpo
            info = q(
                """
                SELECT c.razon_social AS cliente,
                       COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                       COALESCE(b.nombre,'') AS marca,
                       COALESCE(m.nombre,'') AS modelo,
                       COALESCE(d.numero_serie,'') AS numero_serie,
                       COALESCE(d.numero_interno,'') AS numero_interno
                  FROM ingresos t
                  JOIN devices d   ON d.id = t.device_id
                  JOIN customers c ON c.id = d.customer_id
                  LEFT JOIN marcas b ON b.id = d.marca_id
                  LEFT JOIN models m ON m.id = d.model_id
                 WHERE t.id=%s
                """,
                [ingreso_id],
                one=True,
            ) or {}
            os_txt = os_label(ingreso_id)
            tech_name = getattr(request.user, "nombre", "")
            cliente = info.get("cliente") or ""
            equipo = " | ".join([p for p in [info.get("tipo_equipo") or "", info.get("marca") or "", info.get("modelo") or ""] if p])
            ns = info.get("numero_serie") or ""

            subject = f"Solicitud de asignacion {os_txt} - {cliente}"
            lines = [
                f"El tecnico {tech_name} solicita asignacion.",
                f"OS: {os_txt}",
                f"Cliente: {cliente}",
                f"Equipo: {equipo or '-'}",
                f"N/S: {ns or '-'}",
            ]
            qs_parts = ["tab=principal"]
            try:
                if uid:
                    qs_parts.append(f"tecnico_id={int(uid)}")
            except Exception:
                pass
            qs = ("?" + "&".join(qs_parts)) if qs_parts else ""
            try:
                url = _frontend_url(request, f"/ingresos/{ingreso_id}") + qs
                lines.append("")
                lines.append(f"Abrir hoja: {url}")
            except Exception:
                pass
            body = "\n".join(lines)
            try:
                body = _email_append_footer_text(body)
            except Exception:
                pass
            # Destinatarios: primero los configurados, luego fallback a correos de la empresa
            recips = getattr(settings, "ASSIGNMENT_REQUEST_RECIPIENTS", []) or []
            if not isinstance(recips, (list, tuple)):
                recips = [str(recips)] if recips else []
            if not recips:
                fallback1 = getattr(settings, "COMPANY_FOOTER_EMAIL_2", None)
                fallback2 = getattr(settings, "COMPANY_FOOTER_EMAIL", None)
                recips = [x for x in [fallback1, fallback2] if x]
            try:
                notify_solicitud_asignacion(ingreso_id, uid, tecnico_nombre=tech_name, emails=recips)
            except Exception:
                pass

            # Debug info (sin secretos)
            try:
                email_debug.update({
                    "backend": getattr(settings, "EMAIL_BACKEND", None),
                    "host": getattr(settings, "EMAIL_HOST", None),
                    "port": getattr(settings, "EMAIL_PORT", None),
                    "use_tls": getattr(settings, "EMAIL_USE_TLS", None),
                    "use_ssl": getattr(settings, "EMAIL_USE_SSL", None),
                    "from": getattr(settings, "DEFAULT_FROM_EMAIL", None),
                    "recipients": list(recips),
                })
            except Exception:
                pass

            if recips:
                try:
                    sent, dbg = _send_mail_with_fallback(subject, body, recips)
                    email_sent = bool(sent)
                    try:
                        email_debug.update(dbg or {})
                    except Exception:
                        pass
                    logger.info(
                        "assignment_request_email sent=%s ingreso_id=%s user_id=%s recipients=%s backend=%s",
                        email_sent, ingreso_id, uid, recips, getattr(settings, "EMAIL_BACKEND", ""),
                    )
                except Exception as e:
                    try:
                        email_debug["error"] = str(e)
                        email_debug["exception"] = e.__class__.__name__
                    except Exception:
                        pass
                    logger.exception(
                        "assignment_request_email failed",
                        extra={"ingreso_id": ingreso_id, "user_id": uid, "recipients": recips},
                    )
                    email_sent = False
            else:
                logger.warning(
                    "assignment_request_email no recipients configured",
                    extra={"ingreso_id": ingreso_id, "user_id": uid},
                )
        except Exception as e:
            try:
                email_debug["error"] = str(e)
                email_debug["exception"] = e.__class__.__name__
            except Exception:
                pass
            email_sent = False

        # Solo exponer debug a roles altos o en DEBUG
        resp = {"ok": True, "email_sent": bool(email_sent)}
        try:
            if getattr(settings, "DEBUG", False) or _rol(request) in ("jefe", "admin"):
                resp["email_debug"] = email_debug
        except Exception:
            pass
        return Response(resp)


class IngresoSolicitarBajaView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, ingreso_id: int):
        _set_audit_user(request)
        if user_has_permission(request, "action.ingreso.baja_alta"):
            raise PermissionDenied("Tu usuario ya tiene permiso para dar baja directa")

        require_any_permission(
            request,
            [
                "page.ingresos_history",
                "page.work_queues",
                "page.budget_queues",
                "page.logistics",
                "action.ingreso.edit_basics",
                "action.ingreso.edit_diagnosis",
                "action.ingreso.edit_location",
                "action.ingreso.edit_delivery",
                "action.ingreso.manage_derivations",
                "action.ingreso.repair_transitions",
                "action.presupuesto.manage",
            ],
            message="No autorizado para solicitar baja",
        )

        uid = getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)
        if not uid:
            return Response({"detail": "Usuario invalido"}, status=400)

        payload = request.data or {}
        motivo = str(payload.get("motivo") or "").strip()
        if not motivo:
            return Response({"detail": "El motivo es obligatorio"}, status=400)

        row = q("SELECT estado FROM ingresos WHERE id=%s", [ingreso_id], one=True)
        if not row:
            return Response({"detail": "Ingreso no encontrado"}, status=404)
        estado_actual = (row.get("estado") or "").lower()
        if estado_actual == "baja":
            return Response({"ok": True, "already_baja": True, "email_sent": False})

        pending = _pending_baja_request(ingreso_id)
        if pending:
            return Response(
                {
                    "ok": True,
                    "already_pending": True,
                    "email_sent": False,
                    "solicitud_id": pending.get("id"),
                    "baja_solicitada_fecha": pending.get("created_at"),
                }
            )

        req_row = None
        try:
            req_row = q(
                """
                INSERT INTO ingreso_baja_requests (ingreso_id, usuario_id, motivo, created_at)
                VALUES (%s, %s, %s, NOW())
                RETURNING id, created_at
                """,
                [ingreso_id, uid, motivo],
                one=True,
            )
        except Exception:
            pending = _pending_baja_request(ingreso_id)
            if pending:
                return Response(
                    {
                        "ok": True,
                        "already_pending": True,
                        "email_sent": False,
                        "solicitud_id": pending.get("id"),
                        "baja_solicitada_fecha": pending.get("created_at"),
                    }
                )
            logger.exception("baja_request_insert failed", extra={"ingreso_id": ingreso_id, "user_id": uid})
            return Response({"detail": "No se pudo registrar la solicitud de baja"}, status=500)

        info = q(
            """
            SELECT c.razon_social AS cliente,
                   COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                   COALESCE(b.nombre,'') AS marca,
                   COALESCE(m.nombre,'') AS modelo,
                   COALESCE(d.numero_serie,'') AS numero_serie,
                   COALESCE(d.numero_interno,'') AS numero_interno
              FROM ingresos t
              JOIN devices d ON d.id = t.device_id
              JOIN customers c ON c.id = d.customer_id
              LEFT JOIN marcas b ON b.id = d.marca_id
              LEFT JOIN models m ON m.id = d.model_id
             WHERE t.id=%s
            """,
            [ingreso_id],
            one=True,
        ) or {}

        os_txt = os_label(ingreso_id)
        cliente = info.get("cliente") or ""
        equipo = " | ".join(
            [
                p
                for p in [info.get("tipo_equipo") or "", info.get("marca") or "", info.get("modelo") or ""]
                if p
            ]
        )
        numero_serie = info.get("numero_serie") or ""
        numero_interno = info.get("numero_interno") or ""
        actor_nombre = getattr(request.user, "nombre", None) or getattr(request.user, "username", "") or ""
        actor_email = getattr(request.user, "email", "") or ""
        actor_line = actor_nombre or "-"
        if actor_email:
            actor_line = f"{actor_line} ({actor_email})" if actor_line else actor_email

        subject = f"Solicitud de BAJA {os_txt} - {cliente or 'Sin cliente'}"
        lines = [
            "Se solicito dar de baja un equipo en el sistema de reparaciones.",
            "",
            "Detalle del ingreso:",
            f"- OS: {os_txt}",
            f"- Cliente: {cliente or '-'}",
            f"- Equipo: {equipo or '-'}",
            f"- Numero de serie: {numero_serie or '-'}",
            f"- Numero interno: {numero_interno or '-'}",
            f"- Solicitado por: {actor_line or '-'}",
            "",
            "Motivo de la solicitud:",
            motivo,
        ]
        try:
            url = _frontend_url(request, f"/ingresos/{ingreso_id}") + "?tab=principal"
            lines.append("")
            lines.append(f"Hoja de servicio: {url}")
        except Exception:
            pass
        body = "\n".join(lines)
        try:
            body = _email_append_footer_text(body)
        except Exception:
            pass

        email_sent = False
        email_debug = {}
        recips = []
        try:
            rec_rows = q(
                """
                SELECT DISTINCT LOWER(TRIM(CAST(email AS TEXT))) AS email
                  FROM users
                 WHERE COALESCE(activo, TRUE) = TRUE
                   AND LOWER(COALESCE(rol, '')) = 'jefe'
                   AND NULLIF(TRIM(CAST(email AS TEXT)), '') IS NOT NULL
                 ORDER BY 1
                """
            ) or []
            recips = [str(r.get("email") or "").strip() for r in rec_rows if str(r.get("email") or "").strip()]
            try:
                notify_solicitud_baja(ingreso_id, solicitante_id=uid, motivo=motivo, emails=recips)
            except Exception:
                pass
            try:
                email_debug.update(
                    {
                        "backend": getattr(settings, "EMAIL_BACKEND", None),
                        "host": getattr(settings, "EMAIL_HOST", None),
                        "port": getattr(settings, "EMAIL_PORT", None),
                        "use_tls": getattr(settings, "EMAIL_USE_TLS", None),
                        "use_ssl": getattr(settings, "EMAIL_USE_SSL", None),
                        "from": getattr(settings, "DEFAULT_FROM_EMAIL", None),
                        "recipients": list(recips),
                    }
                )
            except Exception:
                pass
            if recips:
                try:
                    sent, dbg = _send_mail_with_fallback(subject, body, recips)
                    email_sent = bool(sent)
                    try:
                        email_debug.update(dbg or {})
                    except Exception:
                        pass
                    logger.info(
                        "baja_request_email sent=%s ingreso_id=%s user_id=%s recipients=%s backend=%s",
                        email_sent,
                        ingreso_id,
                        uid,
                        recips,
                        getattr(settings, "EMAIL_BACKEND", ""),
                    )
                except Exception as e:
                    try:
                        email_debug["error"] = str(e)
                        email_debug["exception"] = e.__class__.__name__
                    except Exception:
                        pass
                    logger.exception(
                        "baja_request_email failed",
                        extra={"ingreso_id": ingreso_id, "user_id": uid, "recipients": recips},
                    )
                    email_sent = False
            else:
                logger.warning(
                    "baja_request_email no recipients configured",
                    extra={"ingreso_id": ingreso_id, "user_id": uid},
                )
        except Exception as e:
            try:
                email_debug["error"] = str(e)
                email_debug["exception"] = e.__class__.__name__
            except Exception:
                pass
            email_sent = False

        resp = {
            "ok": True,
            "email_sent": bool(email_sent),
            "solicitud_id": req_row.get("id") if isinstance(req_row, dict) else None,
            "baja_solicitada_fecha": req_row.get("created_at") if isinstance(req_row, dict) else None,
        }
        try:
            if getattr(settings, "DEBUG", False) or _rol(request) in ("jefe", "admin"):
                resp["email_debug"] = email_debug
        except Exception:
            pass
        return Response(resp)


class IngresoSolicitarBajaRechazarView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, ingreso_id: int):
        require_roles(request, ["jefe", "jefe_veedor", "admin", "recepcion"])
        _set_audit_user(request)

        row = q("SELECT id FROM ingresos WHERE id=%s", [ingreso_id], one=True)
        if not row:
            return Response({"detail": "Ingreso no encontrado"}, status=404)

        try:
            canceled = q(
                """
                UPDATE ingreso_baja_requests
                   SET canceled_at = COALESCE(canceled_at, NOW())
                 WHERE ingreso_id = %s
                   AND accepted_at IS NULL
                   AND canceled_at IS NULL
                RETURNING id
                """,
                [ingreso_id],
                one=True,
            )
        except Exception:
            logger.exception(
                "baja_request_reject failed",
                extra={"ingreso_id": ingreso_id, "user_id": getattr(request, "user_id", None)},
            )
            return Response({"detail": "No se pudo rechazar la solicitud de baja"}, status=500)

        if canceled:
            return Response({"ok": True, "rejected": True})
        return Response({"ok": True, "already_processed": True})


class PendientesGeneralView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def get(self, request):
        require_roles(request, ["jefe", "admin", "jefe_veedor", "tecnico"])
        tecnico_raw = (request.GET.get("tecnico_id") or "").strip()
        tecnico_filter = tecnico_raw.lower()

        with connection.cursor() as cur:
            _set_audit_user(request)

            sql = """
                SELECT t.id,
                       t.estado,
                       t.presupuesto_estado,
                       t.motivo,
                       c.razon_social,
                       d.numero_serie,
                       COALESCE(d.numero_interno,'') AS numero_interno,
                       COALESCE(b.nombre,'') AS marca,
                       COALESCE(m.nombre,'') AS modelo,
                       COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                       COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,'')) AS equipo_variante,
                       t.asignado_a,
                       COALESCE(u.nombre,'') AS tecnico,
                       COALESCE(u.nombre,'') AS tecnico_nombre,
                       COALESCE(u.nombre,'') AS asignado_a_nombre,
                       t.fecha_ingreso,
                       CASE WHEN ed.estado = 'devuelto' THEN true ELSE false END AS derivado_devuelto
                FROM ingresos t
                JOIN devices d   ON d.id = t.device_id
                JOIN customers c ON c.id = d.customer_id
                LEFT JOIN marcas b ON b.id = d.marca_id
                LEFT JOIN models m ON m.id = d.model_id
                LEFT JOIN users u ON u.id = t.asignado_a
                LEFT JOIN locations loc ON loc.id = t.ubicacion_id
                LEFT JOIN (
                  SELECT e.*, ROW_NUMBER() OVER (
                    PARTITION BY e.ingreso_id ORDER BY e.fecha_deriv DESC, e.id DESC
                  ) AS rn
                  FROM equipos_derivados e
                ) ed ON ed.ingreso_id = t.id AND ed.rn = 1
                WHERE LOWER(loc.nombre) = LOWER(%s)
                  AND t.estado NOT IN ('liberado','entregado','alquilado','baja')
            """
            params = ["taller"]
            if tecnico_raw.isdigit():
                sql += " AND t.asignado_a = %s"
                params.append(int(tecnico_raw))
            elif tecnico_filter in {"sin_asignar", "unassigned", "none", "null"}:
                sql += " AND t.asignado_a IS NULL"

            sql += """
                ORDER BY
                  (CASE WHEN ed.estado = 'devuelto' THEN 1 ELSE 0 END) DESC,
                  (t.motivo = 'urgente control') DESC,
                  t.fecha_ingreso ASC
            """
            cur.execute(sql, params)
            rows = _fetchall_dicts(cur)

        return Response(IngresoListItemSerializer(rows, many=True).data)


def _resolve_location_history_values(rows):
    if not rows:
        return rows

    def _location_id(value):
        if value is None:
            return None
        raw = str(value).strip()
        if not raw or raw.lower() in {"null", "none"}:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    ids = set()
    for row in rows:
        if (row.get("column_name") or "").strip().lower() != "ubicacion_id":
            continue
        for key in ("old_value", "new_value"):
            loc_id = _location_id(row.get(key))
            if loc_id is not None:
                ids.add(loc_id)

    if not ids:
        return rows

    try:
        loc_rows = q(
            """
            SELECT id, nombre
              FROM locations
             WHERE id = ANY(%s)
            """,
            [list(ids)],
        ) or []
        names = {int(r["id"]): r.get("nombre") for r in loc_rows if r.get("id") is not None}
    except Exception:
        return rows

    for row in rows:
        if (row.get("column_name") or "").strip().lower() != "ubicacion_id":
            continue
        for key in ("old_value", "new_value"):
            loc_id = _location_id(row.get(key))
            if loc_id is not None and names.get(loc_id):
                row[key] = names[loc_id]
    return rows


class IngresoHistorialView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def get(self, request, ingreso_id: int):
        require_roles(request, ["jefe", "jefe_veedor", "admin"])
        # Flag opcional para incluir la auditorÃ­Â­a HTTP (audit_log) en la respuesta
        def _param_truthy(name: str) -> bool:
            v = (request.query_params.get(name) or "").strip().lower()
            return v in {"1", "true", "yes", "on"}
        include_audit = _param_truthy("include_audit")
        if connection.vendor == "postgresql":
            # change_log (si existe) + audit_log (opcional)
            rows = []
            try:
                rows = q(
                    """
                      SELECT cl.ts, cl.user_id, cl.user_role, COALESCE(u.nombre,'') AS user_nombre, cl.table_name, cl.record_id, cl.column_name, cl.old_value, cl.new_value FROM audit.change_log cl LEFT JOIN users u ON u.id = cl.user_id
                      WHERE ingreso_id = %s
                      ORDER BY cl.ts DESC, cl.id DESC
                    """,
                    [ingreso_id]
                ) or []
            except Exception:
                rows = []
            if include_audit:
                # Complementar con auditorÃ­Â­a HTTP (audit_log), pero sin expandir por clave
                pat1 = f"/api/ingresos/{ingreso_id}/%"
                pat2 = f"/api/ingresos/{ingreso_id}/"
                pat3 = f"/api/quotes/{ingreso_id}/%"
                pat4 = f"/api/quotes/{ingreso_id}/"
                al_rows = q(
                    """
                    SELECT al.id AS _id, al.ts, al.user_id, al.role AS user_role, COALESCE(u.nombre,'') AS user_nombre, al.method, al.path, al.body
                      FROM audit_log al LEFT JOIN users u ON u.id = al.user_id
                     WHERE path LIKE %s OR path = %s OR path LIKE %s OR path = %s
                     ORDER BY al.ts DESC, al.id DESC
                    """,
                    [pat1, pat2, pat3, pat4]
                ) or []
                out = []
                for r in (al_rows or []):
                    path = (r.get("path") or "").lower()
                    method = (r.get("method") or "").upper()
                    table_name = "ingresos"
                    record_id = ingreso_id
                    if "/accesorios/" in path:
                        table_name = "ingreso_accesorios"
                    elif "/fotos/" in path:
                        table_name = "ingreso_media"
                    elif "/quotes/" in path or "/presupuestos/" in path:
                        table_name = "quotes"
                    body = r.get("body")
                    try:
                        if isinstance(body, (dict, list)):
                            body_str = json.dumps(body, ensure_ascii=False)
                        elif isinstance(body, str):
                            body_str = body
                        else:
                            body_str = ""
                    except Exception:
                        body_str = ""
                    out.append({
                        "ts": r.get("ts"),
                        "user_id": r.get("user_id"),
                        "user_role": r.get("user_role"),
                        "user_nombre": r.get("user_nombre"),
                        "table_name": table_name,
                        "record_id": record_id,
                        "column_name": f"{method} {r.get('path')}",
                        "old_value": None,
                        "new_value": (body_str or None) and body_str[:512],
                    })
                rows = (rows or []) + out
            rows = _resolve_location_history_values(rows)
            rows.sort(key=lambda x: (x.get("ts") or ""), reverse=True)
        else:
            # 1) Cambios de estado (ingreso_events)
            ev_rows = q(
                """
                SELECT 
                  e.id AS _id,
                  e.ts AS ts,
                  e.usuario_id AS user_id,
                  u.rol AS user_role, COALESCE(u.nombre,'') AS user_nombre,
                  'ingresos' AS table_name,
                  e.ingreso_id AS record_id,
                  'estado' AS column_name,
                  e.de_estado AS old_value,
                  e.a_estado AS new_value
                FROM ingreso_events e
                LEFT JOIN users u ON u.id = e.usuario_id
                WHERE e.ingreso_id = %s
                ORDER BY e.ts DESC, e.id DESC
                """,
                [ingreso_id]
            ) or []

            # 2) Cambios por HTTP (audit_log): opcional (?include_audit=1)
            pat1 = f"/api/ingresos/{ingreso_id}/%"
            pat2 = f"/api/ingresos/{ingreso_id}/"
            pat3 = f"/api/quotes/{ingreso_id}/%"
            pat4 = f"/api/quotes/{ingreso_id}/"
            out = list(ev_rows)
            if include_audit:
                al_rows = q(
                    """
                    SELECT al.id AS _id, al.ts, al.user_id, al.role AS user_role, COALESCE(u.nombre,'') AS user_nombre, al.method, al.path, al.body FROM audit_log al LEFT JOIN users u ON u.id = al.user_id
                     WHERE path LIKE %s OR path = %s OR path LIKE %s OR path = %s
                     ORDER BY al.ts DESC, al.id DESC
                    """,
                    [pat1, pat2, pat3, pat4]
                ) or []
                for r in (al_rows or []):
                    path = (r.get("path") or "").lower()
                    method = (r.get("method") or "").upper()
                    table_name = "ingresos"
                    record_id = ingreso_id
                    # HeurÃ­Â­stica por ruta
                    if "/accesorios/" in path:
                        table_name = "ingreso_accesorios"
                    elif "/fotos/" in path:
                        table_name = "ingreso_media"
                    elif "/quotes/" in path or "/presupuestos/" in path:
                        table_name = "quotes"
                    body = r.get("body")
                    try:
                        if isinstance(body, (dict, list)):
                            body_str = json.dumps(body, ensure_ascii=False)
                        elif isinstance(body, str):
                            body_str = body
                        else:
                            body_str = ""
                    except Exception:
                        body_str = ""
                    out.append({
                        "ts": r.get("ts"),
                        "user_id": r.get("user_id"),
                        "user_role": r.get("user_role"),
                        "user_nombre": r.get("user_nombre"),
                        "table_name": table_name,
                        "record_id": record_id,
                        "column_name": f"{method} {r.get('path')}",
                        "old_value": None,
                        "new_value": (body_str or None) and body_str[:512],
                    })

            # Orden final por fecha desc y sin modificar formato esperado
            out.sort(key=lambda x: (x.get("ts") or "",), reverse=True)
            rows = out
        return Response(rows)


class CerrarReparacionView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def post(self, request, ingreso_id: int):
        require_roles(request, ["jefe","jefe_veedor","admin"])
        has_permite_reparacion = _ingresos_has_permite_reparacion_col()
        permite_reparacion_sql = (
            "COALESCE(permite_reparacion, TRUE) AS permite_reparacion"
            if has_permite_reparacion
            else "TRUE AS permite_reparacion"
        )
        row_repair = q(
            f"""
            SELECT
              asignado_a,
              motivo,
              {permite_reparacion_sql}
            FROM ingresos
            WHERE id=%s
            """,
            [ingreso_id],
            one=True,
        )
        if not row_repair:
            raise ValidationError("Ingreso no encontrado")
        if _is_cotizacion_bloqueada_para_reparar(row_repair.get("motivo"), row_repair.get("permite_reparacion")):
            raise ValidationError("Ingreso en cotización: primero habilite la reparación.")
        if _rol(request) == "jefe_veedor":
            uid = getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)
            if int(row_repair.get("asignado_a") or 0) != int(uid or 0):
                raise PermissionDenied("Solo el tecnico asignado puede editar diagnostico y reparacion")
        r = (request.data or {}).get("resolucion")
        if r not in ("reparado","no_reparado","no_se_encontro_falla","presupuesto_rechazado","cambio"):
            return Response({"detail": "resolucion invÃ¡lida"}, status=400)

        serial_cambio = (request.data or {}).get("serial_cambio")
        if r == "cambio":
            if not (serial_cambio or "").strip():
                return Response({"detail": "serial_cambio requerido para resolucion 'cambio'"}, status=400)
            serial_cambio = (serial_cambio or "").strip()

        with connection.cursor() as cur:
            _set_audit_user(request)
            # Detectar si existe columna serial_cambio
            cur.execute(
                """
                SELECT 1 FROM information_schema.columns
                 WHERE table_name = 'ingresos' AND column_name = 'serial_cambio'
                   AND table_schema = ANY(current_schemas(true))
                 LIMIT 1
                """
            )
            has_serial_col = cur.fetchone() is not None
            if r == "cambio" and has_serial_col:
                cur.execute(
                    """
                    UPDATE ingresos
                       SET resolucion = %s,
                           serial_cambio = %s
                     WHERE id = %s
                    """,
                    [r, serial_cambio, ingreso_id],
                )
            else:
                cur.execute(
                    """
                    UPDATE ingresos
                       SET resolucion = %s
                     WHERE id = %s
                    """,
                    [r, ingreso_id],
                )
        return Response({"ok": True})


class NuevoIngresoView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @transaction.atomic
    def post(self, request):
        require_any_permission(request, ["action.ingreso.create", "page.new_ingreso"])
        # Best-effort: ensure PK sequences are aligned to prevent duplicate key
        # errors due to out-of-sync sequences after legacy imports.
        try:
            from .helpers_impl import repair_pk_sequence
            repair_pk_sequence('devices')
            repair_pk_sequence('ingresos')
        except Exception:
            pass
        data = request.data or {}
        cliente = data.get("cliente") or {}
        equipo = data.get("equipo") or {}

        # Empresa a facturar (branding de PDFs). Default SEPID; valida solo dos valores.
        empresa_facturar = (data.get("empresa_facturar") or "SEPID").strip().upper()
        if empresa_facturar not in ("SEPID", "MGBIO"):
            empresa_facturar = "SEPID"

        motivo_raw = (data.get("motivo") or "").strip()
        if not motivo_raw:
            return Response({"detail": "motivo requerido"}, status=400)

        motivo_label_raw = _map_motivo_to_db_label(motivo_raw)
        if not motivo_label_raw:
            valid_motivos = _get_motivo_enum_values()
            return Response({"detail": "motivo invÃ¡lido", "valid_values": valid_motivos}, status=400)

        raw_vals = _get_motivo_enum_values_raw() or []
        if raw_vals:
            target = None
            norm_target = _norm_txt(motivo_label_raw)
            for rv in raw_vals:
                if _norm_txt(rv) == norm_target:
                    target = rv
                    break
            if not target:
                target = next((x for x in raw_vals if _norm_txt(x) == _norm_txt('otros')), raw_vals[0])
            motivo = target
        else:
            motivo = motivo_label_raw
        permite_reparacion_default = not _motivo_is_cotizacion_equipo(motivo)

        numero_interno = (equipo.get("numero_interno") or "").strip()
        if numero_interno and not numero_interno.upper().startswith(("MG", "NM", "NV", "CE")):
            numero_interno = "MG " + numero_interno
        has_mg_schema = _has_mg_schema()
        mg_owner_id = _mg_owner_customer_id()

        ubicacion_id = data.get("ubicacion_id")
        if not ubicacion_id:
            t = q("SELECT id FROM locations WHERE LOWER(nombre)=LOWER(%s) LIMIT 1", ["taller"], one=True)
            if not t:
                return Response({"detail": "No se encontro la ubicaciÃ³n 'Taller' en el catalogo. Creala en 'locations'."}, status=400)
            ubicacion_id = t["id"]

        informe_preliminar = (data.get("informe_preliminar") or "").strip()
        accesorios_text = (data.get("accesorios") or "").strip()
        comentarios_text = (data.get("comentarios") or "").strip() or None
        accesorios_items = data.get("accesorios_items") or []

        remito_ingreso = (data.get("remito_ingreso") or "").strip()
        fecha_ingreso_dt = None
        _fi_raw = data.get("fecha_ingreso")
        if _fi_raw is not None and str(_fi_raw).strip() != "":
            _dt = _parse_datetime_or_date(_fi_raw)
            if not _dt:
                return Response({"detail": "fecha_ingreso invÃ¡lida (use YYYY-MM-DD o DD/MM/AAAA)"}, status=400)
            if timezone.is_naive(_dt):
                _dt = timezone.make_aware(_dt, timezone.get_current_timezone())
            fecha_ingreso_dt = _dt

        if not equipo.get("marca_id") or not equipo.get("modelo_id"):
            return Response({"detail": "equipo.marca_id y equipo.modelo_id son requeridos"}, status=400)

        c = None
        if cliente.get("id"):
            c = q("SELECT id, cod_empresa, razon_social FROM customers WHERE id=%s", [cliente["id"]], one=True)
        elif cliente.get("cod_empresa"):
            c = q("SELECT id, cod_empresa, razon_social FROM customers WHERE cod_empresa=%s", [cliente["cod_empresa"]], one=True)
        elif cliente.get("razon_social"):
            c = q("SELECT id, cod_empresa, razon_social FROM customers WHERE LOWER(razon_social)=LOWER(%s)", [cliente["razon_social"]], one=True)
        else:
            return Response({"detail": "Debe seleccionar un cliente"}, status=400)

        if not c:
            return Response({"detail": "Cliente inexistente"}, status=400)

        if cliente.get("cod_empresa") and c["cod_empresa"] != cliente["cod_empresa"]:
            return Response({"detail": "El cÃ³digo no corresponde a la razÃ³n social seleccionada."}, status=400)
        if cliente.get("razon_social") and c["razon_social"].lower() != (cliente["razon_social"] or "").lower():
            return Response({"detail": "La razÃ³n social no corresponde al cÃ³digo seleccionado."}, status=400)

        customer_id = c["id"]

        marca = q("SELECT id FROM marcas WHERE id=%s", [equipo["marca_id"]], one=True)
        model = q("SELECT id FROM models WHERE id=%s AND marca_id=%s", [equipo["modelo_id"], equipo["marca_id"]], one=True)
        if not marca or not model:
            return Response({"detail": "Marca o modelo inexistente"}, status=400)

        prop = data.get("propietario") or {}
        prop_nombre = (prop.get("nombre") or "").strip()
        prop_contacto = (prop.get("contacto") or "").strip()
        prop_doc = (prop.get("doc") or "").strip()

        # Validacion: si el cliente es 'Particular', propietario.nombre y propietario.doc son obligatorios
        try:
            rs_lower = (c.get("razon_social") or "").strip().lower()
        except Exception:
            rs_lower = ""
        if rs_lower == "particular":
            if not prop_nombre or not prop_doc:
                return Response({"detail": "Para cliente 'Particular' es obligatorio completar Nombre y CUIT del propietario"}, status=400)

        numero_serie = (equipo.get("numero_serie") or "").strip()
        ns_key = (numero_serie or "").replace(" ", "").replace("-", "").upper()
        # CÃ¡lculo preliminar de garantÃ­a de fÃ¡brica (Parte 1)
        try:
            wcalc = compute_warranty(
                numero_serie,
                brand_id=equipo.get("marca_id"),
                model_id=equipo.get("modelo_id"),
            )
        except Exception:
            wcalc = {"garantia": None, "vence_el": None, "fecha_venta": None}

        # Chequeo adicional de duplicado por nÂ§mero interno (MG/NM/NV/CE ####):
        # si existe un ingreso abierto para el mismo MG, se reutiliza ese ingreso.
        if numero_interno:
            has_mg_sale_extended = _has_mg_sale_extended_schema()
            if has_mg_schema and has_mg_sale_extended:
                mg_state_sql = "COALESCE(d.mg_estado,'activo') AS mg_estado,"
                mg_sale_sql = """
                  d.mg_venta_fecha AS mg_venta_fecha,
                  COALESCE(d.mg_venta_factura_numero,'') AS mg_venta_factura_numero,
                  COALESCE(d.mg_venta_remito_numero,'') AS mg_venta_remito_numero,
                  d.mg_venta_customer_id AS mg_venta_customer_id,
                  COALESCE(d.mg_venta_numero_alternativo,'') AS mg_venta_numero_alternativo
                """
            else:
                mg_state_sql = "'activo' AS mg_estado,"
                mg_sale_sql = """
                  NULL AS mg_venta_fecha,
                  '' AS mg_venta_factura_numero,
                  '' AS mg_venta_remito_numero,
                  NULL AS mg_venta_customer_id,
                  '' AS mg_venta_numero_alternativo
                """

            mg_device = q(
                f"""
                SELECT
                  d.id,
                  d.customer_id,
                  COALESCE(d.numero_interno,'') AS numero_interno,
                  COALESCE(d.numero_serie,'') AS numero_serie,
                  COALESCE(d.alquilado,false) AS alquilado,
                  {mg_state_sql}
                  {mg_sale_sql}
                FROM devices d
                WHERE d.numero_interno ~* '^(MG|NM|NV|CE)\\s*\\d{{1,4}}$'
                  AND UPPER(REGEXP_REPLACE(d.numero_interno,
                      '^(MG|NM|NV|CE)\\s*(\\d{{1,4}})$', '\\1 ' || LPAD('\\2',4,'0'))) =
                      UPPER(REGEXP_REPLACE(%s,
                      '^(MG|NM|NV|CE)\\s*(\\d{{1,4}})$', '\\1 ' || LPAD('\\2',4,'0')))
                LIMIT 1
                """,
                [numero_interno],
                one=True,
            )
            if mg_device:
                mg_flags = resolve_mg_flags(mg_device, mg_owner_id)
                if mg_flags.get("mg_inactivo_venta"):
                    return Response(
                        {
                            "detail": "MG histÃ³rico inactivo por venta; no operativo para nuevos ingresos.",
                            "conflict_type": "MG_INACTIVO_VENTA",
                            "payload": {
                                "device_id": mg_device.get("id"),
                                "numero_interno": mg_device.get("numero_interno") or numero_interno,
                                "mg_estado": "inactivo_venta",
                                "mg_venta_fecha": mg_device.get("mg_venta_fecha"),
                                "mg_venta_factura_numero": mg_device.get("mg_venta_factura_numero") or "",
                                "mg_venta_remito_numero": mg_device.get("mg_venta_remito_numero") or "",
                                "mg_venta_customer_id": mg_device.get("mg_venta_customer_id"),
                                "mg_venta_numero_alternativo": mg_device.get("mg_venta_numero_alternativo") or "",
                            },
                        },
                        status=400,
                    )

            dup_mg = q(
                """
                SELECT t.id,
                       COALESCE(t.fecha_ingreso, t.fecha_creacion) AS fecha_ingreso,
                       t.fecha_creacion
                  FROM ingresos t
                  JOIN devices d ON d.id = t.device_id
                 WHERE d.numero_interno ~* '^(MG|NM|NV|CE)\\s*\\d{1,4}$'
                   AND UPPER(REGEXP_REPLACE(d.numero_interno,
                       '^(MG|NM|NV|CE)\\s*(\\d{1,4})$', '\\1 ' || LPAD('\\2',4,'0'))) =
                       UPPER(REGEXP_REPLACE(%s,
                       '^(MG|NM|NV|CE)\\s*(\\d{1,4})$', '\\1 ' || LPAD('\\2',4,'0')))
                   AND t.estado NOT IN ('entregado','alquilado','baja')
                 ORDER BY t.id DESC
                 LIMIT 1
                """,
                [numero_interno],
                one=True,
            )
            if dup_mg:
                existing_id = dup_mg["id"]
                fecha_ingreso_val = dup_mg.get("fecha_ingreso") or dup_mg.get("fecha_creacion")
                try:
                    fecha_ingreso_iso = fecha_ingreso_val.isoformat()
                except Exception:
                    fecha_ingreso_iso = fecha_ingreso_val
                return Response({
                    "ok": True,
                    "ingreso_id": existing_id,
                    "os": os_label(existing_id),
                    "existing": True,
                    "fecha_ingreso": fecha_ingreso_iso,
                })

        if ns_key:
            dup = q(
                """
                SELECT t.id,
                       COALESCE(t.fecha_ingreso, t.fecha_creacion) AS fecha_ingreso,
                       t.fecha_creacion
                  FROM ingresos t
                  JOIN devices d ON d.id = t.device_id
                 WHERE REPLACE(REPLACE(UPPER(d.numero_serie),' ',''),'-','') = %s
                   AND t.estado NOT IN ('entregado','alquilado','baja')
                 ORDER BY t.id DESC
                 LIMIT 1
                """,
                [ns_key],
                one=True,
            )
            if dup:
                existing_id = dup["id"]
                fecha_ingreso_val = dup.get("fecha_ingreso") or dup.get("fecha_creacion")
                try:
                    fecha_ingreso_iso = fecha_ingreso_val.isoformat()
                except Exception:
                    fecha_ingreso_iso = fecha_ingreso_val
                return Response({
                    "ok": True,
                    "ingreso_id": existing_id,
                    "os": os_label(existing_id),
                    "existing": True,
                    "fecha_ingreso": fecha_ingreso_iso,
                })

        # Auto-chequeo de garantÃ­Â­a de fÃ¡brica por N/S si el usuario no la marcÃ³
        # no auto-calculo de garantia de fabrica por ahora (se toma del payload)

        # ResoluciÃ³n consistente de device por NS / MG con validaciones de conflicto
        dev_ns = None
        dev_mg = None
        if ns_key:
            dev_ns = q(
                """
                SELECT id, COALESCE(numero_serie,'') AS numero_serie, COALESCE(numero_interno,'') AS numero_interno
                  FROM devices
                 WHERE REPLACE(REPLACE(UPPER(numero_serie),' ',''),'-','') = %s
                 LIMIT 1
                """,
                [ns_key],
                one=True,
            )
        if numero_interno:
            # Intento exacto primero
            dev_mg = q(
                """
                SELECT id, COALESCE(numero_serie,'') AS numero_serie, COALESCE(numero_interno,'') AS numero_interno
                  FROM devices
                 WHERE numero_interno = %s
                 LIMIT 1
                """,
                [numero_interno],
                one=True,
            )
            if not dev_mg:
                # BÃºsqueda por nÃºmero interno normalizado (MG|NM|NV|CE ####)
                dev_mg = q(
                    """
                    SELECT id, COALESCE(numero_serie,'') AS numero_serie, COALESCE(numero_interno,'') AS numero_interno
                      FROM devices
                     WHERE numero_interno ~* '^(MG|NM|NV|CE)\\s*\\d{1,4}$'
                       AND UPPER(REGEXP_REPLACE(numero_interno,
                           '^(MG|NM|NV|CE)\\s*(\\d{1,4})$', '\\1 ' || LPAD('\\2',4,'0'))) =
                           UPPER(REGEXP_REPLACE(%s,
                           '^(MG|NM|NV|CE)\\s*(\\d{1,4})$', '\\1 ' || LPAD('\\2',4,'0')))
                     LIMIT 1
                    """,
                    [numero_interno],
                    one=True,
                )

        conflict_payload = {
            "numero_serie_input": numero_serie or None,
            "numero_interno_input": numero_interno or None,
            "device_ns": dev_ns,
            "device_mg": dev_mg,
        }

        # Clasificar conflictos NS/MG
        if dev_ns and dev_mg and dev_ns["id"] != dev_mg["id"]:
            msg = (
                "Conflicto entre nÃºmero de serie y nÃºmero interno. "
                "El nÃºmero de serie ingresado estÃ¡ asociado a un equipo distinto "
                "del que tiene asignado el nÃºmero interno ingresado. "
                "Revise que no haya error de tipeo en N/S o MG."
            )
            logger.warning("NS/MG conflict (ids distintos): %s", conflict_payload)
            return Response(
                {
                    "detail": msg,
                    "conflict_type": "NS_MG_MISMATCH",
                    "payload": conflict_payload,
                },
                status=400,
            )

        if dev_mg and numero_serie and (dev_mg.get("numero_serie") or "").strip():
            ns_dev_mg = (dev_mg.get("numero_serie") or "").strip()
            ns_dev_mg_key = ns_dev_mg.replace(" ", "").replace("-", "").upper()
            if ns_dev_mg_key != ns_key:
                msg = (
                    "El nÃºmero interno ingresado ya estÃ¡ asociado a otro equipo con "
                    "un nÃºmero de serie distinto. Revise que no haya error de tipeo."
                )
                logger.warning("MG already linked to different NS: %s", conflict_payload)
                return Response(
                    {
                        "detail": msg,
                        "conflict_type": "MG_LINKED_TO_OTHER_NS",
                        "payload": conflict_payload,
                    },
                    status=400,
                )

        # Determinar device_id en base a las resoluciones anteriores
        device_id = None
        if dev_ns:
            device_id = dev_ns["id"]
        elif dev_mg:
            device_id = dev_mg["id"]
        else:
            # No existe device; crear uno nuevo respetando constraints
            if connection.vendor == "postgresql":
                device_id = exec_returning(
                    """
                    INSERT INTO devices (customer_id, marca_id, model_id, numero_serie, numero_interno)
                    VALUES (%s, %s, %s, %s, NULLIF(%s,''))
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """,
                    [customer_id, equipo["marca_id"], equipo["modelo_id"], numero_serie, numero_interno],
                )
                if not device_id and ns_key:
                    row_dev = None
                    for _ in range(3):
                        row_dev = q(
                            """
                            SELECT id FROM devices
                             WHERE REPLACE(REPLACE(UPPER(numero_serie),' ',''),'-','') = %s
                             LIMIT 1
                            """,
                            [ns_key],
                            one=True,
                        )
                        if row_dev:
                            break
                        time.sleep(0.05)
                    device_id = row_dev and int(row_dev.get("id"))
            else:
                exec_void(
                    """
                    INSERT INTO devices (customer_id, marca_id, model_id, numero_serie, numero_interno)
                    VALUES (%s, %s, %s, %s, NULLIF(%s,''))
                    """,
                    [customer_id, equipo["marca_id"], equipo["modelo_id"], numero_serie, numero_interno],
                )
                device_id = last_insert_id()
        # Blindaje: siempre alinear marca/modelo del device con la selecciÃ³n actual.
        # Evita Ã³rdenes con cruces (marca de un lado y modelo de otra marca).
        if device_id:
            exec_void(
                "UPDATE devices SET marca_id=%s, model_id=%s WHERE id=%s",
                [equipo["marca_id"], equipo["modelo_id"], device_id],
            )
        # Actualizar snapshot de cliente del device al del ingreso actual
        if device_id and customer_id:
            try:
                exec_void("UPDATE devices SET customer_id=%s WHERE id=%s", [customer_id, device_id])
            except Exception:
                pass
        # Actualizar fecha de vencimiento de garantÃ­a en el device segÃºn Excel
        try:
            vence = wcalc.get("vence_el") if isinstance(wcalc, dict) else None
            if vence is None:
                exec_void("UPDATE devices SET garantia_vence = NULL WHERE id=%s", [device_id])
            else:
                exec_void("UPDATE devices SET garantia_vence = %s WHERE id=%s", [vence, device_id])
        except Exception:
            pass
        if numero_interno and device_id:
            try:
                exec_void(
                    "UPDATE devices SET numero_interno = NULLIF(%s,'') WHERE id=%s",
                    [numero_interno, device_id],
                )
            except Exception as e:
                # Seguridad adicional: capturar violaciones de Ã­ndice Ãºnico y devolver error entendible
                msg = (
                    "No se pudo asignar el nÃºmero interno al equipo porque ya estÃ¡ "
                    "en uso por otro dispositivo. Revise el MG ingresado."
                )
                logger.warning("Error al actualizar numero_interno (device_id=%s, MG=%s): %s", device_id, numero_interno, e)
                return Response(
                    {
                        "detail": msg,
                        "conflict_type": "MG_UNIQUE_CONSTRAINT",
                        "payload": conflict_payload,
                    },
                    status=400,
                )

        auto_gar_rep = False
        last_out_candidates = []
        if numero_serie:
            row_last_ns = q(
                """
                SELECT MAX(t.fecha_entrega) AS last_out
                  FROM ingresos t
                  JOIN devices d ON d.id = t.device_id
                 WHERE d.numero_serie = %s
                   AND t.fecha_entrega IS NOT NULL
                """,
                [numero_serie],
                one=True,
            )
            last_ns = row_last_ns and row_last_ns.get("last_out")
            if last_ns:
                last_out_candidates.append(last_ns)
        if numero_interno:
            row_mg = q(
                """
                SELECT MAX(t.fecha_entrega) AS last_out
                  FROM ingresos t
                  JOIN devices d ON d.id = t.device_id
                 WHERE d.numero_interno = %s
                   AND t.fecha_entrega IS NOT NULL
                """,
                [numero_interno],
                one=True,
            )
            last_mg = row_mg and row_mg.get("last_out")
            if last_mg:
                last_out_candidates.append(last_mg)
        if last_out_candidates:
            try:
                last_out = max(last_out_candidates)
                auto_gar_rep = (timezone.now() - last_out).days <= 90
            except Exception:
                auto_gar_rep = False
        garantia_rep_payload = bool(data.get("garantia_reparacion"))
        etiq_ok = bool(data.get("etiq_garantia_ok"))
        garantia_rep_final = garantia_rep_payload or auto_gar_rep

        tecnico_id = data.get("tecnico_id")
        if not tecnico_id:
            tdef = q("SELECT tecnico_id FROM models WHERE id=%s", [equipo["modelo_id"]], one=True)
            tecnico_id = tdef["tecnico_id"] if tdef else None
        if not tecnico_id:
            tmarca = q("SELECT tecnico_id FROM marcas WHERE id=%s", [equipo["marca_id"]], one=True)
            tecnico_id = (tmarca or {}).get("tecnico_id")
        if tecnico_id:
            tech = q("SELECT id FROM users WHERE id=%s AND activo=true AND rol IN ('tecnico','jefe')",
                     [tecnico_id], one=True)
            if not tech:
                return Response({"detail": "TÃ©cnico invÃ¡lido o inactivo"}, status=400)

        uid = getattr(request.user, "id", None) or getattr(request, "user_id", None)
        if not uid:
            return Response({"detail": "Usuario no autenticado"}, status=401)
        _set_audit_user(request)

        # PostgreSQL-only build: motivo es texto vÃ¡lido segÃºn enum del modelo

        equipo_variante = (request.data.get("equipo_variante") or "").strip() or None
        has_permite_reparacion = _ingresos_has_permite_reparacion_col()
        permite_col_sql = ", permite_reparacion" if has_permite_reparacion else ""
        permite_val_sql = ", %s" if has_permite_reparacion else ""
        insert_params = [
            device_id, motivo, ubicacion_id, uid, tecnico_id,
            informe_preliminar, accesorios_text, comentarios_text, equipo_variante,
            prop_nombre, prop_contacto, prop_doc,
            garantia_rep_final, wcalc.get("garantia"), etiq_ok,
        ]
        if has_permite_reparacion:
            insert_params.append(bool(permite_reparacion_default))

        # Asegurar que tenemos un device_id vÃ¡lido antes de crear el ingreso
        if not device_id:
            return Response({
                "detail": "No se pudo identificar el equipo. Ingrese nÃºmero de serie o nÃºmero interno vÃ¡lido."
            }, status=400)
        ingreso_id = exec_returning(
            f"""
            INSERT INTO ingresos (
              device_id, motivo, ubicacion_id, recibido_por, asignado_a,
              informe_preliminar, accesorios, comentarios, equipo_variante,
              propietario_nombre, propietario_contacto, propietario_doc,
              garantia_reparacion, garantia_fabrica, etiq_garantia_ok
              {permite_col_sql}
            )
            VALUES (%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,
                    NULLIF(%s,''), NULLIF(%s,''), NULLIF(%s,''),
                    %s, %s, %s
                    {permite_val_sql})
            RETURNING id
            """,
             insert_params
        )

        # Setear empresa_facturar si la columna existe en la base
        try:
            with connection.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM information_schema.columns
                     WHERE table_name = 'ingresos'
                       AND column_name = 'empresa_facturar'
                       AND table_schema = ANY(current_schemas(true))
                     LIMIT 1
                    """
                )
                exists = cur.fetchone() is not None
                if exists:
                    exec_void("UPDATE ingresos SET empresa_facturar=%s WHERE id=%s", [empresa_facturar, ingreso_id])
        except Exception:
            # Mejor no romper si no existe la columna
            pass

        sets, params = [], []
        if remito_ingreso:
            sets.append("remito_ingreso = NULLIF(%s,'')")
            params.append(remito_ingreso)
        if fecha_ingreso_dt is not None:
            sets.append("fecha_ingreso = %s")
            params.append(fecha_ingreso_dt)
        if fecha_ingreso_dt is None:
            sets.append("fecha_ingreso = COALESCE(fecha_ingreso, NOW())")

        if sets:
            params.append(ingreso_id)
            exec_void(
                f"UPDATE ingresos SET {', '.join(sets)} WHERE id=%s",
                params,
            )

        for it in (accesorios_items or []):
            try:
                acc_id = int(it.get("accesorio_id"))
            except (TypeError, ValueError):
                continue
            ref = (it.get("referencia") or "").strip() or None
            desc = (it.get("descripcion") or "").strip() or None
            exec_void(
              "INSERT INTO ingreso_accesorios(ingreso_id, accesorio_id, referencia, descripcion) VALUES (%s, %s, %s, %s)",
              [ingreso_id, acc_id, ref, desc]
            )

        return Response({"ok": True, "ingreso_id": ingreso_id, "os": os_label(ingreso_id)}, status=201)


class GarantiaReparacionCheckView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def get(self, request):
        ns = (request.GET.get("numero_serie") or "").strip()
        mg = (request.GET.get("numero_interno") or request.GET.get("mg") or "").strip()
        if mg and not mg.upper().startswith(("MG", "NM", "NV", "CE")):
            mg = "MG " + mg
        if not ns and not mg:
            return Response({"within_90_days": False, "last_ingreso": None})

        last_out_candidates = []
        if ns:
            row_ns = q(
                """
                SELECT MAX(t.fecha_entrega) AS last_out
                  FROM ingresos t
                  JOIN devices d ON d.id = t.device_id
                 WHERE d.numero_serie = %s
                   AND t.fecha_entrega IS NOT NULL
                """,
                [ns],
                one=True,
            )
            last_ns = row_ns and row_ns.get("last_out")
            if last_ns:
                last_out_candidates.append(last_ns)

        if mg:
            row_mg = q(
                """
                SELECT MAX(t.fecha_entrega) AS last_out
                  FROM ingresos t
                  JOIN devices d ON d.id = t.device_id
                 WHERE d.numero_interno = %s
                   AND t.fecha_entrega IS NOT NULL
                """,
                [mg],
                one=True,
            )
            last_mg = row_mg and row_mg.get("last_out")
            if last_mg:
                last_out_candidates.append(last_mg)

        if not last_out_candidates:
            return Response({"within_90_days": False, "last_ingreso": None})

        last_out = max(last_out_candidates)
        within = (timezone.now() - last_out).days <= 90
        return Response({"within_90_days": within, "last_ingreso": last_out})

#TODO: chequear porquÃ© en GarantiaFabricaCheckView hay cÃ³digo muerto
class GarantiaFabricaCheckView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def get(self, request):
        # CÃ¡lculo basado en Excel GENERAL (Parte 1)
        ns = (request.GET.get("numero_serie") or "").strip()
        mg = (request.GET.get("numero_interno") or request.GET.get("mg") or "").strip()
        if mg and not mg.upper().startswith(("MG", "NM", "NV", "CE")):
            mg = "MG " + mg

        if not ns and not mg:
            return Response({"within_365_days": False, "fecha_venta": None, "found": False})

        # Permitir que el front envÃ­e brand_id/model_id explÃ­citos
        brand_id_explicit = request.GET.get("brand_id")
        model_id_explicit = request.GET.get("model_id")
        try:
            brand_id_explicit = int(brand_id_explicit) if brand_id_explicit not in (None, "", "null") else None
        except Exception:
            brand_id_explicit = None
        try:
            model_id_explicit = int(model_id_explicit) if model_id_explicit not in (None, "", "null") else None
        except Exception:
            model_id_explicit = None

        device = None
        if ns:
            device = q(
                "SELECT id, numero_serie, marca_id, model_id FROM devices WHERE numero_serie=%s LIMIT 1",
                [ns],
                one=True,
            )
        if (not device) and mg:
            device = q(
                "SELECT id, numero_serie, marca_id, model_id FROM devices WHERE numero_interno=%s LIMIT 1",
                [mg],
                one=True,
            )

        numero_serie = (device or {}).get("numero_serie") or ns
        # Prioridad: IDs explÃ­citos enviados por el front; si no, los del device (si existe)
        brand_id = brand_id_explicit if brand_id_explicit is not None else (device or {}).get("marca_id")
        model_id = model_id_explicit if model_id_explicit is not None else (device or {}).get("model_id")
        try:
            calc = compute_warranty(numero_serie, brand_id=brand_id, model_id=model_id)
        except Exception:
            calc = {"garantia": None, "fecha_venta": None, "vence_el": None, "meta": {}}

        en_garantia = bool(calc.get("garantia")) if calc.get("garantia") is not None else False
        fecha_venta = calc.get("fecha_venta")
        vence = calc.get("vence_el")
        meta = calc.get("meta") or {}

        return Response({
            "within_365_days": en_garantia,
            "fecha_venta": (fecha_venta.isoformat() if fecha_venta else None),
            "garantia_vence": (vence.isoformat() if vence else None),
            "found": bool(fecha_venta),
            "meta": meta,
        })
        # Fuente de verdad: Ãºltimo ingreso del equipo
        ns = (request.GET.get("numero_serie") or "").strip()
        mg = (request.GET.get("numero_interno") or request.GET.get("mg") or "").strip()
        if mg and not mg.upper().startswith(("MG", "NM", "NV", "CE")):
            mg = "MG " + mg

        if not ns and not mg:
            return Response({"within_365_days": False, "fecha_venta": None, "found": False})

        wh = []
        params = []
        if ns:
            wh.append("d.numero_serie = %s")
            params.append(ns)
        if mg:
            wh.append("d.numero_interno = %s")
            params.append(mg)
        sql_where = " OR ".join(wh) if wh else "1=0"
        try:
            row = q(
                f"""
                SELECT t.id AS ingreso_id,
                       COALESCE(t.garantia_fabrica,false) AS garantia,
                       COALESCE(t.fecha_ingreso, t.fecha_creacion) AS fecha_ingreso,
                       d.id AS device_id
                  FROM ingresos t
                  JOIN devices d ON d.id = t.device_id
                 WHERE {sql_where}
                 ORDER BY COALESCE(t.fecha_ingreso, t.fecha_creacion) DESC, t.id DESC
                 LIMIT 1
                """,
                params,
                one=True,
            )
        except Exception:
            row = None

        if not row:
            return Response({"within_365_days": False, "fecha_venta": None, "found": False})

        garantia = bool(row.get("garantia"))
        meta = {
            "source": "ingreso",
            "ingreso_id": row.get("ingreso_id"),
            "device_id": row.get("device_id"),
            "fecha_ingreso": row.get("fecha_ingreso"),
        }
        return Response({
            "within_365_days": garantia,
            "fecha_venta": None,
            "found": True,
            "meta": meta,
        })


class IngresoDetalleView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, ingreso_id: int):
        try:
            strong = str(request.GET.get("strong", "")).strip().lower() in ("1","true","yes")
        except Exception:
            strong = False
        broad_detail_permissions = [
            "page.ingresos_history",
            "page.work_queues",
            "page.budget_queues",
            "page.logistics",
            "page.service_sheet_principal",
            "action.ingreso.edit_basics",
            "action.ingreso.edit_diagnosis",
            "action.ingreso.edit_location",
            "action.ingreso.edit_delivery",
            "action.ingreso.manage_derivations",
            "action.ingreso.repair_transitions",
            "action.ingreso.baja_alta",
            "action.presupuesto.manage",
        ]
        if user_has_permission(request, "page.liberados") and not user_has_any_permission(request, broad_detail_permissions):
            estado_row = q("SELECT estado FROM ingresos WHERE id=%s", [ingreso_id], one=True)
            if not estado_row:
                return Response({"detail": "Ingreso no encontrado"}, status=404)
            if (estado_row.get("estado") or "").strip().lower() != "liberado":
                raise PermissionDenied("Solo se pueden ver ingresos liberados")
        has_permite_reparacion = _ingresos_has_permite_reparacion_col()
        permite_reparacion_sql = (
            "COALESCE(t.permite_reparacion, TRUE) AS permite_reparacion,"
            if has_permite_reparacion
            else "TRUE AS permite_reparacion,"
        )
        has_mg_schema = _has_mg_schema()
        has_mg_sale_extended = _has_mg_sale_extended_schema()
        mg_sale_customer_join = (
            "LEFT JOIN customers c_mg_sale ON c_mg_sale.id = d.mg_venta_customer_id"
            if has_mg_sale_extended
            else "LEFT JOIN customers c_mg_sale ON 1=0"
        )
        if has_mg_schema and has_mg_sale_extended:
            mg_select_sql = """
              COALESCE(d.mg_estado,'activo') AS mg_estado,
              d.mg_inactivo_desde AS mg_inactivo_desde,
              d.mg_venta_fecha AS mg_venta_fecha,
              COALESCE(d.mg_venta_factura_numero,'') AS mg_venta_factura_numero,
              COALESCE(d.mg_venta_remito_numero,'') AS mg_venta_remito_numero,
              COALESCE(d.mg_venta_observaciones,'') AS mg_venta_observaciones,
              d.mg_venta_usuario_id AS mg_venta_usuario_id,
              d.mg_venta_customer_id AS mg_venta_customer_id,
              COALESCE(c_mg_sale.razon_social,'') AS mg_venta_customer_nombre,
              COALESCE(d.mg_venta_numero_alternativo,'') AS mg_venta_numero_alternativo,
            """
        else:
            mg_select_sql = """
              'activo' AS mg_estado,
              NULL AS mg_inactivo_desde,
              NULL AS mg_venta_fecha,
              '' AS mg_venta_factura_numero,
              '' AS mg_venta_remito_numero,
              '' AS mg_venta_observaciones,
              NULL AS mg_venta_usuario_id,
              NULL AS mg_venta_customer_id,
              '' AS mg_venta_customer_nombre,
              '' AS mg_venta_numero_alternativo,
            """
        if strong:
            # PequeÃ±o delay best-effort para evitar carrera read-after-write
            try:
                time.sleep(0.12)
            except Exception:
                pass
        row = q(
            f"""
            SELECT
              t.id,
              t.motivo,
              {permite_reparacion_sql}
              t.estado,
              t.presupuesto_estado,
              t.resolucion,
              t.fecha_ingreso,
              t.fecha_servicio,
              t.garantia_reparacion,
              COALESCE(t.etiq_garantia_ok, true) AS etiq_garantia_ok,
              t.faja_garantia,
              t.remito_ingreso,
              t.remito_salida,
              t.factura_numero,
              t.fecha_entrega,
              t.alquilado,
              t.alquiler_a,
              t.alquiler_remito,
              t.alquiler_fecha,
              t.informe_preliminar,
              t.descripcion_problema,
              t.trabajos_realizados,
              t.comentarios,
              t.accesorios,
              t.equipo_variante,
              t.ubicacion_id,
              COALESCE(l.nombre,'') AS ubicacion_nombre,
              t.asignado_a,
              COALESCE(u.nombre,'') AS asignado_a_nombre,
              t.recibido_por AS ingresado_por_id,
              COALESCE(ur.nombre,'') AS ingresado_por_nombre,
              COALESCE(d.propietario_nombre, d.propietario) AS propietario_nombre,
              COALESCE(d.propietario_contacto, '') AS propietario_contacto,
              COALESCE(d.propietario_doc, '') AS propietario_doc,
              d.id AS device_id,
              COALESCE(d.numero_serie,'') AS numero_serie,
              COALESCE(d.numero_interno,'') AS numero_interno,
              {mg_select_sql}
              COALESCE(t.garantia_fabrica,false) AS garantia,
              d.marca_id,
              COALESCE(b.nombre,'') AS marca,
              d.model_id,
              COALESCE(m.nombre,'') AS modelo,
              COALESCE(m.tipo_equipo,'') AS tipo_equipo,
              c.id AS customer_id,
              c.razon_social,
              c.cod_empresa,
              c.telefono
            FROM ingresos t
            JOIN devices d ON d.id = t.device_id
            JOIN customers c ON c.id = d.customer_id
            LEFT JOIN marcas b ON b.id = d.marca_id
            LEFT JOIN models m ON m.id = d.model_id
            LEFT JOIN locations l ON l.id = t.ubicacion_id
            LEFT JOIN users u ON u.id = t.asignado_a
            LEFT JOIN users ur ON ur.id = t.recibido_por
            {mg_sale_customer_join}
            WHERE t.id = %s
            """,
            [ingreso_id],
            one=True,
        )

        # Snapshot inmediato en devices (fase 2: fuente de verdad)
        try:
            exec_void(
                "UPDATE devices SET propietario_nombre = NULLIF(%s,''), propietario_contacto = NULLIF(%s,''), propietario_doc = NULLIF(%s,'') WHERE id = %s",
                [prop_nombre, prop_contacto, prop_doc, device_id]
            )
        except Exception:
            pass
        if not row:
            return Response({"detail": "Ingreso no encontrado"}, status=404)
        row.update(resolve_mg_flags(row, _mg_owner_customer_id()))
        # Agregar serial_cambio si existe la columna
        try:
            with connection.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM information_schema.columns
                     WHERE table_name='ingresos' AND column_name='serial_cambio'
                       AND table_schema = ANY(current_schemas(true))
                     LIMIT 1
                    """
                )
                if cur.fetchone() is not None:
                    cur.execute("SELECT serial_cambio FROM ingresos WHERE id=%s", [row["id"]])
                    sc = cur.fetchone()
                    row["serial_cambio"] = (sc[0] if sc else None)
        except Exception:
            pass
        # Si estÃ¡ en garantÃ­a de reparaciÃ³n, traer trabajos realizados del Ãºltimo servicio entregado
        if row.get("garantia_reparacion"):
            try:
                prev = q(
                    """
                    SELECT id, trabajos_realizados
                      FROM ingresos
                     WHERE device_id = %s
                       AND fecha_entrega IS NOT NULL
                       AND id <> %s
                     ORDER BY fecha_entrega DESC, id DESC
                     LIMIT 1
                    """,
                    [row.get("device_id"), row.get("id")],
                    one=True,
                )
            except Exception:
                prev = None
            if prev:
                row["garantia_reparacion_trabajos"] = prev.get("trabajos_realizados")
            else:
                row["garantia_reparacion_trabajos"] = None
        row["os"] = os_label(row["id"])
        if not row.get("fecha_creacion"):
            row["fecha_creacion"] = row.get("fecha_ingreso")
        if "tipo_equipo_nombre" not in row:
            row["tipo_equipo_nombre"] = row.get("tipo_equipo") or ""
        try:
            from datetime import date as _date, datetime as _dt
            fs = row.get("fecha_servicio")
            if fs and isinstance(fs, _date) and not isinstance(fs, _dt):
                row["fecha_servicio"] = _dt(fs.year, fs.month, fs.day, 0, 0, 0, tzinfo=None)
        except Exception:
            pass
        accs = q(
            """
          SELECT ia.id, ia.accesorio_id, ca.nombre AS accesorio_nombre, ia.referencia, ia.descripcion
          FROM ingreso_accesorios ia
          JOIN catalogo_accesorios ca ON ca.id = ia.accesorio_id AND ca.activo
          WHERE ia.ingreso_id=%s
          ORDER BY ia.id
            """,
            [ingreso_id],
        )
        row["accesorios_items"] = accs
        # Accesorios vinculados a alquiler
        try:
            accs_alq = q(
                """
              SELECT ia.id, ia.accesorio_id, ca.nombre AS accesorio_nombre, ia.referencia, ia.descripcion
              FROM ingreso_alquiler_accesorios ia
              JOIN catalogo_accesorios ca ON ca.id = ia.accesorio_id AND ca.activo
              WHERE ia.ingreso_id=%s
              ORDER BY ia.id
                """,
                [ingreso_id],
            )
        except Exception:
            accs_alq = []
        row["alquiler_accesorios_items"] = accs_alq
        # Resolver tecnico solicitado (ultima solicitud pendiente si existe)
        req = None
        if _table_exists("ingreso_assignment_requests"):
            try:
                req = q(
                    """
                    SELECT r.usuario_id AS uid, COALESCE(u.nombre,'') AS nombre
                      FROM ingreso_assignment_requests r
                      LEFT JOIN users u ON u.id = r.usuario_id
                     WHERE r.ingreso_id = %s
                       AND r.canceled_at IS NULL
                       AND (r.accepted_at IS NULL)
                     ORDER BY r.created_at DESC, r.id DESC
                     LIMIT 1
                    """,
                    [ingreso_id],
                    one=True,
                )
            except Exception:
                req = None
        if not req and _table_exists("audit_log"):
            # Fallback: usar audit_log si esta habilitada/cargada
            try:
                path1 = f"/api/ingresos/{ingreso_id}/solicitar-asignacion/"
                path2 = f"/api/ingresos/{ingreso_id}/solicitar-asignacion"
                req = q(
                    """
                    SELECT al.user_id AS uid, COALESCE(u.nombre,'') AS nombre
                      FROM audit_log al
                      LEFT JOIN users u ON u.id = al.user_id
                     WHERE al.method = 'POST'
                       AND (al.path = %s OR al.path = %s)
                     ORDER BY al.ts DESC, al.id DESC
                     LIMIT 1
                    """,
                    [path1, path2],
                    one=True,
                )
            except Exception:
                req = None
        if req:
            row["tecnico_solicitado_id"] = req.get("uid")
            row["tecnico_solicitado_nombre"] = req.get("nombre") or ""
        else:
            row["tecnico_solicitado_id"] = None
            row["tecnico_solicitado_nombre"] = ""
        baja_req = _pending_baja_request(ingreso_id)
        if baja_req:
            row["baja_solicitada_id"] = baja_req.get("id")
            row["baja_solicitada_nombre"] = baja_req.get("nombre") or ""
            row["baja_solicitada_motivo"] = baja_req.get("motivo") or ""
            row["baja_solicitada_fecha"] = baja_req.get("created_at")
        else:
            row["baja_solicitada_id"] = None
            row["baja_solicitada_nombre"] = ""
            row["baja_solicitada_motivo"] = ""
            row["baja_solicitada_fecha"] = None
        return Response(IngresoDetailWithAccesoriosSerializer(row).data)

    def patch(self, request, ingreso_id: int):
        rol = _rol(request)
        d = request.data or {}
        _set_audit_user(request)
        sync_actor_id = getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)
        has_permite_reparacion = _ingresos_has_permite_reparacion_col()
        permite_reparacion_sql = (
            "COALESCE(permite_reparacion, TRUE) AS permite_reparacion"
            if has_permite_reparacion
            else "TRUE AS permite_reparacion"
        )
        should_sync_preventivo = False
        basic_fields = {
            "propietario_nombre", "propietario_contacto", "propietario_doc",
            "customer_id", "cliente_id", "razon_social", "cod_empresa", "telefono",
            "numero_serie", "equipo_variante", "remito_ingreso", "informe_preliminar",
            "motivo", "garantia", "comentarios", "marca_id", "modelo_id",
        }
        diagnosis_fields = {
            "descripcion_problema", "trabajos_realizados", "fecha_servicio",
            "garantia_reparacion", "etiq_garantia_ok", "faja_garantia", "numero_interno",
        }
        location_fields = {
            "ubicacion_id", "accesorios", "alquilado", "alquiler_a", "alquiler_remito", "alquiler_fecha",
        }
        delivery_fields = {"remito_salida", "factura_numero", "fecha_entrega"}

        if any(k in d for k in basic_fields):
            require_permission(request, "action.ingreso.edit_basics")
        if any(k in d for k in diagnosis_fields):
            require_permission(request, "action.ingreso.edit_diagnosis")
        if any(k in d for k in location_fields):
            require_permission(request, "action.ingreso.edit_location")
        if any(k in d for k in delivery_fields):
            require_permission(request, "action.ingreso.edit_delivery")
        if connection.vendor == "postgresql":
            exec_void("SET LOCAL app.ingreso_id = %s", [ingreso_id])

        row_est = q(
            f"""
            SELECT estado, asignado_a, alquilado, motivo, {permite_reparacion_sql}
              FROM ingresos
             WHERE id=%s
            """,
            [ingreso_id],
            one=True,
        )
        if not row_est:
            return Response({"detail": "Ingreso no encontrado"}, status=404)
        estado_actual = (row_est["estado"] or "").lower()
        asignado_a = row_est["asignado_a"]
        alquilado_actual = bool(row_est["alquilado"])
        motivo_actual = row_est.get("motivo")

        sets_no_estado, params_no_estado = [], []
        needs_warranty_recompute = False

        if "ubicacion_id" in d:
            ubicacion_id = d.get("ubicacion_id")
            if not ubicacion_id:
                raise ValidationError("ubicacion_id requerido")
            u = q("SELECT id FROM locations WHERE id=%s", [ubicacion_id], one=True)
            if not u:
                raise ValidationError("UbicaciÃ³n inexistente")
            sets_no_estado.append("ubicacion_id=%s")
            params_no_estado.append(ubicacion_id)

        desc_present = False
        trab_present = False
        fecha_present = False
        if "descripcion_problema" in d:
            desc = (d.get("descripcion_problema") or "").strip()
            desc_present = bool(desc)
            sets_no_estado.append("descripcion_problema=%s")
            params_no_estado.append(desc)
        if "trabajos_realizados" in d:
            trab_present = bool((d.get("trabajos_realizados") or "").strip())
            sets_no_estado.append("trabajos_realizados=%s")
            params_no_estado.append(d.get("trabajos_realizados"))
        if "accesorios" in d:
            sets_no_estado.append("accesorios=%s")
            params_no_estado.append(d.get("accesorios"))
        if "fecha_servicio" in d:
            should_sync_preventivo = True
            val = d.get("fecha_servicio")
            if val is None or (isinstance(val, str) and val.strip() == ""):
                sets_no_estado.append("fecha_servicio=NULL")
            else:
                dt = parse_datetime(val)
                if not dt:
                    raise ValidationError("fecha_servicio invÃ¡lida")
                if timezone.is_naive(dt):
                    dt = timezone.make_aware(dt, timezone.get_current_timezone())
                sets_no_estado.append("fecha_servicio=%s")
                params_no_estado.append(dt)
                fecha_present = True
        # Si el rol es tecnico/jefe_veedor, solo el asignado puede editar diagnostico/trabajos/fecha
        if rol in ("tecnico", "jefe_veedor") and any(k in d for k in ("descripcion_problema", "trabajos_realizados", "fecha_servicio")):
            uid = getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)
            if int(asignado_a or 0) != int(uid or 0):
                raise PermissionDenied("Solo el tecnico asignado puede editar diagnostico y reparacion")
        if any(k in d for k in ("remito_salida", "factura_numero", "fecha_entrega")):
            if "remito_salida" in d:
                sets_no_estado.append("remito_salida = NULLIF(%s,'')")
                params_no_estado.append((d.get("remito_salida") or "").strip())
            if "factura_numero" in d:
                sets_no_estado.append("factura_numero = NULLIF(%s,'')")
                params_no_estado.append((d.get("factura_numero") or "").strip())
            if "fecha_entrega" in d:
                val = d.get("fecha_entrega")
                if val is None or (isinstance(val, str) and val.strip() == ""):
                    sets_no_estado.append("fecha_entrega=NULL")
                else:
                    dt = parse_datetime(val)
                    if not dt:
                        raise ValidationError("fecha_entrega invÃ¡lida")
                    if timezone.is_naive(dt):
                        dt = timezone.make_aware(dt, timezone.get_current_timezone())
                    sets_no_estado.append("fecha_entrega=%s")
                    params_no_estado.append(dt)
        if "garantia_reparacion" in d:
            sets_no_estado.append("garantia_reparacion=%s")
            params_no_estado.append(bool(d.get("garantia_reparacion")))
        if "etiq_garantia_ok" in d:
            sets_no_estado.append("etiq_garantia_ok=%s")
            params_no_estado.append(bool(d.get("etiq_garantia_ok")))
        if "faja_garantia" in d:
            sets_no_estado.append("faja_garantia = NULLIF(%s,'')")
            params_no_estado.append((d.get("faja_garantia") or "").strip())
        if "numero_interno" in d:
            val_raw = (d.get("numero_interno") or "").strip()
            val = val_raw
            if val and not val.upper().startswith(("MG", "NM", "NV", "CE")):
                val = "MG " + val
            try:
                dev_row = q("SELECT device_id FROM ingresos WHERE id=%s", [ingreso_id], one=True)
                device_id = dev_row and dev_row.get("device_id")
            except Exception:
                device_id = None
            if val and device_id:
                # Verificar conflicto con otro device por MG normalizado
                conflict = q(
                    """
                    SELECT id
                      FROM devices
                     WHERE id <> %s
                       AND numero_interno ~* '^(MG|NM|NV|CE)\\s*\\d{1,4}$'
                       AND UPPER(REGEXP_REPLACE(numero_interno,
                           '^(MG|NM|NV|CE)\\s*(\\d{1,4})$', '\\1 ' || LPAD('\\2',4,'0'))) =
                           UPPER(REGEXP_REPLACE(%s,
                           '^(MG|NM|NV|CE)\\s*(\\d{1,4})$', '\\1 ' || LPAD('\\2',4,'0')))
                     LIMIT 1
                    """,
                    [device_id, val],
                    one=True,
                )
                if conflict:
                    return Response(
                        {
                            "detail": "El nÃºmero interno ya estÃ¡ asignado a otro equipo.",
                            "conflict_type": "MG_DUPLICATE",
                            "conflict_device_id": conflict["id"],
                            "numero_interno_input": val,
                        },
                        status=400,
                    )
            try:
                exec_void(
                    "UPDATE devices SET numero_interno = NULLIF(%s,'') WHERE id = (SELECT device_id FROM ingresos WHERE id=%s)",
                    [val, ingreso_id]
                )
            except Exception as e:
                return Response(
                    {
                        "detail": "No se pudo asignar el nÃºmero interno (posible duplicado).",
                        "conflict_type": "MG_UNIQUE_CONSTRAINT",
                        "numero_interno_input": val,
                        "error": str(e),
                    },
                    status=400,
                )
        if any(k in d for k in ("propietario_nombre", "propietario_contacto", "propietario_doc")):
            if "propietario_nombre" in d:
                sets_no_estado.append("propietario_nombre = NULLIF(%s,'')")
                params_no_estado.append((d.get("propietario_nombre") or "").strip())
            if "propietario_contacto" in d:
                sets_no_estado.append("propietario_contacto = NULLIF(%s,'')")
                params_no_estado.append((d.get("propietario_contacto") or "").strip())
            if "propietario_doc" in d:
                sets_no_estado.append("propietario_doc = NULLIF(%s,'')")
                params_no_estado.append((d.get("propietario_doc") or "").strip())
            # Fase 2: persistir tambiÃ©n en devices
            try:
                exec_void(
                    "UPDATE devices SET propietario_nombre = NULLIF(%s,''), propietario_contacto = NULLIF(%s,''), propietario_doc = NULLIF(%s,'') WHERE id = (SELECT device_id FROM ingresos WHERE id=%s)",
                    [
                        (d.get("propietario_nombre") or "").strip(),
                        (d.get("propietario_contacto") or "").strip(),
                        (d.get("propietario_doc") or "").strip(),
                        ingreso_id,
                    ],
                )
            except Exception:
                pass
        if "customer_id" in d or "cliente_id" in d:
            try:
                new_cid = int(d.get("customer_id") or d.get("cliente_id"))
            except Exception:
                return Response({"detail": "customer_id invÃ¡lido"}, status=400)
            if new_cid <= 0:
                return Response({"detail": "customer_id invÃ¡lido"}, status=400)
            c_row = q("SELECT id FROM customers WHERE id=%s", [new_cid], one=True)
            if not c_row:
                return Response({"detail": "Cliente inexistente"}, status=404)
            exec_void(
                "UPDATE devices SET customer_id=%s WHERE id = (SELECT device_id FROM ingresos WHERE id=%s)",
                [new_cid, ingreso_id],
            )
        if any(k in d for k in ("razon_social", "cod_empresa", "telefono")):
            rs = d.get("razon_social")
            ce = d.get("cod_empresa")
            tel = d.get("telefono")
            sets, params = [], []
            if rs is not None:
                sets.append("razon_social = NULLIF(%s,'')")
                params.append((rs or "").strip())
            if ce is not None:
                sets.append("cod_empresa = NULLIF(%s,'')")
                params.append((ce or "").strip())
            if tel is not None:
                sets.append("telefono = NULLIF(%s,'')")
                params.append((tel or "").strip())
            if sets:
                params.append(ingreso_id)
                exec_void(
                    f"""
                    UPDATE customers
                       SET {', '.join(sets)}
                     WHERE id = (
                        SELECT d.customer_id FROM devices d
                        JOIN ingresos t ON t.device_id = d.id
                        WHERE t.id=%s
                     )
                    """,
                    params,
                )
        if "numero_serie" in d:
            ns = (d.get("numero_serie") or "").strip()
            needs_warranty_recompute = True
            ns_key = (ns or "").replace(" ", "").replace("-", "").upper()
            # Reasociar el ingreso al device que ya tenga ese N/S (normalizado)
            if ns_key:
                row_target = q(
                    """
                    SELECT id FROM devices
                     WHERE REPLACE(REPLACE(UPPER(numero_serie),' ','') ,'-','') = %s
                     LIMIT 1
                    """,
                    [ns_key],
                    one=True,
                )
                if row_target:
                    target_id = int(row_target["id"])
                    row_cur = q("SELECT device_id FROM ingresos WHERE id=%s", [ingreso_id], one=True)
                    if not row_cur:
                        return Response({"detail": "Ingreso no encontrado"}, status=404)
                    cur_dev_id = int(row_cur["device_id"])
                    if cur_dev_id != target_id:
                        exec_void("UPDATE ingresos SET device_id=%s WHERE id=%s", [target_id, ingreso_id])
                        # Resincronizar snapshot del device anterior (si tiene otros ingresos)
                        try:
                            last_old = q(
                                """
                                SELECT id
                                  FROM ingresos
                                 WHERE device_id=%s
                                 ORDER BY COALESCE(fecha_ingreso, fecha_creacion) DESC, id DESC
                                 LIMIT 1
                                """,
                                [cur_dev_id],
                                one=True,
                            )
                            if last_old and last_old.get("id"):
                                # Disparar trigger de snapshot actualizando la misma fila (no-op lÃ³gica)
                                exec_void(
                                    "UPDATE ingresos SET fecha_creacion=fecha_creacion WHERE id=%s",
                                    [int(last_old["id"])],
                                )
                        except Exception:
                            pass
                    # No modificar numero_serie en devices si ya existe ese device
                else:
                    # Fallback: si no existe device con ese N/S, actualizar el N/S del device actual
                    exec_void(
                        "UPDATE devices SET numero_serie = NULLIF(%s,'') WHERE id = (SELECT device_id FROM ingresos WHERE id=%s)",
                        [ns, ingreso_id],
                    )
            else:
                # Si viene vacÃ­Â­o, limpiar N/S del device actual
                exec_void(
                    "UPDATE devices SET numero_serie = NULLIF(%s,'') WHERE id = (SELECT device_id FROM ingresos WHERE id=%s)",
                    [ns, ingreso_id],
                )
            # Si no vino 'garantia' explÃ­Â­cito, auto-chequear por N/S
            # removed: auto-chequeo de garantia por N/S (fuente: ultimo ingreso)
                # (bloque removido)
        # Permitir actualizar variante de equipo (texto libre)
        if "equipo_variante" in d:
            sets_no_estado.append("equipo_variante = NULLIF(%s,'')")
            params_no_estado.append((d.get("equipo_variante") or "").strip())
        if "remito_ingreso" in d:
            sets_no_estado.append("remito_ingreso = NULLIF(%s,'')")
            params_no_estado.append((d.get("remito_ingreso") or "").strip())
        if "informe_preliminar" in d:
            sets_no_estado.append("informe_preliminar = NULLIF(%s,'')")
            params_no_estado.append((d.get("informe_preliminar") or "").strip())
        if "motivo" in d:
            motivo_raw = (d.get("motivo") or "").strip()
            if not motivo_raw:
                raise ValidationError("motivo requerido")
            motivo_label_raw = _map_motivo_to_db_label(motivo_raw)
            if not motivo_label_raw:
                valid_motivos = _get_motivo_enum_values()
                return Response({"detail": "motivo invÃ¡lido", "valid_values": valid_motivos}, status=400)
            raw_vals = _get_motivo_enum_values_raw() or []
            if raw_vals:
                target = None
                norm_target = _norm_txt(motivo_label_raw)
                for rv in raw_vals:
                    if _norm_txt(rv) == norm_target:
                        target = rv
                        break
                if not target:
                    target = next((x for x in raw_vals if _norm_txt(x) == _norm_txt("otros")), raw_vals[0])
                motivo_db = target
            else:
                motivo_db = motivo_label_raw
            sets_no_estado.append("motivo=%s")
            params_no_estado.append(motivo_db)
            if has_permite_reparacion:
                nuevo_es_cotizacion = _motivo_is_cotizacion_equipo(motivo_db)
                anterior_es_cotizacion = _motivo_is_cotizacion_equipo(motivo_actual)
                if nuevo_es_cotizacion and not anterior_es_cotizacion:
                    sets_no_estado.append("permite_reparacion=FALSE")
                elif (not nuevo_es_cotizacion) and anterior_es_cotizacion:
                    sets_no_estado.append("permite_reparacion=TRUE")
        # (actualizacion de garantia en devices removida)
            # bloque removido





        # Actualizar garantia de fÃ¡brica a nivel de ingreso cuando se envÃ­Â­a en el PATCH
        if "garantia" in d:
            sets_no_estado.append("garantia_fabrica=%s")
            params_no_estado.append(bool(d.get("garantia")))
        if "comentarios" in d:
            sets_no_estado.append("comentarios = NULLIF(%s,'')")
            params_no_estado.append((d.get("comentarios") or "").strip())
        # Actualizar marca/modelo del dispositivo asociado al ingreso
        if ("marca_id" in d) or ("modelo_id" in d):
            needs_warranty_recompute = True
            marca_id = d.get("marca_id")
            modelo_id = d.get("modelo_id")
            # Traer device actual
            cur_dev = q("SELECT device_id FROM ingresos WHERE id=%s", [ingreso_id], one=True)
            if not cur_dev:
                return Response({"detail": "Ingreso no encontrado"}, status=404)
            device_id = cur_dev["device_id"]
            if modelo_id is not None:
                # Validar modelo y obtener su marca
                md = q("SELECT id, marca_id FROM models WHERE id=%s", [modelo_id], one=True)
                if not md:
                    raise ValidationError("modelo_id invÃ¡lido")
                modelo_marca = md["marca_id"]
                if marca_id is not None and int(marca_id) != int(modelo_marca):
                    raise ValidationError("modelo_id no pertenece a la marca_id indicada")
                # Ajustar marca al del modelo si no fue provista
                exec_void("UPDATE devices SET model_id=%s, marca_id=%s WHERE id=%s", [modelo_id, modelo_marca, device_id])
            elif marca_id is not None:
                # Validar marca
                mk = q("SELECT id FROM marcas WHERE id=%s", [marca_id], one=True)
                if not mk:
                    raise ValidationError("marca_id invÃ¡lida")
                # Si el modelo actual no corresponde a la nueva marca, ponerlo en NULL
                row = q("SELECT model_id FROM devices WHERE id=%s", [device_id], one=True)
                cur_model = row and row.get("model_id")
                if cur_model:
                    ok = q("SELECT 1 FROM models WHERE id=%s AND marca_id=%s", [cur_model, marca_id], one=True)
                    if not ok:
                        exec_void("UPDATE devices SET marca_id=%s, model_id=NULL WHERE id=%s", [marca_id, device_id])
                    else:
                        exec_void("UPDATE devices SET marca_id=%s WHERE id=%s", [marca_id, device_id])
        if "alquilado" in d:
            new_alquilado = bool(d.get("alquilado"))
            if alquilado_actual and not new_alquilado and rol != "jefe":
                raise PermissionDenied("Solo jefe puede destildar alquiler")
            sets_no_estado.append("alquilado=%s")
            params_no_estado.append(new_alquilado)
            try:
                if new_alquilado:
                    sets_no_estado.append("estado='alquilado'")
                    dash_id = _dash_location_id()
                    if dash_id and not any(str(s).strip().startswith("ubicacion_id=") for s in sets_no_estado):
                        sets_no_estado.append("ubicacion_id=%s")
                        params_no_estado.append(dash_id)
            except Exception:
                pass
        if "alquiler_a" in d:
            sets_no_estado.append("alquiler_a=NULLIF(%s,'')")
            params_no_estado.append((d.get("alquiler_a") or "").strip())
        if "alquiler_remito" in d:
            sets_no_estado.append("alquiler_remito=NULLIF(%s,'')")
            params_no_estado.append((d.get("alquiler_remito") or "").strip())
        if "alquiler_fecha" in d:
            sets_no_estado.append("alquiler_fecha=%s")
            params_no_estado.append(d.get("alquiler_fecha") or None)

        # Recalcular garantÃ­a si cambiÃ³ N/S o marca/modelo
        if needs_warranty_recompute:
            try:
                row_dev = q(
                    """
                    SELECT d.id, d.numero_serie, d.marca_id, d.model_id
                      FROM devices d
                      JOIN ingresos t ON t.device_id = d.id
                     WHERE t.id=%s
                    """,
                    [ingreso_id],
                    one=True,
                )
                if row_dev:
                    calc = compute_warranty(
                        (row_dev.get("numero_serie") or "").strip(),
                        brand_id=row_dev.get("marca_id"),
                        model_id=row_dev.get("model_id"),
                    )
                    if not any(str(s).strip().startswith("garantia_fabrica=") for s in sets_no_estado):
                        sets_no_estado.append("garantia_fabrica=%s")
                        params_no_estado.append(calc.get("garantia"))
                    vence = calc.get("vence_el")
                    if vence is None:
                        exec_void("UPDATE devices SET garantia_vence=NULL WHERE id=%s", [row_dev.get("id")])
                    else:
                        exec_void("UPDATE devices SET garantia_vence=%s WHERE id=%s", [vence, row_dev.get("id")])
            except Exception:
                pass

        diag_present = desc_present or trab_present or fecha_present
        promote_from_ingresado = diag_present and estado_actual == "ingresado"
        promote_from_asignado = diag_present and estado_actual == "asignado"
        if promote_from_ingresado:
            if not asignado_a:
                raise ValidationError("Antes de diagnosticar, asigne un tÃ©cnico al ingreso")
            with transaction.atomic():
                # Auto-setear fecha_servicio solo en la transiciÃ³n ingresado -> diagnosticado
                # si no vino explÃ­Â­cita en el payload y aÃºn estÃ¡ NULL en DB.
                sets_tmp = list(sets_no_estado)
                params_tmp = list(params_no_estado)
                if not fecha_present:
                    # Establecer solo si actualmente es NULL (COALESCE mantiene existente)
                    sets_tmp.append("fecha_servicio=COALESCE(fecha_servicio, %s)")
                    params_tmp.append(timezone.now())
                    should_sync_preventivo = True
                if sets_tmp:
                    params_all = list(params_tmp) + [ingreso_id]
                    q(f"UPDATE ingresos SET {', '.join(sets_tmp)} WHERE id=%s", params_all)
                q("UPDATE ingresos SET estado='diagnosticado' WHERE id=%s AND estado='ingresado'", [ingreso_id])
            if should_sync_preventivo:
                sync_result = sync_plan_from_ingreso_fecha_servicio(ingreso_id, sync_actor_id)
                if (sync_result or {}).get("status") == "error":
                    logger.warning(
                        "preventivo sync returned error status after promote_from_ingresado: ingreso_id=%s",
                        ingreso_id,
                    )
            return Response({"ok": True})
        if promote_from_asignado:
            sets_no_estado.append("estado='diagnosticado'")
        if not sets_no_estado:
            return Response({"ok": True})
        params_no_estado.append(ingreso_id)
        q(f"UPDATE ingresos SET {', '.join(sets_no_estado)} WHERE id=%s", params_no_estado)
        if should_sync_preventivo:
            sync_result = sync_plan_from_ingreso_fecha_servicio(ingreso_id, sync_actor_id)
            if (sync_result or {}).get("status") == "error":
                logger.warning(
                    "preventivo sync returned error status after ingreso patch: ingreso_id=%s",
                    ingreso_id,
                )
        return Response({"ok": True})


# Nuevo: marcar controlado sin defecto (equipos propios revisados sin falla)
class MarcarControladoSinDefectoView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    def post(self, request, ingreso_id: int):
        require_roles(request, ["tecnico", "jefe", "jefe_veedor"])
        # Solo el tÃ©cnico asignado puede marcar (salvo jefes)
        try:
            if _rol(request) in ("tecnico", "jefe_veedor"):
                row = q("SELECT asignado_a FROM ingresos WHERE id=%s", [ingreso_id], one=True)
                uid = getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)
                if not row or int(row.get("asignado_a") or 0) != int(uid or 0):
                    raise PermissionDenied("Solo el tecnico asignado puede marcar como controlado")
        except PermissionDenied:
            raise
        except Exception:
            if _rol(request) in ("tecnico", "jefe_veedor"):
                raise PermissionDenied("Solo el tecnico asignado puede marcar como controlado")

        _set_audit_user(request)
        # Forzar estado y presupuesto_estado
        exec_void(
            "UPDATE ingresos SET estado='controlado_sin_defecto', presupuesto_estado='no_aplica' WHERE id=%s",
            [ingreso_id],
        )

        auto_moved = False
        auto_moved_to = None
        new_ubic_id = None
        try:
            info = q(
                """
                SELECT COALESCE(d.numero_serie,'') AS numero_serie,
                       COALESCE(d.numero_interno,'') AS numero_interno,
                       t.ubicacion_id,
                       COALESCE(loc.nombre,'') AS ubicacion_nombre
                  FROM ingresos t
                  LEFT JOIN devices d ON d.id = t.device_id
                  LEFT JOIN locations loc ON loc.id = t.ubicacion_id
                 WHERE t.id=%s
                """,
                [ingreso_id],
                one=True,
            ) or {}
            ns = (info.get("numero_serie") or "").strip()
            ni = (info.get("numero_interno") or "").strip()
            import re
            pat = re.compile(r"\bMG \d{4}\b", re.IGNORECASE)
            is_mg = bool(pat.search(ns) or pat.search(ni)) or (ns.upper().startswith("MG ") or ni.upper().startswith("MG "))
            if is_mg:
                try:
                    ensure_default_locations()
                except Exception:
                    pass
                target_name = "EstanterÃ­a de Alquiler"
                loc_row = q(
                    "SELECT id, nombre FROM locations WHERE LOWER(nombre)=LOWER(%s) LIMIT 1",
                    [target_name],
                    one=True,
                )
                if loc_row:
                    target_id = loc_row.get("id")
                    cur_id = info.get("ubicacion_id")
                    if target_id and int(cur_id or 0) != int(target_id):
                        exec_void("UPDATE ingresos SET ubicacion_id=%s WHERE id=%s", [target_id, ingreso_id])
                        auto_moved = True
                        auto_moved_to = loc_row.get("nombre") or target_name
                        new_ubic_id = target_id
        except Exception:
            pass

        resp = {"ok": True, "estado": "controlado_sin_defecto", "presupuesto_estado": "no_aplica"}
        if auto_moved:
            resp["auto_moved"] = True
            resp["auto_moved_to"] = auto_moved_to
            resp["ubicacion_id"] = new_ubic_id
            resp["ubicacion_nombre"] = auto_moved_to
        return Response(resp)


__all__ = [
    'MisPendientesView',
    'PendientesPresupuestoView',
    'PresupuestadosView',
    'AprobadosParaRepararView',
    'AprobadosYReparadosView',
    'LiberadosView',
    'GeneralEquiposView',
    'GeneralPorClienteView',
    'MarcarControladoSinDefectoView',
    'MarcarParaRepararView',
    'MarcarReparadoView',
    'HabilitarReparacionCotizacionView',
    'EntregarIngresoView',
    'DarBajaIngresoView',
    'DarAltaIngresoView',
    'IngresoCorreccionesHistoricasView',
    'IngresoAsignarTecnicoView',
    'IngresoSolicitarAsignacionView',
    'IngresoSolicitarBajaView',
    'IngresoSolicitarBajaRechazarView',
    'PendientesGeneralView',
    'IngresoHistorialView',
    'CerrarReparacionView',
]

