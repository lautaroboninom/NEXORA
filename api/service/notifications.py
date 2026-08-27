import json
import logging
from collections import OrderedDict
from email.utils import parseaddr

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import connection, transaction
from django.utils import timezone

try:
    from pywebpush import WebPushException, webpush
except Exception:  # pragma: no cover - dependencia opcional hasta instalar requirements
    WebPushException = Exception
    webpush = None

logger = logging.getLogger(__name__)

MANAGEMENT_NOTIFICATION_ROLES = ("jefe", "jefe_veedor", "admin", "supervisor")
TECHNICAL_NOTIFICATION_ROLES = ("tecnico", "jefe", "jefe_veedor")
DELIVERY_NOTIFICATION_ROLES = ("recepcion", "admin", "supervisor", "ventas", "jefe", "jefe_veedor")
ROUTE_SHEET_NOTIFICATION_ROLES = ("logistica",)
BILLING_NOTIFICATION_ROLES = ("cobranzas", "admin", "supervisor", "jefe", "jefe_veedor")
REMITO_PDF_DELIVERY_ROLES = ("admin", "supervisor", "jefe", "jefe_veedor", "recepcion", "cobranzas")
REMITO_PDF_INGRESS_ROLES = ("admin", "supervisor", "jefe", "jefe_veedor", "recepcion", "cobranzas")
REMITO_PDF_DELIVERY_DEFAULT_ROLES = ("admin", "recepcion", "cobranzas")
REMITO_PDF_INGRESS_DEFAULT_ROLES = ("admin", "recepcion", "cobranzas")
REMITO_PDF_CHANNELS = ("email",)


NOTIFICATION_CATALOG = [
    {
        "key": "ingreso_liberado",
        "label": "Liberado",
        "description": "Equipo liberado con orden de salida impresa.",
        "group": "Ingresos",
        "default_roles": [],
        "allowed_roles": TECHNICAL_NOTIFICATION_ROLES,
    },
    {
        "key": "ingreso_asignado",
        "label": "Asignación técnica",
        "description": "Ingreso asignado o reasignado a un técnico.",
        "group": "Ingresos",
        "default_roles": ["tecnico"],
        "allowed_roles": TECHNICAL_NOTIFICATION_ROLES,
    },
    {
        "key": "sales_order_created",
        "label": "Nueva orden de entrega",
        "description": "Orden de entrega pendiente de preparación para Recepción.",
        "group": "Órdenes de entrega",
        "default_roles": ["recepcion"],
        "allowed_roles": DELIVERY_NOTIFICATION_ROLES,
    },
    {
        "key": "route_stop_created",
        "label": "Nueva parada de Hoja de ruta",
        "description": "Parada agregada a la agenda diaria de entregas.",
        "group": "Hoja de ruta",
        "default_roles": ROUTE_SHEET_NOTIFICATION_ROLES,
        "allowed_roles": ROUTE_SHEET_NOTIFICATION_ROLES,
    },
    {
        "key": "sales_order_remito_ready",
        "label": "Entrega lista para facturar",
        "description": "Orden de entrega con remito disponible para Cobranzas.",
        "group": "Cobranzas",
        "default_roles": ["cobranzas"],
        "allowed_roles": BILLING_NOTIFICATION_ROLES,
    },
    {
        "key": "billing_pending_summary",
        "label": "Resumen de facturación pendiente",
        "description": "Resumen de remitos pendientes para Cobranzas.",
        "group": "Cobranzas",
        "default_roles": ["cobranzas"],
        "allowed_roles": BILLING_NOTIFICATION_ROLES,
    },
    {
        "key": "billing_remito_postponement_due",
        "label": "Remito pospuesto disponible",
        "description": "Un remito pospuesto volvió a la cola de facturación.",
        "group": "Cobranzas",
        "default_roles": ["cobranzas"],
        "allowed_roles": BILLING_NOTIFICATION_ROLES,
        "channels": ("bell",),
    },
    {
        "key": "sales_order_commercial_terms_updated",
        "label": "Corrección comercial de entrega",
        "description": "Precio o plazo corregido en una orden ya entregada con remito.",
        "group": "Cobranzas",
        "default_roles": ["cobranzas"],
        "allowed_roles": BILLING_NOTIFICATION_ROLES,
    },
    {
        "key": "service_order_ready_to_bill",
        "label": "OS lista para facturar",
        "description": "Orden de servicio liberada para facturar como concepto.",
        "group": "Cobranzas",
        "default_roles": ["cobranzas"],
        "allowed_roles": BILLING_NOTIFICATION_ROLES,
    },
    {
        "key": "presupuesto_mg_aprobado",
        "label": "Presupuesto de MG aprobado",
        "description": "Presupuesto aprobado sobre un equipo propio de MG BIO, con el PDF adjunto.",
        "group": "Cobranzas",
        "default_roles": ["cobranzas"],
        "allowed_roles": BILLING_NOTIFICATION_ROLES,
        # Solo correo: el PDF del presupuesto es el aviso, la campana no aporta.
        "channels": ("email",),
    },
    {
        "key": "solicitud_asignacion",
        "label": "Solicitud de asignación",
        "description": "Un técnico solicita que se le asigne un ingreso.",
        "group": "Ingresos",
        "default_roles": ["jefe"],
        "allowed_roles": MANAGEMENT_NOTIFICATION_ROLES,
    },
    {
        "key": "presupuesto_aprobado",
        "label": "Presupuesto aprobado",
        "description": "Presupuesto aprobado para un ingreso asignado.",
        "group": "Presupuestos",
        "default_roles": ["tecnico"],
        "allowed_roles": TECHNICAL_NOTIFICATION_ROLES,
    },
    {
        "key": "presupuesto_rechazado_reparado",
        "label": "Rechazo con el equipo ya reparado",
        "description": "Rechazaron el presupuesto cuando la reparación ya estaba hecha: hay que deshacerla.",
        "group": "Presupuestos",
        "default_roles": ["tecnico"],
        "allowed_roles": TECHNICAL_NOTIFICATION_ROLES,
    },
    {
        "key": "no_se_repara_listo_remito",
        "label": "No se repara - listo para remito",
        "description": "El técnico marcó \"no se repara\": el equipo ya puede salir con orden de salida.",
        "group": "Ingresos",
        # Misma audiencia que "Reparación lista para remito": el equipo está en
        # condiciones de salir y sólo falta imprimir la orden.
        "default_roles": ["recepcion", "jefe"],
        "allowed_roles": (*MANAGEMENT_NOTIFICATION_ROLES, "recepcion", "tecnico"),
    },
    {
        "key": "reparacion_lista_remito",
        "label": "Reparación lista para remito",
        "description": "Equipo reparado que ya puede liberarse con orden de salida.",
        "group": "Ingresos",
        # Recepción imprime la orden de salida y el técnico asignado sigue su equipo.
        # Jefatura también lo quiere: es el aviso de que el equipo ya se puede
        # entregar y sólo falta la orden de salida.
        "default_roles": ["recepcion", "tecnico", "jefe"],
        "allowed_roles": (*MANAGEMENT_NOTIFICATION_ROLES, "recepcion", "tecnico"),
    },
    {
        "key": "solicitud_baja",
        "label": "Solicitud de baja",
        "description": "Solicitud pendiente para dar de baja un ingreso.",
        "group": "Ingresos",
        "default_roles": ["jefe"],
        "allowed_roles": MANAGEMENT_NOTIFICATION_ROLES,
    },
    {
        "key": "ingreso_conflicto_abierto",
        "label": "Conflicto de ingreso abierto",
        "description": "Un equipo volvió a entrar con su orden anterior todavía abierta. El remito ya salió; falta decidir qué pasa con la orden vieja.",
        "group": "Ingresos",
        "default_roles": ["jefe"],
        "allowed_roles": MANAGEMENT_NOTIFICATION_ROLES,
    },
    {
        "key": "baja_patrimonial",
        "label": "Baja patrimonial",
        "description": "Equipo dado de baja para reflejar en gestión patrimonial.",
        "group": "Patrimonio",
        "default_roles": ["jefe"],
        "allowed_roles": MANAGEMENT_NOTIFICATION_ROLES,
    },
    {
        "key": "alta_patrimonial",
        "label": "Alta patrimonial",
        "description": "Equipo dado de alta para reflejar en gestión patrimonial.",
        "group": "Patrimonio",
        "default_roles": ["jefe"],
        "allowed_roles": MANAGEMENT_NOTIFICATION_ROLES,
    },
    {
        "key": "venta_patrimonial",
        "label": "Venta patrimonial",
        "description": "Equipo propio MG vendido, para reflejar la baja por venta en gestión patrimonial.",
        "group": "Patrimonio",
        "default_roles": ["jefe"],
        "allowed_roles": MANAGEMENT_NOTIFICATION_ROLES,
    },
    {
        "key": "derivacion_devuelta",
        "label": "Derivación devuelta",
        "description": "Equipo devuelto desde un proveedor externo.",
        "group": "Derivaciones",
        "default_roles": ["tecnico"],
        "allowed_roles": TECHNICAL_NOTIFICATION_ROLES,
    },
    {
        "key": "preventivo_vencido",
        "label": "Preventivo vencido",
        "description": "Mantenimiento preventivo vencido.",
        "group": "Preventivos",
        "default_roles": ["jefe"],
        "allowed_roles": MANAGEMENT_NOTIFICATION_ROLES,
    },
    {
        "key": "preventivo_proximo",
        "label": "Preventivo próximo",
        "description": "Mantenimiento preventivo próximo a vencer.",
        "group": "Preventivos",
        "default_roles": ["jefe"],
        "allowed_roles": MANAGEMENT_NOTIFICATION_ROLES,
    },
    {
        "key": "stock_minimo",
        "label": "Stock mínimo",
        "description": "Repuesto que llegó al stock mínimo.",
        "group": "Repuestos",
        "default_roles": ["jefe", "jefe_veedor"],
        "allowed_roles": MANAGEMENT_NOTIFICATION_ROLES,
    },
    {
        "key": "presupuesto_pendiente",
        "label": "Presupuesto pendiente",
        "description": "Presupuesto emitido con aprobación demorada.",
        "group": "Presupuestos",
        "default_roles": ["jefe"],
        "allowed_roles": MANAGEMENT_NOTIFICATION_ROLES,
    },
    {
        "key": "presupuesto_decision_portal",
        "label": "Decision de presupuesto desde Portal",
        "description": "Un cliente aprobo o rechazo un presupuesto desde el Portal.",
        "group": "Presupuestos",
        "default_roles": ["jefe"],
        "allowed_roles": MANAGEMENT_NOTIFICATION_ROLES,
    },
    {
        "key": "resumen_operativo",
        "label": "Resumen operativo",
        "description": "Resumen de alertas y objetivos del servicio técnico.",
        "group": "Operación",
        "default_roles": ["jefe", "jefe_veedor", "admin"],
        "allowed_roles": MANAGEMENT_NOTIFICATION_ROLES,
    },
    {
        "key": "bejerman_sync_job_blocked",
        "label": "Sincronización Bejerman bloqueada",
        "description": "Job de sincronización Bejerman bloqueado que requiere intervención.",
        "group": "Integración Bejerman",
        "default_roles": ["admin", "supervisor"],
        "allowed_roles": MANAGEMENT_NOTIFICATION_ROLES,
        "channels": ("email",),
    },
    {
        "key": "remito_pdf_rt",
        "label": "PDF remito RT",
        "description": "PDF impreso de remitos Bejerman RT de venta.",
        "group": "Remitos Bejerman",
        "default_roles": REMITO_PDF_DELIVERY_DEFAULT_ROLES,
        "allowed_roles": REMITO_PDF_DELIVERY_ROLES,
        "channels": REMITO_PDF_CHANNELS,
    },
    {
        "key": "remito_pdf_rd",
        "label": "PDF remito RD",
        "description": "PDF impreso de remitos Bejerman RD.",
        "group": "Remitos Bejerman",
        "default_roles": REMITO_PDF_DELIVERY_DEFAULT_ROLES,
        "allowed_roles": REMITO_PDF_DELIVERY_ROLES,
        "channels": REMITO_PDF_CHANNELS,
    },
    {
        "key": "remito_pdf_rta",
        "label": "PDF remito RTA",
        "description": "PDF impreso de remitos Bejerman RTA de alquiler.",
        "group": "Remitos Bejerman",
        "default_roles": REMITO_PDF_DELIVERY_DEFAULT_ROLES,
        "allowed_roles": REMITO_PDF_DELIVERY_ROLES,
        "channels": REMITO_PDF_CHANNELS,
    },
    {
        "key": "remito_pdf_rtn",
        "label": "PDF remito RTN",
        "description": "PDF impreso de remitos Bejerman RTN de demo.",
        "group": "Remitos Bejerman",
        "default_roles": REMITO_PDF_DELIVERY_DEFAULT_ROLES,
        "allowed_roles": REMITO_PDF_DELIVERY_ROLES,
        "channels": REMITO_PDF_CHANNELS,
    },
    {
        "key": "remito_pdf_rss",
        "label": "PDF remito RSS",
        "description": "PDF impreso de remitos Bejerman RSS de servicio técnico.",
        "group": "Remitos Bejerman",
        "default_roles": REMITO_PDF_DELIVERY_DEFAULT_ROLES,
        "allowed_roles": REMITO_PDF_DELIVERY_ROLES,
        "channels": REMITO_PDF_CHANNELS,
    },
    {
        "key": "remito_pdf_ris",
        "label": "PDF remito RIS",
        "description": "PDF impreso de remitos Bejerman RIS de ingreso.",
        "group": "Remitos Bejerman",
        "default_roles": REMITO_PDF_INGRESS_DEFAULT_ROLES,
        "allowed_roles": REMITO_PDF_INGRESS_ROLES,
        "channels": REMITO_PDF_CHANNELS,
    },
    {
        "key": "remito_pdf_rda",
        "label": "PDF remito RDA",
        "description": "PDF impreso de remitos Bejerman RDA de devolución de alquiler.",
        "group": "Remitos Bejerman",
        "default_roles": REMITO_PDF_INGRESS_DEFAULT_ROLES,
        "allowed_roles": REMITO_PDF_INGRESS_ROLES,
        "channels": REMITO_PDF_CHANNELS,
    },
    {
        "key": "remito_pdf_rdn",
        "label": "PDF remito RDN",
        "description": "PDF impreso de remitos Bejerman RDN de devolución de demo.",
        "group": "Remitos Bejerman",
        "default_roles": REMITO_PDF_INGRESS_DEFAULT_ROLES,
        "allowed_roles": REMITO_PDF_INGRESS_ROLES,
        "channels": REMITO_PDF_CHANNELS,
    },
]

