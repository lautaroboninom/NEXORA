import json
import logging
from collections import OrderedDict

from django.db import connection, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


NOTIFICATION_CATALOG = [
    {
        "key": "ingreso_liberado",
        "label": "Liberado",
        "description": "Equipo liberado con orden de salida impresa.",
        "group": "Ingresos",
        "default_roles": [],
    },
    {
        "key": "ingreso_asignado",
        "label": "Asignación técnica",
        "description": "Ingreso asignado o reasignado a un técnico.",
        "group": "Ingresos",
        "default_roles": ["tecnico"],
    },
    {
        "key": "solicitud_asignacion",
        "label": "Solicitud de asignación",
        "description": "Un técnico solicita que se le asigne un ingreso.",
        "group": "Ingresos",
        "default_roles": ["jefe"],
    },
    {
        "key": "presupuesto_aprobado",
        "label": "Presupuesto aprobado",
        "description": "Presupuesto aprobado para un ingreso asignado.",
        "group": "Presupuestos",
        "default_roles": ["tecnico"],
    },
    {
        "key": "reparacion_lista_remito",
        "label": "Reparación lista para remito",
        "description": "Equipo reparado que ya puede liberarse con orden de salida.",
        "group": "Ingresos",
        "default_roles": ["jefe"],
    },
    {
        "key": "solicitud_baja",
        "label": "Solicitud de baja",
        "description": "Solicitud pendiente para dar de baja un ingreso.",
        "group": "Ingresos",
        "default_roles": ["jefe"],
    },
    {
        "key": "baja_patrimonial",
        "label": "Baja patrimonial",
        "description": "Equipo dado de baja para reflejar en gestión patrimonial.",
        "group": "Patrimonio",
        "default_roles": ["jefe"],
    },
    {
        "key": "alta_patrimonial",
        "label": "Alta patrimonial",
        "description": "Equipo dado de alta para reflejar en gestión patrimonial.",
        "group": "Patrimonio",
        "default_roles": ["jefe"],
    },
    {
        "key": "derivacion_devuelta",
        "label": "Derivación devuelta",
        "description": "Equipo devuelto desde un proveedor externo.",
        "group": "Derivaciones",
        "default_roles": ["tecnico"],
    },
    {
        "key": "preventivo_vencido",
        "label": "Preventivo vencido",
        "description": "Mantenimiento preventivo vencido.",
        "group": "Preventivos",
        "default_roles": ["jefe"],
    },
    {
        "key": "preventivo_proximo",
        "label": "Preventivo próximo",
        "description": "Mantenimiento preventivo próximo a vencer.",
        "group": "Preventivos",
        "default_roles": ["jefe"],
    },
    {
        "key": "stock_minimo",
        "label": "Stock mínimo",
        "description": "Repuesto que llegó al stock mínimo.",
        "group": "Repuestos",
        "default_roles": ["jefe", "jefe_veedor"],
    },
    {
        "key": "presupuesto_pendiente",
        "label": "Presupuesto pendiente",
        "description": "Presupuesto emitido con aprobación demorada.",
        "group": "Presupuestos",
        "default_roles": ["jefe"],
    },
    {
        "key": "presupuesto_decision_portal",
        "label": "Decision de presupuesto desde Portal",
        "description": "Un cliente aprobo o rechazo un presupuesto desde el Portal.",
        "group": "Presupuestos",
        "default_roles": ["jefe"],
    },
    {
        "key": "resumen_operativo",
        "label": "Resumen operativo",
        "description": "Resumen de alertas y objetivos del servicio técnico.",
        "group": "Operación",
        "default_roles": ["jefe", "jefe_veedor", "admin"],
    },
]

CATALOG_BY_KEY = {item["key"]: item for item in NOTIFICATION_CATALOG}
DEFAULT_LIMIT = 20
MAX_LIMIT = 50


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


def notifications_schema_ready():
    return _table_exists("notifications") and _table_exists("notification_user_preferences")


