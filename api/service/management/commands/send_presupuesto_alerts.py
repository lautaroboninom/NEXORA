import logging
from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.mail import EmailMessage, get_connection, send_mail
from django.utils import timezone

from service.views.helpers import _email_append_footer_text, exec_void, os_label, q
from service.notifications import emit_notification

logger = logging.getLogger(__name__)


def _frontend_link(path: str) -> str:
    base = (getattr(settings, "PUBLIC_WEB_URL", "") or getattr(settings, "FRONTEND_ORIGIN", "")).strip()
    if base:
        return f"{base.rstrip('/')}{path}"
    return path


def _equipolabel_row(r):
    try:
        tipo = (r.get("tipo_equipo") or "").strip()
        marca = (r.get("marca") or "").strip()
        modelo = (r.get("modelo") or "").strip()
        variante = (r.get("equipo_variante") or "").strip()
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
        logger.warning("presupuesto_alerts no recipients configured")
        return False, debug
    try:
        sent = send_mail(
            subject,
            body,
            getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipients,
            fail_silently=False,
        )
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
                msg = EmailMessage(
                    subject,
                    body,
                    getattr(settings, "DEFAULT_FROM_EMAIL", None),
                    recipients,
                    connection=conn,
                )
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


class Command(BaseCommand):
    help = "Envia alertas de presupuestos pendientes (rol jefe)"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="No envia emails; solo lista candidatos.")
        parser.add_argument("--limit", type=int, default=0, help="Limitar cantidad de avisos.")

    def handle(self, *args, **opts):
        if not getattr(settings, "PRESUPUESTO_ALERT_ENABLED", True):
            self.stdout.write("PRESUPUESTO_ALERT_ENABLED=0; skip")
            return

        first_days = int(getattr(settings, "PRESUPUESTO_ALERT_FIRST_DAYS", 7) or 7)
        repeat_days = int(getattr(settings, "PRESUPUESTO_ALERT_REPEAT_DAYS", 3) or 3)
        location = (getattr(settings, "PRESUPUESTO_ALERT_LOCATION", "taller") or "taller").strip()
        if first_days < 1:
            first_days = 7
        if repeat_days < 1:
            repeat_days = 3

        try:
            q("SELECT 1 FROM ingreso_presupuesto_alerts LIMIT 1")
        except Exception:
            self.stderr.write("Tabla ingreso_presupuesto_alerts no existe. Ejecuta apply_presupuesto_alerts_schema.")
            return

        recips = q(
            """
            SELECT DISTINCT LOWER(email) AS email
              FROM users
             WHERE activo=true
               AND rol='jefe'
               AND COALESCE(email,'') <> ''
            """
        ) or []
        recipients = [r.get("email") for r in recips if r.get("email")]
        if not recipients:
            self.stdout.write("No hay destinatarios (rol jefe).")
            return

        rows = q(
            """
            WITH base AS (
              SELECT
                t.id,
                c.razon_social AS cliente,
                COALESCE(b.nombre,'') AS marca,
                COALESCE(m.nombre,'') AS modelo,
                COALESCE(m.tipo_equipo,'') AS tipo_equipo,
                COALESCE(NULLIF(t.equipo_variante,''), NULLIF(d.variante,''), NULLIF(m.variante,'')) AS equipo_variante,
                COALESCE(d.numero_interno,'') AS numero_interno,
                COALESCE(d.numero_serie,'') AS numero_serie,
                q.fecha_emitido AS presupuesto_fecha_emision,
                CASE
                  WHEN ipa.last_sent_at IS NOT NULL AND ipa.last_sent_at >= q.fecha_emitido THEN ipa.last_sent_at
                  ELSE NULL
                END AS last_sent_at
              FROM ingresos t
              JOIN devices d ON d.id = t.device_id
              JOIN customers c ON c.id = d.customer_id
              LEFT JOIN marcas b ON b.id = d.marca_id
              LEFT JOIN models m ON m.id = d.model_id
              LEFT JOIN quotes q ON q.id = (
                SELECT q2.id FROM quotes q2
                WHERE q2.ingreso_id = t.id
                ORDER BY COALESCE(q2.version_num, 1) DESC, q2.id DESC
                LIMIT 1
              )
              LEFT JOIN ingreso_presupuesto_alerts ipa ON ipa.ingreso_id = t.id
              LEFT JOIN locations loc ON loc.id = t.ubicacion_id
              WHERE q.fecha_emitido IS NOT NULL
                AND (
                      q.estado::text IN ('emitido','enviado','presupuestado')
                      OR t.presupuesto_estado = 'presupuestado'
                    )
                AND LOWER(loc.nombre) = LOWER(%s)
                AND t.estado NOT IN ('entregado','liberado','alquilado','baja','vendido_pendiente_entrega','vendido_entregado')
            )
            SELECT *,
                   FLOOR(EXTRACT(EPOCH FROM (now() - presupuesto_fecha_emision)) / 86400) AS dias_desde_emitido
              FROM base
             WHERE now() - presupuesto_fecha_emision >= (%s * interval '1 day')
               AND (last_sent_at IS NULL OR now() - last_sent_at >= (%s * interval '1 day'))
             ORDER BY presupuesto_fecha_emision ASC, id ASC
            """,
            [location, first_days, repeat_days],
        ) or []

        limit = int(opts.get("limit") or 0)
        if limit > 0:
            rows = rows[:limit]

        if not rows:
            self.stdout.write("No hay presupuestos para alertar.")
            return

        dry_run = bool(opts.get("dry_run"))
        sent_count = 0
        for r in rows:
            ingreso_id = r.get("id")
            os_txt = f"OS {os_label(ingreso_id)}"
            cliente = r.get("cliente") or "-"
            equipo = _equipolabel_row(r)
            ns_val = _ns_label(r)
            fecha = r.get("presupuesto_fecha_emision")
            if fecha is not None:
                try:
                    fecha_txt = fecha.astimezone(timezone.get_current_timezone()).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    try:
                        fecha_txt = fecha.strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        fecha_txt = str(fecha)
            else:
                fecha_txt = "-"
            dias = int(r.get("dias_desde_emitido") or 0)

            link = _frontend_link(f"/ingresos/{ingreso_id}")
            subject = f"Presupuesto pendiente de aprobacion - {os_txt}"
            lines = [
                f"Aviso automatico: el presupuesto de {os_txt} lleva {dias} dias sin aprobacion.",
                "",
                "Detalle del equipo:",
                f"- Cliente: {cliente}",
                f"- Equipo: {equipo}",
                f"- N/S: {ns_val}",
                f"- Fecha presupuesto: {fecha_txt}",
            ]
            if link:
                lines += ["", f"Abrir hoja de servicio: {link}"]
            lines += ["", "Aviso automatico - no responder a este correo."]
            body = _email_append_footer_text("\n".join(lines))

            if dry_run:
                self.stdout.write(f"[DRY] {os_txt} -> {', '.join(recipients)}")
                continue

            try:
                emit_notification(
                    "presupuesto_pendiente",
                    emails=recipients,
                    title=f"Presupuesto pendiente - {os_txt}",
                    body="\n".join(
                        [
                            f"El presupuesto lleva {dias} días sin aprobación.",
                            f"Cliente: {cliente}",
                            f"Equipo: {equipo}",
                            f"N/S: {ns_val}",
                        ]
                    ),
                    href=f"/ingresos/{ingreso_id}?tab=presupuesto",
                    severity="critical",
                    entity_type="ingreso",
                    entity_id=ingreso_id,
                    dedupe_key=f"ingreso:{ingreso_id}:presupuesto_pendiente:{timezone.localdate().isoformat()}",
                    payload={"ingreso_id": ingreso_id, "dias": dias},
                )
            except Exception:
                pass

            ok, debug = _send_mail_with_fallback(subject, body, recipients)
            if ok:
                try:
                    exec_void(
                        """
                        INSERT INTO ingreso_presupuesto_alerts (ingreso_id, last_sent_at, created_at, updated_at)
                        VALUES (%s, NOW(), NOW(), NOW())
                        ON CONFLICT (ingreso_id) DO UPDATE
                           SET last_sent_at = EXCLUDED.last_sent_at,
                               updated_at = EXCLUDED.updated_at
                        """,
                        [ingreso_id],
                    )
                    sent_count += 1
                except Exception:
                    logger.exception("presupuesto_alerts failed to update last_sent_at", extra={"ingreso_id": ingreso_id})
            else:
                logger.warning(
                    "presupuesto_alerts failed",
                    extra={"ingreso_id": ingreso_id, "recipients": recipients, "debug": debug},
                )

        if dry_run:
            self.stdout.write(f"DRY-RUN finalizado. Candidatos: {len(rows)}")
        else:
            self.stdout.write(f"Alertas enviadas: {sent_count} de {len(rows)}")