CATALOG_BY_KEY = {item["key"]: item for item in NOTIFICATION_CATALOG}
DEFAULT_LIMIT = 20
MAX_LIMIT = 50
NOTIFICATION_CHANNELS = ("bell", "email", "push")
EXTRA_EMAIL_ROLES = {"admin", "supervisor", "cobranzas"}


def q(sql, params=None, one=False):
    with connection.cursor() as cur:
        cur.execute(sql, params or [])
        if not cur.description:
            return None
        cols = [col[0] for col in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        if one:
            return rows[0] if rows else None
        return rows


def os_label(value):
    try:
        return str(int(value)).zfill(5)
    except Exception:
        return str(value)


def _safe_set_rollback_false():
    try:
        transaction.set_rollback(False)
    except Exception:
        pass


def _table_exists(name):
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                  FROM information_schema.tables
                 WHERE table_schema = ANY(current_schemas(true))
                   AND table_name = %s
                 LIMIT 1
                """,
                [name],
            )
            return bool(cur.fetchone())
    except Exception:
        _safe_set_rollback_false()
        return False


def _table_columns(name):
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_schema = ANY(current_schemas(true))
                   AND table_name = %s
                """,
                [name],
            )
            return {str(row[0]) for row in cur.fetchall()}
    except Exception:
        _safe_set_rollback_false()
        return set()


def _table_has_column(name, column):
    return column in _table_columns(name)


def notifications_schema_ready():
    return _table_exists("notifications") and _table_exists("notification_user_preferences")


def push_schema_ready():
    return _table_exists("notification_push_subscriptions")


def notification_email_schema_ready():
    return _table_exists("notification_email_addresses")


def _web_push_settings():
    public_key = str(getattr(settings, "WEB_PUSH_VAPID_PUBLIC_KEY", "") or "").strip()
    private_key = str(getattr(settings, "WEB_PUSH_VAPID_PRIVATE_KEY", "") or "").strip().replace("\\n", "\n")
    subject = str(getattr(settings, "WEB_PUSH_VAPID_SUBJECT", "") or "").strip()
    if not subject:
        from_email = parseaddr(str(getattr(settings, "DEFAULT_FROM_EMAIL", "") or ""))[1] or "no-reply@sepid.com.ar"
        subject = f"mailto:{from_email}"
    return public_key, private_key, subject


def web_push_available():
    public_key, private_key, _subject = _web_push_settings()
    return bool(public_key and private_key and webpush)


def active_push_subscription_count(user_id):
    if not push_schema_ready():
        return 0
    row = q(
        """
        SELECT COUNT(*) AS total
          FROM notification_push_subscriptions
         WHERE user_id = %s
           AND disabled_at IS NULL
        """,
        [user_id],
        one=True,
    ) or {}
    return int(row.get("total") or 0)


def get_push_config_for_user(user_id):
    public_key, private_key, _subject = _web_push_settings()
    return {
        "available": bool(public_key and private_key and webpush),
        "publicKey": public_key if public_key and private_key else "",
        "active": active_push_subscription_count(user_id) > 0,
    }


def _subscription_fields(subscription):
    data = subscription.get("subscription") if isinstance(subscription, dict) and isinstance(subscription.get("subscription"), dict) else subscription
    if not isinstance(data, dict):
        raise ValueError("Suscripción inválida.")
    keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}
    endpoint = str(data.get("endpoint") or "").strip()
    p256dh = str(keys.get("p256dh") or data.get("p256dh") or "").strip()
    auth = str(keys.get("auth") or data.get("auth") or "").strip()
    content_encoding = str(
        data.get("contentEncoding")
        or data.get("content_encoding")
        or data.get("encoding")
        or "aes128gcm"
    ).strip() or "aes128gcm"
    if not endpoint or not p256dh or not auth:
        raise ValueError("La suscripción push está incompleta.")
    return endpoint, p256dh, auth, content_encoding


