"""Horario de los trabajos que corren una vez por día.

Los runners diarios (alertas de la mañana, resumen de Cobranzas) duermen hasta la
hora configurada y ahí llaman al comando de turno. Estaba repetido en cada
comando, y con la misma falla en los dos: si el contenedor arrancaba *después* de
la hora —un deploy al mediodía, el reboot de la madrugada que terminó tarde, un
corte de luz— `next_daily_run` saltaba directo a mañana y ese día se quedaba sin
alertas, en silencio y sin que nada lo registrara.

`already_due_today` es la pieza que faltaba: dice si la hora de hoy ya pasó, para
poder correr el trabajo al arrancar en vez de darlo por perdido. Repetir una
corrida no molesta porque las notificaciones se deduplican por día (la cola de
correos rechaza el mismo `notification_key` + `dedupe_key` + destinatario), así
que el riesgo de correr de más es cero y el de correr de menos es un día entero
de avisos que nadie recibe.
"""

from __future__ import annotations

from datetime import datetime, time as daily_time, timedelta

from django.core.management.base import CommandError
from django.utils import timezone


def parse_daily_time(value: str, *, setting_name: str = "la hora diaria") -> daily_time:
    clean_value = str(value or "").strip()
    try:
        return datetime.strptime(clean_value, "%H:%M").time()
    except ValueError as exc:
        raise CommandError(f"{setting_name} debe tener formato HH:MM.") from exc


def next_daily_run(now: datetime, target_time: daily_time) -> datetime:
    current_timezone = timezone.get_current_timezone()
    local_now = timezone.localtime(now, current_timezone)
    candidate = timezone.make_aware(
        datetime.combine(local_now.date(), target_time),
        current_timezone,
    )
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate


def already_due_today(now: datetime, target_time: daily_time) -> bool:
    """¿La hora de hoy ya pasó? Entonces el arranque tiene que correr el trabajo."""

    current_timezone = timezone.get_current_timezone()
    local_now = timezone.localtime(now, current_timezone)
    scheduled_today = timezone.make_aware(
        datetime.combine(local_now.date(), target_time),
        current_timezone,
    )
    return local_now >= scheduled_today
