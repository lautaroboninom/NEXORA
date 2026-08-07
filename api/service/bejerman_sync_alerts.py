from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone as datetime_timezone
from typing import Any

from django.conf import settings
from django.db import connection
from django.utils import timezone

from .notification_email_outbox import queue_notification_email
from .notifications import (
    MANAGEMENT_NOTIFICATION_ROLES,
    active_users_for_notification,
    notification_email_recipients_for_users,
)


logger = logging.getLogger(__name__)

NOTIFICATION_KEY = "bejerman_sync_job_blocked"
DEFAULT_ALERT_AFTER_HOURS = 2
LEGACY_BLOCKED_SYNC_TYPES = frozenset({"stock_entry_str", "stock_exit_rts"})


def _stats(*, disabled: int = 0) -> dict[str, int]:
    return {
        "disabled": disabled,
        "matched": 0,
        "recipients": 0,
        "queued": 0,
        "duplicates": 0,
        "reactivated": 0,
        "errors": 0,
    }


def _configured_after_hours(value: float | int | None) -> float:
    raw = (
        value
        if value is not None
        else getattr(
            settings,
            "BEJERMAN_SYNC_BLOCKED_ALERT_AFTER_HOURS",
            DEFAULT_ALERT_AFTER_HOURS,
        )
    )
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return float(DEFAULT_ALERT_AFTER_HOURS)


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime_timezone.utc)
    return parsed


def _utc_episode_timestamp(value: Any) -> str:
    parsed = _as_datetime(value)
    if parsed is None:
        raise ValueError("El job bloqueado no tiene updated_at válido.")
    return (
        parsed.astimezone(datetime_timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _is_stale_actionable_job(job: dict[str, Any], cutoff: datetime) -> bool:
    if str(job.get("status") or "").strip().lower() != "blocked":
        return False
    sync_type = str(job.get("sync_type") or "").strip().lower()
    if sync_type in LEGACY_BLOCKED_SYNC_TYPES:
        return False
    updated_at = _as_datetime(job.get("updated_at"))
    return bool(updated_at and updated_at <= cutoff)


def _stale_blocked_jobs(*, cutoff: datetime, limit: int) -> list[dict[str, Any]]:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id, sync_type, ingreso_id, numero_serie, source_deposit,
                   target_deposit, company_key, status, attempts, last_error,
                   created_at, updated_at
              FROM bejerman_sync_jobs
             WHERE status = 'blocked'
               AND sync_type NOT IN (%s, %s)
               AND updated_at <= %s
             ORDER BY updated_at ASC, id ASC
             LIMIT %s
            """,
            [
                "stock_entry_str",
                "stock_exit_rts",
                cutoff,
                max(1, min(500, int(limit or 100))),
            ],
        )
        columns = [column[0] for column in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def _alert_body(job: dict[str, Any], *, now: datetime) -> str:
    updated_at = _as_datetime(job.get("updated_at"))
    blocked_hours = (
        max(0.0, (now - updated_at).total_seconds() / 3600)
        if updated_at is not None
        else 0.0
    )
    last_error = str(job.get("last_error") or "").strip() or "Sin detalle registrado."
    return "\n".join(
        [
            f"La sincronización Bejerman lleva {blocked_hours:.1f} horas bloqueada.",
            "",
            f"Job: #{job.get('id')}",
            f"Tipo: {job.get('sync_type') or '-'}",
            f"Ingreso / OS: {job.get('ingreso_id') or '-'}",
            f"Empresa: {job.get('company_key') or '-'}",
            f"Número de serie / partida: {job.get('numero_serie') or '-'}",
            (
                "Depósito: "
                f"{job.get('source_deposit') or '-'} → {job.get('target_deposit') or '-'}"
            ),
            f"Intentos: {job.get('attempts') or 0}",
            f"Bloqueado desde: {_utc_episode_timestamp(job.get('updated_at'))}",
            f"Último error: {last_error}",
            "",
            "Revisá la cola de sincronización Bejerman en NEXORA: /bejerman",
        ]
    )


def queue_stale_blocked_bejerman_job_alerts(
    *,
    after_hours: float | int | None = None,
    limit: int = 100,
) -> dict[str, int]:
    """Encola una sola alerta por cada episodio bloqueado, sin interrumpir el worker."""

    result = _stats()
    if not bool(getattr(settings, "BEJERMAN_SYNC_BLOCKED_ALERT_ENABLED", True)):
        return _stats(disabled=1)

    now = timezone.now()
    cutoff = now - timedelta(hours=_configured_after_hours(after_hours))
    try:
        candidates = _stale_blocked_jobs(cutoff=cutoff, limit=limit)
    except Exception:
        logger.exception("No se pudieron consultar los jobs Bejerman bloqueados")
        result["errors"] += 1
        return result

    jobs = [job for job in candidates if _is_stale_actionable_job(job, cutoff)]
    result["matched"] = len(jobs)
    if not jobs:
        return result

    try:
        users = active_users_for_notification(
            NOTIFICATION_KEY,
            roles=MANAGEMENT_NOTIFICATION_ROLES,
            require_email=True,
            channel="email",
        )
        recipients = notification_email_recipients_for_users(users)
    except Exception:
        logger.exception("No se pudo resolver la audiencia de alertas Bejerman")
        result["errors"] += 1
        return result

    result["recipients"] = len(recipients)
    if not recipients:
        return result

    for job in jobs:
        try:
            episode_timestamp = _utc_episode_timestamp(job.get("updated_at"))
            queued = queue_notification_email(
                notification_key=NOTIFICATION_KEY,
                dedupe_key=(
                    f"{NOTIFICATION_KEY}:{int(job['id'])}:{episode_timestamp}"
                ),
                recipients=recipients,
                subject=(
                    "[NEXORA] Sincronización Bejerman bloqueada "
                    f"(job #{int(job['id'])})"
                ),
                body=_alert_body(job, now=now),
                reactivate_failed=False,
            )
            result["queued"] += int((queued or {}).get("queued") or 0)
            result["duplicates"] += int((queued or {}).get("duplicates") or 0)
            result["reactivated"] += int((queued or {}).get("reactivated") or 0)
        except Exception:
            logger.exception(
                "No se pudo encolar la alerta del job Bejerman %s",
                job.get("id"),
            )
            result["errors"] += 1
    return result