def save_push_subscription(user_id, subscription, user_agent=""):
    if not push_schema_ready():
        raise RuntimeError("El esquema de notificaciones push no está aplicado.")
    endpoint, p256dh, auth, content_encoding = _subscription_fields(subscription)
    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO notification_push_subscriptions(
              user_id, endpoint, p256dh, auth, content_encoding, user_agent,
              disabled_at, failure_count, last_error
            ) VALUES (%s,%s,%s,%s,%s,%s,NULL,0,'')
            ON CONFLICT (endpoint) DO UPDATE SET
              user_id = EXCLUDED.user_id,
              p256dh = EXCLUDED.p256dh,
              auth = EXCLUDED.auth,
              content_encoding = EXCLUDED.content_encoding,
              user_agent = EXCLUDED.user_agent,
              disabled_at = NULL,
              failure_count = 0,
              last_error = '',
              updated_at = CURRENT_TIMESTAMP
            RETURNING id, endpoint
            """,
            [
                user_id,
                endpoint,
                p256dh,
                auth,
                content_encoding[:64],
                str(user_agent or "")[:500],
            ],
        )
        row = cur.fetchone()
    return {"id": int(row[0]), "endpoint": row[1], "active": True}


def delete_push_subscription(user_id, endpoint=None):
    if not push_schema_ready():
        return 0
    endpoint_value = str(endpoint or "").strip()
    with connection.cursor() as cur:
        if endpoint_value:
            cur.execute(
                """
                DELETE FROM notification_push_subscriptions
                 WHERE user_id = %s
                   AND endpoint = %s
                """,
                [user_id, endpoint_value],
            )
        else:
            cur.execute(
                "DELETE FROM notification_push_subscriptions WHERE user_id = %s",
                [user_id],
            )
        return int(cur.rowcount or 0)


def _load_push_subscriptions(user_id):
    if not push_schema_ready():
        return []
    return q(
        """
        SELECT id, endpoint, p256dh, auth, content_encoding
          FROM notification_push_subscriptions
         WHERE user_id = %s
           AND disabled_at IS NULL
         ORDER BY updated_at DESC, id DESC
        """,
        [user_id],
    ) or []


def _mark_push_success(subscription_id):
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE notification_push_subscriptions
               SET failure_count = 0,
                   last_error = '',
                   last_success_at = CURRENT_TIMESTAMP,
                   updated_at = CURRENT_TIMESTAMP
             WHERE id = %s
            """,
            [subscription_id],
        )


