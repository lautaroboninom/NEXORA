"""La emisión del remito, fuera del request HTTP.

El problema que resuelve: la emisión vivía entera dentro del POST, y el navegador
**congela las pestañas que quedan en segundo plano**. Como el operador se va a mirar
la ventana de impresión apenas la abre, la pestaña que estaba esperando la respuesta
se dormía y no la recogía nunca: el remito salía en Bejerman, pero en pantalla el
cartel seguía diciendo "Emitiendo" durante una hora. Volver a la pestaña lo destrababa,
lo que hacía parecer que el servidor "se despertaba" cuando en realidad el que se había
dormido era el cliente.

Con esto el POST sólo anota el pedido y contesta; el trabajo corre aparte y el
resultado queda guardado esperando a que alguien lo venga a buscar. Que la pestaña se
congele, se cierre o se recargue deja de importar: el `jobId` alcanza para retomar.

No se toca `generate_bejerman_remito` por dentro — es una función grande y delicada
(reservas, splits, reconciliación de remitos inciertos). Acá se la envuelve, y se la
sigue corriendo dentro de un `transaction.atomic()` para que se comporte igual que
cuando la cubría `ATOMIC_REQUESTS`.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from typing import Any

from django.db import close_old_connections, connection, transaction

from .bejerman_delivery import BillingError, DeliveryOrderError, generate_bejerman_remito


# Las dos que puede levantar la emisión. No comparten ancestro (las dos cuelgan de
# RuntimeError por separado), así que hay que nombrarlas a mano: si se cae una de la
# lista, su mensaje —que suele ser accionable, tipo "falta stock"— se degrada a un
# "error interno" inútil para el operador.
ERRORES_DE_NEGOCIO = (DeliveryOrderError, BillingError)


logger = logging.getLogger(__name__)

TABLE_NAME = "delivery_remito_jobs"

# Un trabajo normal tarda segundos. Pasado este plazo sin terminar, lo más probable es
# que el proceso que lo corría se haya ido (deploy, reinicio). No se lo marca fallado
# —el remito puede haber salido igual en Bejerman— pero se deja de prometerle al
# operador que siga esperando: se lo manda a mirar el estado de la orden.
STALE_SECONDS = 600

# La ventana del barrido es más ancha que la que ve el operador, a propósito: la
# pestaña deja de esperar a los 10 minutos, pero cerrar la fila es irreversible y el
# hilo podría seguir vivo. A los 20 no queda nadie atrás. Si por algún motivo el hilo
# todavía estuviera trabajando, su `_finish_ok`/`_finish_error` pisa esta fila después
# y gana el resultado real.
SWEEP_AFTER_SECONDS = STALE_SECONDS * 2


class RemitoJobNotFound(DeliveryOrderError):
    def __init__(self, job_id: str):
        super().__init__(
            "DELIVERY_REMITO_JOB_NOT_FOUND",
            "No se encontró la emisión solicitada. Puede haber vencido; revise el estado de la orden.",
            status_code=404,
        )
        self.job_id = job_id


def _json_param(value: Any) -> str:
    return json.dumps(value, default=str)


def _row(cur) -> dict[str, Any] | None:
    raw = cur.fetchone()
    if raw is None:
        return None
    return dict(zip([column[0] for column in cur.description], raw))


def _error_payload(exc: Exception) -> dict[str, Any]:
    """Guarda el error con la misma forma que devolvía el POST sincrónico.

    Así el frontend sigue viendo `detail`/`code`/status como siempre y no hay que
    tocar el manejo de errores que ya existe.
    """

    if isinstance(exc, ERRORES_DE_NEGOCIO):
        payload: dict[str, Any] = {"detail": str(exc), "code": exc.code}
        extra = getattr(exc, "payload", None)
        if isinstance(extra, dict):
            payload.update(extra)
        retry_after_ms = getattr(exc, "retry_after_ms", None)
        if retry_after_ms:
            payload["retry_after_ms"] = retry_after_ms
        payload["status"] = getattr(exc, "status_code", 400) or 400
        return payload
    # Cualquier otra cosa es un error nuestro: no se filtra el detalle al operador,
    # pero queda entero en el log.
    return {
        "detail": "No se pudo emitir el remito por un error interno. El comprobante puede haber salido igual en Bejerman: revise el estado de la orden antes de reintentar.",
        "code": "DELIVERY_REMITO_JOB_FAILED",
        "status": 500,
    }


def _claim(job_id: str) -> dict[str, Any] | None:
    """Pasa el trabajo a `running`. Devuelve None si otro ya lo tomó."""

    with connection.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {TABLE_NAME}
               SET status = 'running', started_at = clock_timestamp()
             WHERE id = %s AND status = 'pending'
            RETURNING id, actor_user_id, order_ids, request_payload
            """,
            [job_id],
        )
        return _row(cur)