def _as_bool(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


def _normalize_role(role):
    return (role or "").strip().lower()


def _role_default_enabled(notification_key, role):
    item = CATALOG_BY_KEY.get(notification_key) or {}
    role_key = _normalize_role(role)
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
    rows = q(
        """
        SELECT user_id, enabled
          FROM notification_user_preferences
         WHERE notification_key = %s
           AND user_id = ANY(%s)
        """,
        [notification_key, ids],
    ) or []
    return {int(row["user_id"]): _as_bool(row.get("enabled")) for row in rows}


def _effective_enabled(user_row, notification_key, overrides):
    uid = int(user_row.get("id"))
    if uid in overrides and overrides[uid] is not None:
        return bool(overrides[uid])
    return _role_default_enabled(notification_key, user_row.get("rol"))


def _candidate_users(notification_key, user_ids=None, roles=None, emails=None):
    users = OrderedDict()
    if user_ids:
        for row in _load_active_users(user_ids=user_ids):
            users[int(row["id"])] = row
    if emails:
        for row in _load_active_users(emails=emails):
            users[int(row["id"])] = row
    return list(users.values())


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
):
    if notification_key not in CATALOG_BY_KEY:
        return 0
    if not notifications_schema_ready():
        return 0

    # "roles" queda solo por compatibilidad de firma: ya no crea audiencia interna.
    candidates = _candidate_users(notification_key, user_ids=user_ids, roles=roles, emails=emails)
    if not candidates:
        return 0
    overrides = _load_overrides(notification_key, [row.get("id") for row in candidates])
    recipients = [row for row in candidates if _effective_enabled(row, notification_key, overrides)]
    if not recipients:
        return 0

    key = (dedupe_key or f"{notification_key}:{entity_type or 'general'}:{entity_id or title}").strip()
    severity_value = severity if severity in {"info", "warning", "critical"} else "info"
    payload_text = json.dumps(payload or {}, ensure_ascii=False, default=str)
    inserted = 0
    for user in recipients:
        try:
            with connection.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO notifications(
                      user_id, notification_key, dedupe_key, title, body, href,
                      severity, entity_type, entity_id, payload
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (user_id, notification_key, dedupe_key) DO NOTHING
                    """,
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
                inserted += int(cur.rowcount or 0)
        except Exception:
            logger.exception(
                "No se pudo crear la notificación interna",
                extra={"notification_key": notification_key, "user_id": user.get("id")},
            )
            _safe_set_rollback_false()
    return inserted


def _ingreso_context(ingreso_id):
    return q(
        """
        SELECT c.razon_social AS cliente,
               COALESCE(m.tipo_equipo,'') AS tipo_equipo,
               COALESCE(b.nombre,'') AS marca,
               COALESCE(m.nombre,'') AS modelo,
               COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,''), '') AS equipo_variante,
               COALESCE(d.numero_serie,'') AS numero_serie,
               COALESCE(d.numero_interno,'') AS numero_interno,
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
    return (row.get("numero_interno") or "").strip() or (row.get("numero_serie") or "").strip() or "-"


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
        dedupe_key=f"ingreso:{ingreso_id}:liberado",
        payload={"ingreso_id": ingreso_id},
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
        dedupe_key=f"ingreso:{ingreso_id}:solicitud_asignacion:{tecnico_id}",
        payload={"ingreso_id": ingreso_id, "tecnico_id": tecnico_id},
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
        dedupe_key=f"ingreso:{ingreso_id}:presupuesto_aprobado",
        payload={"ingreso_id": ingreso_id, "tecnico_id": tecnico_id},
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
        dedupe_key=f"ingreso:{ingreso_id}:reparacion_lista_remito",
        payload={"ingreso_id": ingreso_id},
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
        dedupe_key=f"ingreso:{ingreso_id}:solicitud_baja",
        payload={"ingreso_id": ingreso_id, "solicitante_id": solicitante_id, "motivo": motivo},
    )


def notify_estado_patrimonial(ingreso_id, evento, emails=None):
    key = "baja_patrimonial" if evento == "baja" else "alta_patrimonial"
    label = "Baja patrimonial" if evento == "baja" else "Alta patrimonial"
    row = _ingreso_context(ingreso_id)
    os_txt = os_label(ingreso_id)
    return emit_notification(
        key,
        emails=emails,
        title=f"{label} - OS {os_txt}",
        body=_ingreso_body(row),
        href=f"/ingresos/{ingreso_id}?tab=principal",
        severity="critical" if evento == "baja" else "info",
        entity_type="ingreso",
        entity_id=ingreso_id,
        dedupe_key=f"ingreso:{ingreso_id}:{key}:{timezone.localdate().isoformat()}",
        payload={"ingreso_id": ingreso_id, "evento": evento},
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
    )


def list_notifications_for_user(user_id, limit=DEFAULT_LIMIT):
    if not _table_exists("notifications"):
        return {"items": [], "unread_count": 0}
    try:
        limit_value = max(1, min(MAX_LIMIT, int(limit or DEFAULT_LIMIT)))
    except Exception:
        limit_value = DEFAULT_LIMIT
    unread = q(
        """
        SELECT COUNT(*) AS total
          FROM notifications
         WHERE user_id = %s
           AND read_at IS NULL
        """,
        [user_id],
        one=True,
    ) or {}
    rows = q(
        """
        SELECT id, notification_key, title, body, href, severity, entity_type,
               entity_id, payload, created_at, read_at, clicked_at
          FROM notifications
         WHERE user_id = %s
           AND read_at IS NULL
         ORDER BY created_at DESC, id DESC
         LIMIT %s
        """,
        [user_id, limit_value],
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


def get_user_notification_settings(user_id):
    user = q(
        "SELECT id, nombre, email, rol, activo FROM users WHERE id = %s",
        [user_id],
        one=True,
    )
    if not user:
        return None
    overrides = {}
    if _table_exists("notification_user_preferences"):
        rows = q(
            """
            SELECT notification_key, enabled
              FROM notification_user_preferences
             WHERE user_id = %s
            """,
            [user_id],
        ) or []
        overrides = {row.get("notification_key"): _as_bool(row.get("enabled")) for row in rows}
    items = []
    for item in NOTIFICATION_CATALOG:
        key = item["key"]
        override = overrides.get(key)
        default_enabled = _role_default_enabled(key, user.get("rol"))
        effective = override if override is not None else default_enabled
        items.append(
            {
                **item,
                "default_enabled": bool(default_enabled),
                "override_enabled": override,
                "effective_enabled": bool(effective),
            }
        )
    return {"user": user, "items": items}


def save_user_notification_settings(user_id, preferences, updated_by=None):
    if not notifications_schema_ready():
        raise RuntimeError("El esquema de notificaciones no está aplicado.")
    if not isinstance(preferences, dict):
        raise ValueError("preferences debe ser un objeto.")
    valid_keys = set(CATALOG_BY_KEY)
    with transaction.atomic():
        for key, raw_value in preferences.items():
            clean_key = (key or "").strip()
            if clean_key not in valid_keys:
                raise ValueError(f"Tipo de notificación inválido: {clean_key}")
            value = _as_bool(raw_value)
            if value is None:
                with connection.cursor() as cur:
                    cur.execute(
                        """
                        DELETE FROM notification_user_preferences
                         WHERE user_id = %s
                           AND notification_key = %s
                        """,
                        [user_id, clean_key],
                    )
                continue
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
                    [user_id, clean_key, value, updated_by],
                )
    return get_user_notification_settings(user_id)