def _mark_push_failure(subscription_id, error, disable=False):
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE notification_push_subscriptions
               SET failure_count = failure_count + 1,
                   last_error = %s,
                   disabled_at = CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE disabled_at END,
                   updated_at = CURRENT_TIMESTAMP
             WHERE id = %s
            """,
            [str(error or "")[:500], bool(disable), subscription_id],
        )


def _web_push_exception_status(exc):
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    try:
        return int(status_code) if status_code is not None else None
    except Exception:
        return None


def send_web_push_to_user(user_id, notification):
    if not web_push_available():
        return 0
    _public_key, private_key, subject = _web_push_settings()
    subscriptions = _load_push_subscriptions(user_id)
    if not subscriptions:
        return 0
    data = json.dumps(notification or {}, ensure_ascii=False, default=str)
    sent = 0
    for sub in subscriptions:
        subscription_info = {
            "endpoint": sub.get("endpoint"),
            "keys": {
                "p256dh": sub.get("p256dh"),
                "auth": sub.get("auth"),
            },
        }
        try:
            webpush(
                subscription_info=subscription_info,
                data=data,
                vapid_private_key=private_key,
                vapid_claims={"sub": subject},
            )
            _mark_push_success(sub["id"])
            sent += 1
        except WebPushException as exc:
            status_code = _web_push_exception_status(exc)
            _mark_push_failure(sub["id"], exc, disable=status_code in {404, 410})
            logger.warning(
                "No se pudo enviar la notificación push",
                extra={"user_id": user_id, "subscription_id": sub["id"], "status_code": status_code},
            )
        except Exception as exc:
            _mark_push_failure(sub["id"], exc)
            logger.exception(
                "Error inesperado enviando notificación push",
                extra={"user_id": user_id, "subscription_id": sub["id"]},
            )
    return sent


def _push_notification_payload(notification_key, notification_id, title, body, href, severity, entity_type, entity_id, dedupe_key, payload):
    href_value = str(href or "/").strip() or "/"
    return {
        "title": str(title or "").strip() or "NEXORA",
        "body": str(body or "").strip(),
        "href": href_value,
        "icon": "/icons/logo-app-192.png",
        "badge": "/icons/logo-app-192.png",
        "tag": f"nexora:{notification_key}:{dedupe_key or notification_id}",
        "notificationId": notification_id,
        "notificationKey": notification_key,
        "severity": severity,
        "entityType": entity_type,
        "entityId": str(entity_id) if entity_id is not None else "",
        "payload": payload or {},
    }


def _as_bool(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


def _clean_email(value):
    _name, email = parseaddr(str(value or "").strip())
    email = str(email or "").strip()
    if not email:
        raise ValueError("El email es obligatorio.")
    try:
        validate_email(email)
    except DjangoValidationError:
        raise ValueError("El email no es válido.")
    return email


def _normalize_role(role):
    return (role or "").strip().lower()


def _can_manage_extra_emails_for_role(role):
    return _normalize_role(role) in EXTRA_EMAIL_ROLES


def _normalize_catalog_roles(values):
    return {
        _normalize_role(value)
        for value in (values or [])
        if _normalize_role(value)
    }


def _item_allowed_roles(item):
    if "allowed_roles" in item:
        return _normalize_catalog_roles(item.get("allowed_roles"))
    return _normalize_catalog_roles(item.get("default_roles"))


def _item_channels(item):
    raw_channels = item.get("channels") or NOTIFICATION_CHANNELS
    channels = tuple(
        channel
        for channel in (str(value or "").strip().lower() for value in raw_channels)
        if channel in NOTIFICATION_CHANNELS
    )
    return channels or NOTIFICATION_CHANNELS


def _role_allowed_for_item(item, role):
    role_key = _normalize_role(role)
    return bool(role_key and role_key in _item_allowed_roles(item or {}))


def _role_allowed_for_notification(notification_key, role):
    item = CATALOG_BY_KEY.get(notification_key) or {}
    return _role_allowed_for_item(item, role)


def _channel_allowed_for_notification(notification_key, channel):
    item = CATALOG_BY_KEY.get(notification_key) or {}
    return str(channel or "").strip().lower() in _item_channels(item)


def notification_allowed_roles(notification_key):
    return sorted(_item_allowed_roles(CATALOG_BY_KEY.get(notification_key) or {}))


def _role_default_enabled(notification_key, role):
    item = CATALOG_BY_KEY.get(notification_key) or {}
    role_key = _normalize_role(role)
    if not _role_allowed_for_item(item, role_key):
        return False
    return role_key in {str(r).strip().lower() for r in item.get("default_roles") or []}


def _load_active_users(user_ids=None, roles=None, emails=None):
    filters = ["COALESCE(activo, TRUE) = TRUE"]
    params = []
    if user_ids:
        ids = sorted({int(uid) for uid in user_ids if uid is not None})
        if not ids:
            return []
        filters.append("id = ANY(%s)")
        params.append(ids)
    if roles:
        role_values = sorted({_normalize_role(role) for role in roles if role})
        if not role_values:
            return []
        filters.append("LOWER(COALESCE(rol, '')) = ANY(%s)")
        params.append(role_values)
    if emails:
        email_values = sorted({str(email or "").strip().lower() for email in emails if str(email or "").strip()})
        if not email_values:
            return []
        filters.append("LOWER(TRIM(CAST(email AS TEXT))) = ANY(%s)")
        params.append(email_values)
    return q(
        f"""
        SELECT id, nombre, email, rol
          FROM users
         WHERE {' AND '.join(filters)}
         ORDER BY id
        """,
        params,
    ) or []


def _load_overrides(notification_key, user_ids):
    ids = sorted({int(uid) for uid in user_ids if uid is not None})
    if not ids or not _table_exists("notification_user_preferences"):
        return {}
    columns = _table_columns("notification_user_preferences")
    select_parts = ["user_id"]
    select_parts.append("enabled" if "enabled" in columns else "NULL AS enabled")
    select_parts.append("bell_enabled" if "bell_enabled" in columns else "NULL AS bell_enabled")
    select_parts.append("email_enabled" if "email_enabled" in columns else "NULL AS email_enabled")
    select_parts.append("push_enabled" if "push_enabled" in columns else "NULL AS push_enabled")
    rows = q(
        f"""
        SELECT {', '.join(select_parts)}
          FROM notification_user_preferences
         WHERE notification_key = %s
           AND user_id = ANY(%s)
        """,
        [notification_key, ids],
    ) or []
    overrides = {}
    for row in rows:
        legacy = _as_bool(row.get("enabled"))
        bell = _as_bool(row.get("bell_enabled"))
        overrides[int(row["user_id"])] = {
            "legacy": legacy,
            "bell": legacy if bell is None else bell,
            "email": _as_bool(row.get("email_enabled")),
            "push": _as_bool(row.get("push_enabled")),
        }
    return overrides


def _effective_enabled(user_row, notification_key, overrides, channel="bell"):
    uid = int(user_row.get("id"))
    channel_key = channel if channel in NOTIFICATION_CHANNELS else "bell"
    if not _channel_allowed_for_notification(notification_key, channel_key):
        return False
    if not _role_allowed_for_notification(notification_key, user_row.get("rol")):
        return False
    override = overrides.get(uid) or {}
    value = override.get(channel_key)
    if value is not None:
        return bool(value)
    legacy = override.get("legacy")
    if channel_key != "bell" and legacy is not None:
        return bool(legacy)
    return _role_default_enabled(notification_key, user_row.get("rol"))


def _effective_any_enabled(user_row, notification_key, overrides):
    return any(_effective_enabled(user_row, notification_key, overrides, channel) for channel in NOTIFICATION_CHANNELS)


def _load_notification_email_addresses(user_ids):
    ids = sorted({int(uid) for uid in user_ids if uid is not None})
    if not ids or not notification_email_schema_ready():
        return {}
    rows = q(
        """
        SELECT id, user_id, email, label, active, created_at, updated_at
          FROM notification_email_addresses
         WHERE user_id = ANY(%s)
           AND COALESCE(active, TRUE) = TRUE
         ORDER BY user_id, LOWER(TRIM(email)), id
        """,
        [ids],
    ) or []
    grouped = {}
    for row in rows:
        grouped.setdefault(int(row["user_id"]), []).append(row)
    return grouped


def notification_email_recipients_for_users(users, *, include_primary=True, include_extra=True):
    rows = [row for row in users or [] if row]
    extras_by_user = _load_notification_email_addresses([row.get("id") for row in rows]) if include_extra else {}
    recipients = OrderedDict()
    for row in rows:
        primary = str(row.get("email") or "").strip()
        if include_primary and primary:
            recipients.setdefault(primary.lower(), primary)
        if include_extra and _can_manage_extra_emails_for_role(row.get("rol")):
            for extra in extras_by_user.get(int(row.get("id")), []):
                email = str(extra.get("email") or "").strip()
                if email:
                    recipients.setdefault(email.lower(), email)
    return list(recipients.values())


def _user_has_notification_email(user_row):
    return bool(notification_email_recipients_for_users([user_row]))


def _owners_by_email(addresses):
    """Mapea cada dirección al usuario activo que la tiene, principal o extra."""
    owners = {}
    if not addresses:
        return owners
    for row in _load_active_users(emails=addresses):
        email = str(row.get("email") or "").strip().lower()
        if email:
            owners.setdefault(email, row)
    if not notification_email_schema_ready():
        return owners
    pending = [value for value in addresses if value not in owners]
    if not pending:
        return owners
    rows = q(
        """
        SELECT u.id, u.nombre, u.email, u.rol,
               LOWER(TRIM(a.email)) AS extra_email
          FROM notification_email_addresses a
          JOIN users u ON u.id = a.user_id
         WHERE COALESCE(a.active, TRUE) = TRUE
           AND COALESCE(u.activo, TRUE) = TRUE
           AND LOWER(TRIM(a.email)) = ANY(%s)
        """,
        [sorted(pending)],
    ) or []
    for row in rows:
        owners.setdefault(row.pop("extra_email"), row)
    return owners


def filter_email_recipients_by_preference(notification_key, recipients):
    """Saca de una lista de correo a los usuarios que apagaron el canal Mail.

    Los avisos más viejos mandan el correo con ``send_mail`` a una lista fija
    (variables de entorno o una consulta SQL de roles) sin pasar nunca por el
    centro de notificaciones, así que el toggle "Mail" de la campana no tenía
    ningún efecto sobre ellos.

    Sólo saca a quien apagó "Mail" **explícitamente** para esa notificación. No
    se toca a quien nunca abrió la configuración: la lista de entorno sigue
    siendo la que manda por defecto, así que activar el filtro no deja a nadie
    sin avisos de golpe. Las direcciones que no pertenecen a ningún usuario de
    NEXORA (casillas externas o compartidas) se dejan pasar siempre porque no
    hay dónde configurarlas.
    """
    clean = []
    seen = set()
    for raw in recipients or []:
        value = str(raw or "").strip()
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            clean.append(value)
    if not clean or notification_key not in CATALOG_BY_KEY or not notifications_schema_ready():
        return clean

    owners = _owners_by_email([value.lower() for value in clean])
    if not owners:
        return clean
    overrides = _load_overrides(notification_key, [row.get("id") for row in owners.values()])

    allowed = []
    for value in clean:
        owner = owners.get(value.lower())
        if owner is None or not _role_allowed_for_notification(notification_key, owner.get("rol")):
            allowed.append(value)
            continue
        override = overrides.get(int(owner.get("id"))) or {}
        explicit = override.get("email")
        if explicit is None:
            # Fila vieja, sin columnas por canal: `enabled` valía para todo.
            explicit = override.get("legacy")
        if explicit is False:
            continue
        allowed.append(value)
    return allowed


def _candidate_users(notification_key, user_ids=None, roles=None, emails=None):
    """Resuelve la audiencia interna (campana/push) de una notificación.

    Ojo con ``emails``: NO envía correo, sólo busca el usuario dueño de cada
    dirección y lo suma a la audiencia interna. Si se le pasa una lista de
    destinatarios de correo configurada por entorno (p. ej.
    ``ASSIGNMENT_REQUEST_RECIPIENTS``) y esa casilla pertenece a un usuario de
    NEXORA, ese usuario empieza a recibir la campana aunque nunca la haya
    pedido. Para avisos que sólo son correo, mandar el mail por
    ``queue_notification_email``/``send_mail`` y no pasar ``emails`` acá.
    """
    users = OrderedDict()
    if user_ids:
        for row in _load_active_users(user_ids=user_ids):
            users[int(row["id"])] = row
    if emails:
        for row in _load_active_users(emails=emails):
            users[int(row["id"])] = row
    return list(users.values())


def user_ids_subscribed_to(notification_key, roles):
    """Usuarios activos de esos roles que hoy tienen prendido el aviso.

    ``emit_notification`` sólo le escribe a la audiencia que le pasan, así que
    para los avisos con destinatarios calculados a mano (recepción, el técnico
    asignado) el toggle del centro de notificaciones no tenía ningún efecto
    sobre el resto de los roles permitidos: quien lo prendía seguía sin recibir
    nada. Con esto, prenderlo alcanza.
    """
    if notification_key not in CATALOG_BY_KEY:
        return []
    candidates = [
        row
        for row in _load_active_users(roles=roles)
        if _role_allowed_for_notification(notification_key, row.get("rol"))
    ]
    if not candidates:
        return []
    overrides = _load_overrides(notification_key, [row.get("id") for row in candidates])
    return [
        int(row["id"])
        for row in candidates
        if _effective_any_enabled(row, notification_key, overrides)
    ]


def active_user_ids_for_roles(roles):
    return [int(row["id"]) for row in _load_active_users(roles=roles)]


def active_users_for_roles(roles):
    return _load_active_users(roles=roles)


def active_users_for_notification(notification_key, *, user_ids=None, roles=None, emails=None, require_email=False, channel=None):
    if notification_key not in CATALOG_BY_KEY:
        return []
    candidates = OrderedDict()
    if user_ids:
        for row in _load_active_users(user_ids=user_ids):
            candidates[int(row["id"])] = row
    if roles:
        for row in _load_active_users(roles=roles):
            candidates[int(row["id"])] = row
    if emails:
        for row in _load_active_users(emails=emails):
            candidates[int(row["id"])] = row
    if not candidates:
        return []
    candidates = OrderedDict(
        (uid, row)
        for uid, row in candidates.items()
        if _role_allowed_for_notification(notification_key, row.get("rol"))
    )
    if not candidates:
        return []
    overrides = _load_overrides(notification_key, list(candidates))
    channel_key = channel or ("email" if require_email else "bell")
    if channel_key == "any":
        recipients = [row for row in candidates.values() if _effective_any_enabled(row, notification_key, overrides)]
    else:
        if not _channel_allowed_for_notification(notification_key, channel_key):
            return []
        recipients = [
            row
            for row in candidates.values()
            if _effective_enabled(row, notification_key, overrides, channel_key)
        ]
    if require_email:
        recipients = [row for row in recipients if _user_has_notification_email(row)]
    return recipients


def emit_notification(
    notification_key,
    *,
    title,
    body="",
    href="",
    severity="info",
    entity_type=None,
    entity_id=None,
    dedupe_key=None,
    payload=None,
    user_ids=None,
    roles=None,
    emails=None,
    push=False,
    refresh=False,
):
    """Crea (o refresca) la notificación interna para la audiencia indicada.

    ``refresh=True`` es para los avisos que un cron repite mientras la
    situación siga abierta: en vez de dejar una fila nueva por corrida, se
    reusa la del ``dedupe_key`` estable, se actualiza el texto y vuelve a
    quedar sin leer. Así la campana muestra un renglón por caso y no una pila
    del mismo aviso repetido día por día.
    """
    if notification_key not in CATALOG_BY_KEY:
        return 0
    if not notifications_schema_ready():
        return 0

    # "roles" queda solo por compatibilidad de firma: ya no crea audiencia interna.
    candidates = _candidate_users(notification_key, user_ids=user_ids, roles=roles, emails=emails)
    if not candidates:
        return 0
    candidates = [row for row in candidates if _role_allowed_for_notification(notification_key, row.get("rol"))]
    if not candidates:
        return 0
    overrides = _load_overrides(notification_key, [row.get("id") for row in candidates])
    recipients = [row for row in candidates if _effective_any_enabled(row, notification_key, overrides)]
    if not recipients:
        return 0

    key = (dedupe_key or f"{notification_key}:{entity_type or 'general'}:{entity_id or title}").strip()
    severity_value = severity if severity in {"info", "warning", "critical"} else "info"
    payload_text = json.dumps(payload or {}, ensure_ascii=False, default=str)
    has_bell_enabled_column = _table_has_column("notifications", "bell_enabled")
    conflict_target = "ON CONFLICT (user_id, notification_key, dedupe_key)"
    if refresh:
        # Vuelve a mostrarla sin leer y con el texto de esta corrida (los días
        # sin aprobación, el conteo del resumen, etc.) **sólo si el aviso
        # cambió**. Sin el WHERE de abajo, cada corrida del cron pisaba la fila
        # con los mismos datos, la volvía a marcar sin leer y le ponía
        # `created_at` de hoy: el mismo aviso reaparecía todos los días arriba
        # de todo y empujaba fuera de la campana a los avisos de hechos reales.
        refresh_sets = [
            "title = EXCLUDED.title",
            "body = EXCLUDED.body",
            "href = EXCLUDED.href",
            "severity = EXCLUDED.severity",
            "payload = EXCLUDED.payload",
            "created_at = CURRENT_TIMESTAMP",
            "updated_at = CURRENT_TIMESTAMP",
            "read_at = NULL",
            "clicked_at = NULL",
        ]
        if has_bell_enabled_column:
            refresh_sets.append("bell_enabled = EXCLUDED.bell_enabled")
        # Con este WHERE, una repetición idéntica no actualiza nada y el
        # `RETURNING` no devuelve fila: no se cuenta como emitida y tampoco
        # dispara el push al teléfono.
        refresh_where = (
            "WHERE notifications.title IS DISTINCT FROM EXCLUDED.title"
            "   OR notifications.body IS DISTINCT FROM EXCLUDED.body"
            "   OR notifications.severity IS DISTINCT FROM EXCLUDED.severity"
        )
        conflict_sql = (
            f"{conflict_target} DO UPDATE SET "
            + ", ".join(refresh_sets)
            + " "
            + refresh_where
        )
    else:
        conflict_sql = f"{conflict_target} DO NOTHING"

    inserted = 0
    push_targets = []
    for user in recipients:
        bell_enabled = _effective_enabled(user, notification_key, overrides, "bell")
        push_enabled = bool(push and _effective_enabled(user, notification_key, overrides, "push"))
        try:
            with connection.cursor() as cur:
                if has_bell_enabled_column:
                    cur.execute(
                        """
                        INSERT INTO notifications(
                          user_id, notification_key, dedupe_key, title, body, href,
                          severity, entity_type, entity_id, payload, bell_enabled
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
{conflict_sql}
                        RETURNING id
                        """.format(conflict_sql=conflict_sql),
                        [
                            user.get("id"),
                            notification_key,
                            key,
                            str(title or "").strip(),
                            str(body or "").strip(),
                            str(href or "").strip(),
                            severity_value,
                            entity_type,
                            str(entity_id) if entity_id is not None else None,
                            payload_text,
                            bool(bell_enabled),
                        ],
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO notifications(
                          user_id, notification_key, dedupe_key, title, body, href,
                          severity, entity_type, entity_id, payload
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
{conflict_sql}
                        RETURNING id
                        """.format(conflict_sql=conflict_sql),
                        [
                            user.get("id"),
                            notification_key,
                            key,
                            str(title or "").strip(),
                            str(body or "").strip(),
                            str(href or "").strip(),
                            severity_value,
                            entity_type,
                            str(entity_id) if entity_id is not None else None,
                            payload_text,
                        ],
                    )
                row = cur.fetchone()
                if row:
                    inserted += 1
                    if push_enabled:
                        push_targets.append((int(user.get("id")), int(row[0])))
        except Exception:
            logger.exception(
                "No se pudo crear la notificación interna",
                extra={"notification_key": notification_key, "user_id": user.get("id")},
            )
            _safe_set_rollback_false()
    for user_id, notification_id in push_targets:
        try:
            send_web_push_to_user(
                user_id,
                _push_notification_payload(
                    notification_key,
                    notification_id,
                    title,
                    body,
                    href,
                    severity_value,
                    entity_type,
                    entity_id,
                    key,
                    payload,
                ),
            )
        except Exception:
            logger.exception(
                "No se pudo disparar la notificación push",
                extra={"notification_key": notification_key, "user_id": user_id},
            )
            _safe_set_rollback_false()
    return inserted