def _finish_ok(job_id: str, result: dict[str, Any]) -> None:
    with connection.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {TABLE_NAME}
               SET status = 'done', result_payload = %s::jsonb,
                   error_payload = NULL, finished_at = clock_timestamp()
             WHERE id = %s
            """,
            [_json_param(result), job_id],
        )


def _finish_error(job_id: str, exc: Exception) -> None:
    with connection.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {TABLE_NAME}
               SET status = 'error', error_payload = %s::jsonb,
                   result_payload = NULL, finished_at = clock_timestamp()
             WHERE id = %s
            """,
            [_json_param(_error_payload(exc)), job_id],
        )


def _execute(job_id: str) -> None:
    """Cuerpo del hilo. No puede levantar: nadie lo estaría escuchando."""

    close_old_connections()
    try:
        job = _claim(job_id)
        if job is None:
            logger.info("delivery_remito_job_ya_tomado", extra={"job_id": job_id})
            return
        order_ids = job.get("order_ids") or []
        payload = job.get("request_payload") or {}
        if isinstance(order_ids, str):
            order_ids = json.loads(order_ids)
        if isinstance(payload, str):
            payload = json.loads(payload)
        try:
            # El atomic reproduce lo que hacía ATOMIC_REQUESTS cuando esto corría
            # dentro del POST. Las escrituras de estado del trabajo quedan afuera a
            # propósito, para que se vean aunque la emisión haga rollback.
            with transaction.atomic():
                result = generate_bejerman_remito(order_ids, job.get("actor_user_id"), payload)
        except Exception as exc:
            logger.warning(
                "delivery_remito_job_error",
                extra={"job_id": job_id, "order_ids": order_ids, "error": str(exc)},
                exc_info=not isinstance(exc, ERRORES_DE_NEGOCIO),
            )
            _finish_error(job_id, exc)
        else:
            logger.info(
                "delivery_remito_job_listo",
                extra={"job_id": job_id, "remito": result.get("remitoNumber") if isinstance(result, dict) else ""},
            )
            _finish_ok(job_id, result)
    except Exception:
        logger.exception("delivery_remito_job_crash", extra={"job_id": job_id})
    finally:
        close_old_connections()


def _spawn(job_id: str) -> None:
    threading.Thread(
        target=_execute,
        args=(job_id,),
        name=f"remito-{job_id[:8]}",
        daemon=True,
    ).start()



def sweep_stale_remito_jobs(*, older_than_seconds: int | None = None) -> int:
    """Cierra las emisiones que quedaron colgadas cuando se fue el proceso.

    El trabajo corre en un hilo del propio contenedor de la API. Si la API se
    reinicia en el medio —un deploy, un OOM, el reboot del servidor— el hilo se va
    con ella y la fila queda en `running` para siempre: nadie la vuelve a tomar y
    nadie la termina. El operador se destraba solo (a los 10 minutos la pestaña le
    dice que verifique), pero la fila sigue ahí, y el panel se llena de emisiones
    eternamente "en curso" que no significan nada.

    No las marca como fallidas alegremente: el remito **puede haber salido igual**
    en Bejerman antes de que se cortara. Por eso el error que se guarda no dice
    "falló", dice "verificá antes de reintentar", que es la verdad.
    """

    limite = max(60, int(older_than_seconds or SWEEP_AFTER_SECONDS))
    payload = _json_param(
        {
            "detail": (
                "La emisión quedó sin respuesta (se reinició el servidor mientras corría). "
                "El remito puede haber salido igual en Bejerman: verifique el estado de la "
                "orden antes de reintentar."
            ),
            "code": "DELIVERY_REMITO_JOB_STALE",
            "status": 409,
            "sweptBy": "sweep_stale_remito_jobs",
        }
    )
    with connection.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {TABLE_NAME}
               SET status = 'error',
                   error_payload = %s::jsonb,
                   finished_at = clock_timestamp()
             WHERE status IN ('pending', 'running')
               AND clock_timestamp() - COALESCE(started_at, created_at)
                     >= (%s * INTERVAL '1 second')
            RETURNING id
            """,
            [payload, limite],
        )
        cerrados = cur.fetchall()
    if cerrados:
        logger.info(
            "delivery_remito_jobs_barridos",
            extra={"cantidad": len(cerrados), "job_ids": [fila[0] for fila in cerrados]},
        )
    return len(cerrados)

def _open_job_for(order_ids: list[str]) -> dict[str, Any] | None:
    """¿Ya hay una emisión en curso para exactamente estas órdenes?

    Evita que un doble click o una recarga larguen una segunda emisión mientras la
    primera todavía está hablando con Bejerman.
    """

    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, status, result_payload, error_payload, created_at, started_at, finished_at,
                   EXTRACT(EPOCH FROM (clock_timestamp() - COALESCE(started_at, created_at))) AS edad
              FROM {TABLE_NAME}
             WHERE status IN ('pending', 'running')
               AND order_ids @> %s::jsonb AND order_ids <@ %s::jsonb
             ORDER BY created_at DESC
             LIMIT 1
            """,
            [_json_param(order_ids), _json_param(order_ids)],
        )
        return _row(cur)


