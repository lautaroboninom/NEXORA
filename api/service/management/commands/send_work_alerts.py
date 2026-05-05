from types import SimpleNamespace

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.utils import timezone

from service.views.helpers import _email_append_footer_text, exec_void, q
from service.views.work_views import build_work_summary, work_summary_email_fingerprint
from service.notifications import emit_notification


class Command(BaseCommand):
    help = "Envía el resumen operativo de atrasos críticos y objetivos"

    def add_arguments(self, parser):
        parser.add_argument("--periodo", choices=["hoy", "semana"], default="hoy")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--force", action="store_true")
        parser.add_argument("--limit", type=int, default=0)

    def handle(self, *args, **opts):
        periodo = opts["periodo"]
        dry_run = bool(opts["dry_run"])
        force = bool(opts["force"])
        limit = int(opts["limit"] or 0)

        rows = q(
            """
            SELECT id, nombre, email, rol
              FROM users
             WHERE activo = TRUE
               AND email IS NOT NULL
               AND NULLIF(TRIM(email), '') IS NOT NULL
               AND rol IN ('jefe','jefe_veedor','admin')
             ORDER BY rol, nombre
            """
        ) or []
        if limit > 0:
            rows = rows[:limit]

        sent = 0
        skipped = 0
        today = timezone.localdate().isoformat()
        notification_key = f"work_summary:{periodo}:{today}"

        for user in rows:
            request = SimpleNamespace(
                user=SimpleNamespace(id=user.get("id"), rol=user.get("rol")),
                user_id=user.get("id"),
            )
            summary = build_work_summary(request, periodo=periodo)
            digest = work_summary_email_fingerprint(summary)

            state = q(
                """
                SELECT payload_hash
                  FROM work_notification_state
                 WHERE channel = 'email'
                   AND notification_key = %s
                   AND user_id = %s
                 LIMIT 1
                """,
                [notification_key, user.get("id")],
                one=True,
            ) or {}
            if not force and state.get("payload_hash") == digest:
                skipped += 1
                continue

            subject = "Resumen operativo del servicio técnico"
            lines = [
                f"Hola {user.get('nombre') or ''},",
                "",
                f"Resumen operativo: {periodo}.",
                "",
                "Alertas:",
            ]
            alerts = summary.get("alerts") or []
            if alerts:
                lines.extend(
                    f"- {a.get('title')}: {a.get('count')} ({a.get('severity')})"
                    for a in alerts
                )
            else:
                lines.append("- Sin alertas activas.")

            objetivos = summary.get("objetivos") or []
            if objetivos:
                lines.extend(["", "Objetivos:"])
                for obj in objetivos:
                    lines.append(
                        f"- {obj.get('label')}: {obj.get('progress')}/{obj.get('target_value')} "
                        f"({obj.get('status')})"
                    )

            body = _email_append_footer_text("\n".join(lines))
            if dry_run:
                self.stdout.write(f"DRY-RUN {user.get('email')}: {subject}")
            else:
                try:
                    emit_notification(
                        "resumen_operativo",
                        user_ids=[user.get("id")],
                        title="Resumen operativo del servicio técnico",
                        body="\n".join(lines[2:]).strip(),
                        href="/",
                        severity="warning" if alerts else "info",
                        entity_type="trabajo",
                        entity_id=periodo,
                        dedupe_key=notification_key,
                        payload={"periodo": periodo, "alerts": alerts},
                    )
                except Exception:
                    pass
                send_mail(
                    subject,
                    body,
                    getattr(settings, "DEFAULT_FROM_EMAIL", None),
                    [user.get("email")],
                    fail_silently=True,
                )
                exec_void(
                    """
                    INSERT INTO work_notification_state(
                      channel, notification_key, user_id, last_sent_at, payload_hash
                    ) VALUES ('email', %s, %s, CURRENT_TIMESTAMP, %s)
                    ON CONFLICT (channel, notification_key, (COALESCE(user_id, -1)))
                    DO UPDATE SET
                      last_sent_at = CURRENT_TIMESTAMP,
                      payload_hash = EXCLUDED.payload_hash,
                      updated_at = CURRENT_TIMESTAMP
                    """,
                    [notification_key, user.get("id"), digest],
                )
            sent += 1

        self.stdout.write(f"Resumen operativo procesado. enviados={sent} omitidos={skipped}")