def _ingreso_context(ingreso_id):
    mg_estado_sql = (
        "COALESCE(d.mg_estado,'activo') AS mg_estado,"
        if "mg_estado" in _table_columns("devices")
        else "'activo' AS mg_estado,"
    )
    return q(
        f"""
        SELECT c.razon_social AS cliente,
               COALESCE(m.tipo_equipo,'') AS tipo_equipo,
               COALESCE(b.nombre,'') AS marca,
               COALESCE(m.nombre,'') AS modelo,
               COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,''), '') AS equipo_variante,
               COALESCE(d.numero_serie,'') AS numero_serie,
               COALESCE(d.numero_interno,'') AS numero_interno,
               {mg_estado_sql}
               COALESCE(u.nombre,'') AS tecnico_nombre,
               t.asignado_a
          FROM ingresos t
          JOIN devices d ON d.id = t.device_id
          JOIN customers c ON c.id = d.customer_id
          LEFT JOIN marcas b ON b.id = d.marca_id
          LEFT JOIN models m ON m.id = d.model_id
          LEFT JOIN users u ON u.id = t.asignado_a
         WHERE t.id = %s
        """,
        [ingreso_id],
        one=True,
    ) or {}


def _equipo_label(row):
    modelo = " ".join([p for p in [row.get("modelo"), row.get("equipo_variante")] if p]).strip()
    return " | ".join([p for p in [row.get("tipo_equipo"), row.get("marca"), modelo] if p]) or "-"


def _ns_label(row):
    interno = (row.get("numero_interno") or "").strip()
    serie = (row.get("numero_serie") or "").strip()
    # Un MG vendido es histórico: el equipo ya se identifica por su N/S.
    if str(row.get("mg_estado") or "").strip().lower() == "inactivo_venta":
        return serie or "-"
    return interno or serie or "-"


def _ingreso_body(row):
    return "\n".join(
        [
            f"Cliente: {row.get('cliente') or '-'}",
            f"Equipo: {_equipo_label(row)}",
            f"N/S: {_ns_label(row)}",
        ]
    )


def notify_ingreso_liberado(ingreso_id):
    row = _ingreso_context(ingreso_id)
    os_txt = os_label(ingreso_id)
    target = row.get("asignado_a")
    return emit_notification(
        "ingreso_liberado",
        user_ids=[target] if target else None,
        title=f"Equipo liberado - OS {os_txt}",
        body=_ingreso_body(row),
        href=f"/ingresos/{ingreso_id}?tab=principal",
        severity="info",
        entity_type="ingreso",
        entity_id=ingreso_id,
        dedupe_key=f"ingreso:{ingreso_id}:liberado:{timezone.localdate().isoformat()}",
        payload={"ingreso_id": ingreso_id},
        push=True,
    )


def notify_ingreso_asignado(ingreso_id, tecnico_id):
    row = _ingreso_context(ingreso_id)
    os_txt = os_label(ingreso_id)
    return emit_notification(
        "ingreso_asignado",
        user_ids=[tecnico_id],
        title=f"Te asignaron la OS {os_txt}",
        body=_ingreso_body(row),
        href=f"/ingresos/{ingreso_id}?tab=principal",
        severity="info",
        entity_type="ingreso",
        entity_id=ingreso_id,
        dedupe_key=f"ingreso:{ingreso_id}:asignado:{tecnico_id}",
        payload={"ingreso_id": ingreso_id, "tecnico_id": tecnico_id},
        push=True,
    )