def enqueue_remito_job(
    order_ids: list[str],
    actor_user_id: int | None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Anota la emisión y contesta enseguida. El trabajo arranca al commitear."""

    ids = [str(item).strip() for item in (order_ids or []) if str(item).strip()]
    if not ids:
        raise DeliveryOrderError("ORDER_IDS_REQUIRED", "Hay que seleccionar órdenes")

    existente = _open_job_for(ids)
    if existente is not None and float(existente.get("edad") or 0) < STALE_SECONDS:
        return _public_job(existente)

    job_id = f"drj-{uuid.uuid4()}"
    with connection.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {TABLE_NAME} (id, actor_user_id, order_ids, request_payload, status)
            VALUES (%s, %s, %s::jsonb, %s::jsonb, 'pending')
            """,
            [job_id, actor_user_id, _json_param(ids), _json_param(payload or {})],
        )
    # on_commit y no acá mismo: con ATOMIC_REQUESTS la fila todavía no está
    # commiteada, así que el hilo no la encontraría (o se quedaría esperando el lock).
    transaction.on_commit(lambda: _spawn(job_id))
    logger.info("delivery_remito_job_encolado", extra={"job_id": job_id, "order_ids": ids})
    return {"jobId": job_id, "status": "pending", "done": False}


def _public_job(row: dict[str, Any]) -> dict[str, Any]:
    status = str(row.get("status") or "pending")
    edad = float(row.get("edad") or 0)
    if status in {"pending", "running"} and edad >= STALE_SECONDS:
        # Ni terminado ni fallado: honestamente, no se sabe. Lo que no se puede es
        # dejar al operador esperando para siempre.
        return {
            "jobId": row.get("id"),
            "status": "unknown",
            "done": True,
            "error": {
                "detail": (
                    "La emisión quedó sin respuesta (probablemente por un reinicio del servidor). "
                    "El remito puede haber salido igual en Bejerman: revise el estado de la orden antes de reintentar."
                ),
                "code": "DELIVERY_REMITO_JOB_STALE",
                "status": 409,
            },
        }
    payload: dict[str, Any] = {
        "jobId": row.get("id"),
        "status": status,
        "done": status in {"done", "error"},
        "elapsedSeconds": int(edad),
    }
    if status == "done":
        result = row.get("result_payload")
        payload["result"] = json.loads(result) if isinstance(result, str) else result
    if status == "error":
        error = row.get("error_payload")
        payload["error"] = json.loads(error) if isinstance(error, str) else error
    return payload


def get_remito_job(job_id: str, actor_user_id: int | None = None) -> dict[str, Any]:
    """Estado del trabajo. Es lo que consulta la pestaña que está esperando."""

    clean_id = str(job_id or "").strip()
    if not clean_id:
        raise RemitoJobNotFound(clean_id)
    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, actor_user_id, status, result_payload, error_payload,
                   EXTRACT(EPOCH FROM (clock_timestamp() - COALESCE(started_at, created_at))) AS edad
              FROM {TABLE_NAME}
             WHERE id = %s
            """,
            [clean_id],
        )
        row = _row(cur)
    if row is None:
        raise RemitoJobNotFound(clean_id)
    return _public_job(row)
