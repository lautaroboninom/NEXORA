import logging
from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.mail import send_mail
from django.utils import timezone

from service.views.helpers import _email_append_footer_text, q
from service.notifications import emit_notification

logger = logging.getLogger(__name__)


def _frontend_link(path: str) -> str:
    base = (getattr(settings, "PUBLIC_WEB_URL", "") or getattr(settings, "FRONTEND_ORIGIN", "")).strip()
    if base:
        return f"{base.rstrip('/')}{path}"
    return path


def _send(subject, body, recipients):
    if not recipients:
        return False
    try:
        sent = send_mail(
            subject,
            body,
            getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipients,
            fail_silently=False,
        )
        return bool(sent and sent > 0)
    except Exception:
        logger.exception("preventivo_alerts send failed")
        return False


class Command(BaseCommand):
    help = "Envia resumen diario de mantenimientos preventivos proximos/vencidos (rol jefe)"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="No envia emails; solo muestra conteos.")
        parser.add_argument("--limit", type=int, default=0, help="Limitar items en el resumen.")

    def handle(self, *args, **opts):
        if not getattr(settings, "PREVENTIVO_ALERT_ENABLED", True):
            self.stdout.write("PREVENTIVO_ALERT_ENABLED=0; skip")
            return

        try:
            q("SELECT 1 FROM preventivo_planes LIMIT 1")
        except Exception:
            self.stderr.write("Tabla preventivo_planes no existe. Ejecuta apply_preventivos_schema.")
            return

        recipients = [
            r.get("email")
            for r in (
                q(
                    """
                    SELECT DISTINCT LOWER(email) AS email
                      FROM users
                     WHERE activo=true
                       AND rol='jefe'
                       AND COALESCE(email,'') <> ''
                    """
                )
                or []
            )
            if r.get("email")
        ]
        if not recipients:
            self.stdout.write("No hay destinatarios activos con rol jefe.")
            return

        has_repuestos = True
        try:
            q("SELECT 1 FROM preventivo_plan_repuestos LIMIT 1")
        except Exception:
            has_repuestos = False

        if has_repuestos:
            rows = q(
                """
                WITH plans AS (
                  SELECT
                    p.id AS plan_id,
                    p.scope_type::text AS scope_type,
                    p.device_id,
                    p.customer_id,
                    p.periodicidad_valor,
                    p.periodicidad_unidad::text AS periodicidad_unidad,
                    COALESCE(prn.repuesto_aviso_anticipacion_dias, p.aviso_anticipacion_dias) AS aviso_anticipacion_dias,
                    COALESCE(prn.repuesto_ultima_revision_fecha, p.ultima_revision_fecha) AS ultima_revision_fecha,
                    COALESCE(prn.repuesto_proxima_revision_fecha, p.proxima_revision_fecha) AS proxima_revision_fecha,
                    COALESCE(prn.repuesto_nombre, '') AS repuesto_proximo_nombre,
                    COALESCE(cdev.razon_social, ccust.razon_social, '') AS cliente,
                    COALESCE(d.numero_interno,'') AS numero_interno,
                    COALESCE(d.numero_serie,'') AS numero_serie,
                    COALESCE(b.nombre,'') AS marca,
                    COALESCE(m.nombre,'') AS modelo
                  FROM preventivo_planes p
                  LEFT JOIN devices d ON d.id = p.device_id
                  LEFT JOIN customers cdev ON cdev.id = d.customer_id
                  LEFT JOIN customers ccust ON ccust.id = p.customer_id
                  LEFT JOIN marcas b ON b.id = d.marca_id
                  LEFT JOIN models m ON m.id = d.model_id
                  LEFT JOIN LATERAL (
                    SELECT
                      COALESCE(pr.nombre_repuesto,'') AS repuesto_nombre,
                      pr.aviso_anticipacion_dias AS repuesto_aviso_anticipacion_dias,
                      pr.ultima_revision_fecha AS repuesto_ultima_revision_fecha,
                      pr.proxima_revision_fecha AS repuesto_proxima_revision_fecha
                    FROM preventivo_plan_repuestos pr
                    WHERE pr.plan_id = p.id
                      AND pr.activa = true
                    ORDER BY
                      CASE WHEN pr.proxima_revision_fecha IS NULL THEN 1 ELSE 0 END,
                      pr.proxima_revision_fecha ASC,
                      pr.id ASC
                    LIMIT 1
                  ) prn ON TRUE
                  WHERE p.activa = true
                    AND COALESCE(prn.repuesto_proxima_revision_fecha, p.proxima_revision_fecha) IS NOT NULL
                )
                SELECT
                  plans.*,
                  CASE
                    WHEN CURRENT_DATE > plans.proxima_revision_fecha THEN 'vencido'
                    WHEN (CURRENT_DATE + (COALESCE(plans.aviso_anticipacion_dias,30) * INTERVAL '1 day'))::date >= plans.proxima_revision_fecha THEN 'proximo'
                    ELSE 'al_dia'
                  END AS preventivo_estado,
                  (plans.proxima_revision_fecha - CURRENT_DATE) AS dias_restantes
                FROM plans
                WHERE
                  CURRENT_DATE > plans.proxima_revision_fecha
                  OR (CURRENT_DATE + (COALESCE(plans.aviso_anticipacion_dias,30) * INTERVAL '1 day'))::date >= plans.proxima_revision_fecha
                ORDER BY plans.proxima_revision_fecha ASC, plans.plan_id ASC
                """
            ) or []
        else:
            rows = q(
                """
                WITH plans AS (
                  SELECT
                    p.id AS plan_id,
                    p.scope_type::text AS scope_type,
                    p.device_id,
                    p.customer_id,
                    p.periodicidad_valor,
                    p.periodicidad_unidad::text AS periodicidad_unidad,
                    p.aviso_anticipacion_dias,
                    p.ultima_revision_fecha,
                    p.proxima_revision_fecha,
                    ''::text AS repuesto_proximo_nombre,
                    COALESCE(cdev.razon_social, ccust.razon_social, '') AS cliente,
                    COALESCE(d.numero_interno,'') AS numero_interno,
                    COALESCE(d.numero_serie,'') AS numero_serie,
                    COALESCE(b.nombre,'') AS marca,
                    COALESCE(m.nombre,'') AS modelo
                  FROM preventivo_planes p
                  LEFT JOIN devices d ON d.id = p.device_id
                  LEFT JOIN customers cdev ON cdev.id = d.customer_id
                  LEFT JOIN customers ccust ON ccust.id = p.customer_id
                  LEFT JOIN marcas b ON b.id = d.marca_id
                  LEFT JOIN models m ON m.id = d.model_id
                  WHERE p.activa = true
                    AND p.proxima_revision_fecha IS NOT NULL
                )
                SELECT
                  plans.*,
                  CASE
                    WHEN CURRENT_DATE > plans.proxima_revision_fecha THEN 'vencido'
                    WHEN (CURRENT_DATE + (COALESCE(plans.aviso_anticipacion_dias,30) * INTERVAL '1 day'))::date >= plans.proxima_revision_fecha THEN 'proximo'
                    ELSE 'al_dia'
                  END AS preventivo_estado,
                  (plans.proxima_revision_fecha - CURRENT_DATE) AS dias_restantes
                FROM plans
                WHERE
                  CURRENT_DATE > plans.proxima_revision_fecha
                  OR (CURRENT_DATE + (COALESCE(plans.aviso_anticipacion_dias,30) * INTERVAL '1 day'))::date >= plans.proxima_revision_fecha
                ORDER BY plans.proxima_revision_fecha ASC, plans.plan_id ASC
                """
            ) or []

        if not rows:
            self.stdout.write("No hay preventivos para alertar.")
            return

        limit = int(opts.get("limit") or 0)
        if limit > 0:
            rows = rows[:limit]

        vencidos = [r for r in rows if (r.get("preventivo_estado") or "") == "vencido"]
        proximos = [r for r in rows if (r.get("preventivo_estado") or "") == "proximo"]

        def fmt_row(r):
            if r.get("scope_type") == "device":
                equipo = " | ".join(
                    p for p in [r.get("marca") or "", r.get("modelo") or ""] if p
                ) or "-"
                ns = (r.get("numero_interno") or "").strip() or (r.get("numero_serie") or "").strip() or "-"
                return (
                    f"- Plan #{r.get('plan_id')} | Cliente: {r.get('cliente') or '-'} | "
                    f"Equipo: {equipo} | Serie: {ns} | Proxima: {r.get('proxima_revision_fecha')} | "
                    f"Repuesto: {(r.get('repuesto_proximo_nombre') or '-') } | "
                    f"Dias: {r.get('dias_restantes')}"
                )
            return (
                f"- Plan #{r.get('plan_id')} | Institucion: {r.get('cliente') or '-'} | "
                f"Proxima: {r.get('proxima_revision_fecha')} | Dias: {r.get('dias_restantes')}"
            )

        lines = [
            "Resumen diario de mantenimientos preventivos",
            "",
            f"Vencidos: {len(vencidos)}",
        ]
        if vencidos:
            lines.extend(fmt_row(r) for r in vencidos)
        lines.extend(["", f"Proximos: {len(proximos)}"])
        if proximos:
            lines.extend(fmt_row(r) for r in proximos)
        lines.extend(["", f"Total considerado: {len(rows)}", ""])
        lines.append(f"Abrir agenda: {_frontend_link('/equipos?tab=preventivos')}")
        lines.append("Aviso automático - no responder a este correo.")
        body = _email_append_footer_text("\n".join(lines))
        subject = f"Preventivos pendientes - Vencidos: {len(vencidos)} - Proximos: {len(proximos)}"

        if opts.get("dry_run"):
            self.stdout.write(f"[DRY] destinatarios: {', '.join(recipients)}")
            self.stdout.write(f"[DRY] vencidos={len(vencidos)} proximos={len(proximos)} total={len(rows)}")
            return

        try:
            today = timezone.localdate().isoformat()
            if vencidos:
                emit_notification(
                    "preventivo_vencido",
                    emails=recipients,
                    title=f"Preventivos vencidos: {len(vencidos)}",
                    body="\n".join(fmt_row(r) for r in vencidos[:8]),
                    href="/equipos?tab=preventivos",
                    severity="critical",
                    entity_type="preventivo",
                    entity_id="vencidos",
                    dedupe_key=f"preventivo_vencido:{today}",
                    payload={"count": len(vencidos)},
                )
            if proximos:
                emit_notification(
                    "preventivo_proximo",
                    emails=recipients,
                    title=f"Preventivos próximos: {len(proximos)}",
                    body="\n".join(fmt_row(r) for r in proximos[:8]),
                    href="/equipos?tab=preventivos",
                    severity="warning",
                    entity_type="preventivo",
                    entity_id="proximos",
                    dedupe_key=f"preventivo_proximo:{today}",
                    payload={"count": len(proximos)},
                )
        except Exception:
            pass

        if _send(subject, body, recipients):
            self.stdout.write(f"Resumen preventivos enviado a {len(recipients)} destinatarios")
        else:
            self.stderr.write("No se pudo enviar el resumen de preventivos")