def notify_solicitud_asignacion(ingreso_id, tecnico_id, tecnico_nombre="", user_ids=None, emails=None):
    row = _ingreso_context(ingreso_id)
    os_txt = os_label(ingreso_id)
    actor = tecnico_nombre or row.get("tecnico_nombre") or "Un técnico"
    return emit_notification(
        "solicitud_asignacion",
        user_ids=user_ids,
        emails=emails,
        title=f"Solicitud de asignación - OS {os_txt}",
        body=f"{actor} solicita asignación.\n{_ingreso_body(row)}",
        href=f"/ingresos/{ingreso_id}?tab=principal&tecnico_id={tecnico_id}",
        severity="warning",
        entity_type="ingreso",
        entity_id=ingreso_id,
        dedupe_key=(
            f"ingreso:{ingreso_id}:solicitud_asignacion:{tecnico_id}"
            f":{timezone.localdate().isoformat()}"
        ),
        payload={"ingreso_id": ingreso_id, "tecnico_id": tecnico_id},
        push=True,
    )


def notify_presupuesto_aprobado(ingreso_id, tecnico_id):
    row = _ingreso_context(ingreso_id)
    os_txt = os_label(ingreso_id)
    return emit_notification(
        "presupuesto_aprobado",
        user_ids=[tecnico_id],
        title=f"Presupuesto aprobado - OS {os_txt}",
        body=_ingreso_body(row),
        href=f"/ingresos/{ingreso_id}?tab=presupuesto",
        severity="info",
        entity_type="ingreso",
        entity_id=ingreso_id,
        dedupe_key=f"ingreso:{ingreso_id}:presupuesto_aprobado:{timezone.localdate().isoformat()}",
        payload={"ingreso_id": ingreso_id, "tecnico_id": tecnico_id},
        push=True,
    )


def notify_reparacion_lista_remito(ingreso_id, user_ids=None, emails=None):
    row = _ingreso_context(ingreso_id)
    os_txt = os_label(ingreso_id)
    return emit_notification(
        "reparacion_lista_remito",
        user_ids=user_ids,
        emails=emails,
        title=f"Reparación lista para remito - OS {os_txt}",
        body=_ingreso_body(row),
        href=f"/ingresos/{ingreso_id}?tab=principal",
        severity="warning",
        entity_type="ingreso",
        entity_id=ingreso_id,
        dedupe_key=f"ingreso:{ingreso_id}:reparacion_lista_remito:{timezone.localdate().isoformat()}",
        payload={"ingreso_id": ingreso_id},
        push=True,
    )


def notify_presupuesto_rechazado_reparado(ingreso_id, tecnico_id=None):
    """El presupuesto se rechazó con la reparación ya hecha: hay que deshacerla.

    Es el único aviso del circuito que pide *desandar* trabajo, así que va al
    técnico asignado y no a Recepción: nadie más puede volver el equipo a como
    entró ni marcarlo "No se repara".
    """
    row = _ingreso_context(ingreso_id)
    target = tecnico_id or row.get("asignado_a")
    if not target:
        return 0
    os_txt = os_label(ingreso_id)
    body = "\n".join(
        [
            _ingreso_body(row),
            "",
            'Hay que deshacer la reparación y marcar el equipo como "No se repara".',
        ]
    )
    return emit_notification(
        "presupuesto_rechazado_reparado",
        user_ids=[target],
        title=f"Presupuesto rechazado con el equipo reparado - OS {os_txt}",
        body=body,
        href=f"/ingresos/{ingreso_id}?tab=principal",
        severity="warning",
        entity_type="ingreso",
        entity_id=ingreso_id,
        dedupe_key=f"ingreso:{ingreso_id}:presupuesto_rechazado_reparado",
        payload={"ingreso_id": ingreso_id, "tecnico_id": target},
        push=True,
    )


def notify_no_se_repara_listo_remito(ingreso_id, user_ids=None):
    row = _ingreso_context(ingreso_id)
    os_txt = os_label(ingreso_id)
    body = "\n".join(
        [
            _ingreso_body(row),
            "",
            "El equipo sale sin reparar: sólo falta imprimir la orden de salida.",
        ]
    )
    return emit_notification(
        "no_se_repara_listo_remito",
        user_ids=user_ids,
        title=f"No se repara - listo para remito - OS {os_txt}",
        body=body,
        href=f"/ingresos/{ingreso_id}?tab=principal",
        severity="warning",
        entity_type="ingreso",
        entity_id=ingreso_id,
        dedupe_key=f"ingreso:{ingreso_id}:no_se_repara_listo_remito",
        payload={"ingreso_id": ingreso_id},
        push=True,
    )


def notify_solicitud_baja(ingreso_id, solicitante_id=None, motivo="", user_ids=None, emails=None):
    row = _ingreso_context(ingreso_id)
    os_txt = os_label(ingreso_id)
    return emit_notification(
        "solicitud_baja",
        user_ids=user_ids,
        emails=emails,
        title=f"Solicitud de baja - OS {os_txt}",
        body=("\n".join([_ingreso_body(row), f"Motivo: {motivo or '-'}"])).strip(),
        href=f"/ingresos/{ingreso_id}?tab=principal",
        severity="critical",
        entity_type="ingreso",
        entity_id=ingreso_id,
        dedupe_key=f"ingreso:{ingreso_id}:solicitud_baja:{timezone.localdate().isoformat()}",
        payload={"ingreso_id": ingreso_id, "solicitante_id": solicitante_id, "motivo": motivo},
        push=True,
    )


CONFLICTO_ABIERTO_PANEL_PATH = "/bejerman?process_type=remito&source=ingreso_conflicto"


def _frontend_link(path):
    base = (
        str(getattr(settings, "PUBLIC_WEB_URL", "") or "").strip()
        or str(getattr(settings, "FRONTEND_ORIGIN", "") or "").strip()
    )
    return f"{base.rstrip('/')}{path}" if base else path


def notify_ingreso_conflicto_abierto(ingreso_id, conflicting_ingreso_id, user_ids=None, emails=None):
    """Avisa que un equipo volvió a entrar con su orden anterior todavía abierta.

    El remito ya salió y el cliente ya se fue con su comprobante: esto no es una alarma para
    frenar nada, es el pendiente administrativo de decidir qué pasa con la orden vieja.

    Manda campana **y** mail: `emit_notification` sólo crea la campana, el correo hay que
    encolarlo aparte (es el mismo reparto que usan los avisos de Cobranzas).
    """

    row = _ingreso_context(ingreso_id)
    os_txt = os_label(ingreso_id)
    os_previa = os_label(conflicting_ingreso_id)
    previa = q(
        """
        SELECT COALESCE(t.fecha_ingreso, t.fecha_creacion) AS fecha_ingreso,
               COALESCE(t.estado::text, '') AS estado
          FROM ingresos t
         WHERE t.id = %s
        """,
        [conflicting_ingreso_id],
        one=True,
    ) or {}
    fecha_previa = previa.get("fecha_ingreso")
    try:
        fecha_previa = fecha_previa.date().isoformat()
    except Exception:
        fecha_previa = str(fecha_previa or "-")

    detalle = [
        _ingreso_body(row),
        f"Orden anterior: OS {os_previa} (ingresada el {fecha_previa}, estado {previa.get('estado') or '-'})",
        f"Nueva orden: OS {os_txt}",
        "El remito de ingreso ya se emitió, con ese renglón sin número de OS.",
        "Falta resolver si la orden anterior se cierra (el equipo ya había salido) o si corresponde unificarlas.",
    ]
    title = f"Ingreso con orden anterior abierta - OS {os_txt}"
    dedupe_key = f"ingreso:{ingreso_id}:conflicto_abierto:{conflicting_ingreso_id}"

    # Sin destinatario explícito, la audiencia es jefatura: son quienes pueden decidir qué
    # pasa con la orden vieja. Hay que resolver los usuarios acá porque `emit_notification`
    # ignora `roles=` (sólo arma audiencia a partir de user_ids/emails).
    roles = notification_allowed_roles("ingreso_conflicto_abierto")
    if not user_ids and not emails:
        user_ids = [
            int(row["id"])
            for row in active_users_for_notification("ingreso_conflicto_abierto", roles=roles, channel="any")
        ]

    inserted = emit_notification(
        "ingreso_conflicto_abierto",
        user_ids=user_ids,
        emails=emails,
        title=title,
        body="\n".join(detalle),
        href=CONFLICTO_ABIERTO_PANEL_PATH,
        severity="warning",
        entity_type="ingreso",
        entity_id=ingreso_id,
        dedupe_key=dedupe_key,
        payload={"ingreso_id": ingreso_id, "conflicting_ingreso_id": conflicting_ingreso_id},
        push=True,
    )

    try:
        from .notification_email_outbox import queue_notification_email

        destinatarios = notification_email_recipients_for_users(
            active_users_for_notification(
                "ingreso_conflicto_abierto",
                user_ids=user_ids,
                emails=emails,
                roles=None if (user_ids or emails) else roles,
                channel="email",
                require_email=True,
            )
        )
        if destinatarios:
            cuerpo = "\n".join(
                [
                    "Un equipo volvió a entrar al taller con su orden de servicio anterior todavía abierta.",
                    "",
                    *detalle,
                    "",
                    f"Resolver en NEXORA: {_frontend_link(CONFLICTO_ABIERTO_PANEL_PATH)}",
                ]
            )
            footer = str(getattr(settings, "EMAIL_LEGAL_FOOTER", "") or "").strip()
            if footer:
                cuerpo = f"{cuerpo.rstrip()}\n\n{footer}"
            queue_notification_email(
                notification_key="ingreso_conflicto_abierto",
                dedupe_key=dedupe_key,
                recipients=destinatarios,
                subject=title,
                body=cuerpo,
            )
    except Exception:
        # El aviso es importante pero no es el remito: si el correo falla, la campana ya quedó.
        logger.exception("ingreso_conflicto_abierto_email_failed", extra={"ingreso_id": ingreso_id})

    return inserted


def notify_estado_patrimonial(ingreso_id, evento, emails=None):
    if evento == "baja":
        key, label = "baja_patrimonial", "Baja patrimonial"
    elif evento == "venta_mg":
        key, label = "venta_patrimonial", "Venta patrimonial"
    else:
        key, label = "alta_patrimonial", "Alta patrimonial"
    row = _ingreso_context(ingreso_id)
    os_txt = os_label(ingreso_id)
    return emit_notification(
        key,
        emails=emails,
        title=f"{label} - OS {os_txt}",
        body=_ingreso_body(row),
        href=f"/ingresos/{ingreso_id}?tab=principal",
        severity="critical" if evento in ("baja", "venta_mg") else "info",
        entity_type="ingreso",
        entity_id=ingreso_id,
        dedupe_key=f"ingreso:{ingreso_id}:{key}:{timezone.localdate().isoformat()}",
        payload={"ingreso_id": ingreso_id, "evento": evento},
        push=True,
    )


def notify_derivacion_devuelta(ingreso_id, tecnico_id=None):
    row = _ingreso_context(ingreso_id)
    target = tecnico_id or row.get("asignado_a")
    if not target:
        return 0
    os_txt = os_label(ingreso_id)
    return emit_notification(
        "derivacion_devuelta",
        user_ids=[target],
        title=f"Derivación devuelta - OS {os_txt}",
        body=_ingreso_body(row),
        href=f"/ingresos/{ingreso_id}?tab=derivaciones",
        severity="info",
        entity_type="ingreso",
        entity_id=ingreso_id,
        dedupe_key=f"ingreso:{ingreso_id}:derivacion_devuelta:{timezone.localdate().isoformat()}",
        payload={"ingreso_id": ingreso_id, "tecnico_id": target},
        push=True,
    )


def _notification_keys_visible_for_user(user_id):
    user = q(
        "SELECT id, rol FROM users WHERE id = %s AND COALESCE(activo, TRUE) = TRUE",
        [user_id],
        one=True,
    )
    if not user:
        return []
    role = _normalize_role(user.get("rol"))
    return [
        item["key"]
        for item in NOTIFICATION_CATALOG
        if _role_allowed_for_item(item, role)
    ]


def list_notifications_for_user(user_id, limit=DEFAULT_LIMIT):
    if not _table_exists("notifications"):
        return {"items": [], "unread_count": 0}
    try:
        limit_value = max(1, min(MAX_LIMIT, int(limit or DEFAULT_LIMIT)))
    except Exception:
        limit_value = DEFAULT_LIMIT
    visible_keys = _notification_keys_visible_for_user(user_id)
    if not visible_keys:
        return {"items": [], "unread_count": 0}
    bell_filter = "AND COALESCE(bell_enabled, TRUE) = TRUE" if _table_has_column("notifications", "bell_enabled") else ""
    unread = q(
        f"""
        SELECT COUNT(*) AS total
          FROM notifications
         WHERE user_id = %s
           AND notification_key = ANY(%s)
           AND read_at IS NULL
           {bell_filter}
        """,
        [user_id, visible_keys],
        one=True,
    ) or {}
    rows = q(
        f"""
        SELECT id, notification_key, title, body, href, severity, entity_type,
               entity_id, payload, created_at, read_at, clicked_at
          FROM notifications
         WHERE user_id = %s
           AND notification_key = ANY(%s)
           {bell_filter}
         ORDER BY (read_at IS NULL) DESC, created_at DESC, id DESC
         LIMIT %s
        """,
        [user_id, visible_keys, limit_value],
    ) or []
    return {"items": rows, "unread_count": int(unread.get("total") or 0)}


def mark_notification_clicked(user_id, notification_id):
    if not _table_exists("notifications"):
        return None
    row = q(
        """
        UPDATE notifications
           SET read_at = COALESCE(read_at, CURRENT_TIMESTAMP),
               clicked_at = COALESCE(clicked_at, CURRENT_TIMESTAMP),
               updated_at = CURRENT_TIMESTAMP
         WHERE id = %s
           AND user_id = %s
         RETURNING id, href
        """,
        [notification_id, user_id],
        one=True,
    )
    return row


def mark_all_notifications_read(user_id):
    if not _table_exists("notifications"):
        return 0
    bell_filter = "AND COALESCE(bell_enabled, TRUE) = TRUE" if _table_has_column("notifications", "bell_enabled") else ""
    row = q(
        f"""
        WITH updated AS (
            UPDATE notifications
               SET read_at = COALESCE(read_at, CURRENT_TIMESTAMP),
                   updated_at = CURRENT_TIMESTAMP
             WHERE user_id = %s
               AND read_at IS NULL
               {bell_filter}
             RETURNING id
        )
        SELECT COUNT(*) AS total FROM updated
        """,
        [user_id],
        one=True,
    ) or {}
    return int(row.get("total") or 0)


def resolve_notifications(notification_key, *, entity_type=None, entity_id=None, user_ids=None):
    """Cierra sola la campana de un hecho que ya se resolvió.

    Los avisos de "todavía falta hacer X" nacían sin leer y ahí se quedaban
    aunque X se hiciera treinta segundos después: nadie marcaba la fila, así
    que la campana acumulaba pendientes de trabajo ya terminado. Se llama desde
    el punto donde el hecho deja de estar abierto (imprimir la orden de salida,
    por ejemplo) y devuelve cuántas filas quedaron marcadas leídas.
    """
    if not _table_exists("notifications"):
        return 0
    where = ["notification_key = %s", "read_at IS NULL"]
    params = [notification_key]
    if entity_type is not None:
        where.append("entity_type = %s")
        params.append(str(entity_type))
    if entity_id is not None:
        where.append("entity_id = %s")
        params.append(str(entity_id))
    ids = [int(uid) for uid in (user_ids or []) if uid is not None]
    if ids:
        where.append("user_id = ANY(%s)")
        params.append(ids)
    try:
        row = q(
            f"""
            WITH updated AS (
                UPDATE notifications
                   SET read_at = COALESCE(read_at, CURRENT_TIMESTAMP),
                       updated_at = CURRENT_TIMESTAMP
                 WHERE {" AND ".join(where)}
                 RETURNING id
            )
            SELECT COUNT(*) AS total FROM updated
            """,
            params,
            one=True,
        ) or {}
    except Exception:
        logger.exception(
            "No se pudieron cerrar las notificaciones resueltas",
            extra={"notification_key": notification_key, "entity_id": entity_id},
        )
        _safe_set_rollback_false()
        return 0
    return int(row.get("total") or 0)


def _load_user_overrides_by_key(user_id):
    if not _table_exists("notification_user_preferences"):
        return {}
    columns = _table_columns("notification_user_preferences")
    select_parts = ["notification_key"]
    select_parts.append("enabled" if "enabled" in columns else "NULL AS enabled")
    select_parts.append("bell_enabled" if "bell_enabled" in columns else "NULL AS bell_enabled")
    select_parts.append("email_enabled" if "email_enabled" in columns else "NULL AS email_enabled")
    select_parts.append("push_enabled" if "push_enabled" in columns else "NULL AS push_enabled")
    rows = q(
        f"""
        SELECT {', '.join(select_parts)}
          FROM notification_user_preferences
         WHERE user_id = %s
        """,
        [user_id],
    ) or []
    overrides = {}
    for row in rows:
        legacy = _as_bool(row.get("enabled"))
        bell = _as_bool(row.get("bell_enabled"))
        overrides[row.get("notification_key")] = {
            "legacy": legacy,
            "bell": legacy if bell is None else bell,
            "email": _as_bool(row.get("email_enabled")),
            "push": _as_bool(row.get("push_enabled")),
        }
    return overrides


def _notification_settings_items_for_user(user):
    overrides = _load_user_overrides_by_key(user.get("id"))
    items = []
    for item in NOTIFICATION_CATALOG:
        key = item["key"]
        if not _role_allowed_for_item(item, user.get("rol")):
            continue
        allowed_channels = _item_channels(item)
        override = overrides.get(key) or {}
        default_enabled = _role_default_enabled(key, user.get("rol"))
        override_channels = {channel: override.get(channel) for channel in NOTIFICATION_CHANNELS}
        effective_channels = {
            channel: _effective_enabled(user, key, {int(user["id"]): override}, channel)
            for channel in NOTIFICATION_CHANNELS
        }
        primary_channel = allowed_channels[0] if allowed_channels else "bell"
        items.append(
            {
                **item,
                "default_enabled": bool(default_enabled),
                "override_enabled": override_channels.get(primary_channel),
                "effective_enabled": bool(effective_channels.get(primary_channel)),
                "channels": list(allowed_channels),
                "allowed_channels": list(allowed_channels),
                "default_channels": {
                    channel: bool(default_enabled and channel in allowed_channels)
                    for channel in NOTIFICATION_CHANNELS
                },
                "override_channels": override_channels,
                "effective_channels": {channel: bool(value) for channel, value in effective_channels.items()},
            }
        )
    return items


def list_notification_email_addresses(user_id):
    if not notification_email_schema_ready():
        return []
    return q(
        """
        SELECT id, email, label, active, created_at, updated_at
          FROM notification_email_addresses
         WHERE user_id = %s
           AND COALESCE(active, TRUE) = TRUE
         ORDER BY LOWER(TRIM(email)), id
        """,
        [user_id],
    ) or []


def get_user_notification_settings(user_id):
    user = q(
        "SELECT id, nombre, email, rol, activo FROM users WHERE id = %s",
        [user_id],
        one=True,
    )
    if not user:
        return None
    return {"user": user, "items": _notification_settings_items_for_user(user)}


def get_current_user_notification_configuration(user_id):
    data = get_user_notification_settings(user_id)
    if not data:
        return None
    user = data["user"]
    return {
        **data,
        "capabilities": {
            "canManageExtraEmails": _can_manage_extra_emails_for_role(user.get("rol")),
        },
        "primary_email": str(user.get("email") or "").strip(),
        "extra_emails": list_notification_email_addresses(user_id),
        "push": get_push_config_for_user(user_id),
        "mandatory": {},
    }


def _payload_channel_values(raw_value):
    if not isinstance(raw_value, dict):
        return {"bell": _as_bool(raw_value)}, True
    aliases = {
        "bell": "bell",
        "campana": "bell",
        "enabled": "bell",
        "bell_enabled": "bell",
        "email": "email",
        "mail": "email",
        "email_enabled": "email",
        "push": "push",
        "phone": "push",
        "telefono": "push",
        "teléfono": "push",
        "push_enabled": "push",
    }
    values = {}
    for raw_key, value in raw_value.items():
        channel = aliases.get(str(raw_key or "").strip().lower())
        if channel:
            values[channel] = _as_bool(value)
    if not values:
        raise ValueError("Cada preferencia debe incluir al menos un canal válido.")
    return values, False


def _delete_user_notification_preference(user_id, notification_key):
    with connection.cursor() as cur:
        cur.execute(
            """
            DELETE FROM notification_user_preferences
             WHERE user_id = %s
               AND notification_key = %s
            """,
            [user_id, notification_key],
        )


def _upsert_user_notification_preference(user_id, notification_key, values, updated_by=None):
    columns = _table_columns("notification_user_preferences")
    bell = values.get("bell")
    if {"bell_enabled", "email_enabled", "push_enabled"}.issubset(columns):
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO notification_user_preferences(
                  user_id, notification_key, enabled, bell_enabled, email_enabled, push_enabled, updated_by
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (user_id, notification_key) DO UPDATE SET
                  enabled = EXCLUDED.enabled,
                  bell_enabled = EXCLUDED.bell_enabled,
                  email_enabled = EXCLUDED.email_enabled,
                  push_enabled = EXCLUDED.push_enabled,
                  updated_by = EXCLUDED.updated_by,
                  updated_at = CURRENT_TIMESTAMP
                """,
                [user_id, notification_key, bell, bell, values.get("email"), values.get("push"), updated_by],
            )
        return
    with connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO notification_user_preferences(
              user_id, notification_key, enabled, updated_by
            ) VALUES (%s,%s,%s,%s)
            ON CONFLICT (user_id, notification_key) DO UPDATE SET
              enabled = EXCLUDED.enabled,
              updated_by = EXCLUDED.updated_by,
              updated_at = CURRENT_TIMESTAMP
            """,
            [user_id, notification_key, bell, updated_by],
        )


def save_user_notification_settings(user_id, preferences, updated_by=None):
    if not notifications_schema_ready():
        raise RuntimeError("El esquema de notificaciones no está aplicado.")
    if not isinstance(preferences, dict):
        raise ValueError("preferences debe ser un objeto.")
    user = q(
        "SELECT id, rol FROM users WHERE id = %s",
        [user_id],
        one=True,
    )
    if not user:
        raise ValueError("Usuario inválido.")
    valid_keys = set(CATALOG_BY_KEY)
    with transaction.atomic():
        for key, raw_value in preferences.items():
            clean_key = (key or "").strip()
            if clean_key not in valid_keys:
                raise ValueError(f"Tipo de notificación inválido: {clean_key}")
            item = CATALOG_BY_KEY[clean_key]
            if not _role_allowed_for_item(item, user.get("rol")):
                raise ValueError(f"Tu rol no puede configurar esta notificación: {clean_key}")
            incoming_values, legacy_payload = _payload_channel_values(raw_value)
            if legacy_payload and incoming_values.get("bell") is None:
                _delete_user_notification_preference(user_id, clean_key)
                continue
            allowed_channels = set(_item_channels(item))
            invalid_enabled_channels = sorted(
                channel
                for channel, value in incoming_values.items()
                if channel not in allowed_channels and value
            )
            if invalid_enabled_channels:
                raise ValueError(
                    f"Canal no permitido para {clean_key}: {', '.join(invalid_enabled_channels)}"
                )
            incoming_values = {
                channel: value
                for channel, value in incoming_values.items()
                if channel in allowed_channels
            }
            if not incoming_values:
                continue
            current = (_load_overrides(clean_key, [user_id]).get(int(user_id)) or {})
            next_values = {channel: current.get(channel) for channel in NOTIFICATION_CHANNELS}
            for channel, value in incoming_values.items():
                next_values[channel] = value
            if all(next_values.get(channel) is None for channel in NOTIFICATION_CHANNELS):
                _delete_user_notification_preference(user_id, clean_key)
                continue
            _upsert_user_notification_preference(user_id, clean_key, next_values, updated_by=updated_by)
    return get_user_notification_settings(user_id)


def add_notification_email_address(user_id, email, label="", updated_by=None):
    if not notification_email_schema_ready():
        raise RuntimeError("El esquema de emails de notificaciones no está aplicado.")
    clean_email = _clean_email(email)
    clean_label = str(label or "").strip()[:120]
    existing = q(
        """
        SELECT id
          FROM notification_email_addresses
         WHERE user_id = %s
           AND LOWER(TRIM(email)) = LOWER(TRIM(%s))
         LIMIT 1
        """,
        [user_id, clean_email],
        one=True,
    )
    with connection.cursor() as cur:
        if existing:
            cur.execute(
                """
                UPDATE notification_email_addresses
                   SET email = %s,
                       label = %s,
                       active = TRUE,
                       updated_by = %s,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = %s
                   AND user_id = %s
                RETURNING id, email, label, active, created_at, updated_at
                """,
                [clean_email, clean_label, updated_by, existing["id"], user_id],
            )
        else:
            cur.execute(
                """
                INSERT INTO notification_email_addresses(user_id, email, label, active, updated_by)
                VALUES (%s,%s,%s,TRUE,%s)
                RETURNING id, email, label, active, created_at, updated_at
                """,
                [user_id, clean_email, clean_label, updated_by],
            )
        cols = [col[0] for col in cur.description]
        return dict(zip(cols, cur.fetchone()))


def update_notification_email_address(user_id, email_id, payload, updated_by=None):
    if not notification_email_schema_ready():
        raise RuntimeError("El esquema de emails de notificaciones no está aplicado.")
    current = q(
        """
        SELECT id, email, label, active
          FROM notification_email_addresses
         WHERE id = %s
           AND user_id = %s
        """,
        [email_id, user_id],
        one=True,
    )
    if not current:
        return None
    data = payload if isinstance(payload, dict) else {}
    next_email = _clean_email(data.get("email")) if "email" in data else current["email"]
    next_label = str(data.get("label", current.get("label") or "") or "").strip()[:120]
    next_active = _as_bool(data.get("active")) if "active" in data else bool(current.get("active"))
    duplicate = q(
        """
        SELECT id
          FROM notification_email_addresses
         WHERE user_id = %s
           AND id <> %s
           AND LOWER(TRIM(email)) = LOWER(TRIM(%s))
         LIMIT 1
        """,
        [user_id, email_id, next_email],
        one=True,
    )
    if duplicate:
        raise ValueError("Ese email ya está cargado.")
    with connection.cursor() as cur:
        cur.execute(
            """
            UPDATE notification_email_addresses
               SET email = %s,
                   label = %s,
                   active = %s,
                   updated_by = %s,
                   updated_at = CURRENT_TIMESTAMP
             WHERE id = %s
               AND user_id = %s
            RETURNING id, email, label, active, created_at, updated_at
            """,
            [next_email, next_label, bool(next_active), updated_by, email_id, user_id],
        )
        row = cur.fetchone()
        cols = [col[0] for col in cur.description]
    return dict(zip(cols, row))


def delete_notification_email_address(user_id, email_id):
    if not notification_email_schema_ready():
        return 0
    with connection.cursor() as cur:
        cur.execute(
            """
            DELETE FROM notification_email_addresses
             WHERE id = %s
               AND user_id = %s
            """,
            [email_id, user_id],
        )
        return int(cur.rowcount or 0)
