"""Ciclo de vida de las fichas de particulares en el maestro de clientes de Bejerman.

Un particular no tiene codigo de cliente: NEXORA emite con el codigo ocasional '@@@#@@' y Bejerman
busca la ficha por numero de documento, dandola de alta con un codigo secuencial numerico si no
existe (nota *3 de "Estructuras de datos - Ventas"). O sea que el maestro se va poblando con una
ficha por persona.

Este modulo mantiene esas fichas **habilitadas solo mientras el particular tiene algo abierto**:
se rehabilitan al emitir un comprobante suyo y se deshabilitan al entregarle el ultimo equipo.

Tres reglas que sostienen el diseno (verificadas contra el maestro real):

1. Deshabilitar NO rompe nada: hay fichas deshabilitadas con mas de 100 comprobantes vivos colgando,
   y la FK de SegCabV al maestro sigue intacta. Borrar, en cambio, es imposible.
2. Rehabilitar ANTES de emitir vuelve irrelevante la unica incognita que queda (si la busqueda por
   documento de '@@@#@@' respeta o ignora el flag de habilitado): al momento de emitir esta en 1.
3. Solo se tocan las fichas que creo NEXORA, identificadas por tener codigo puramente numerico. Las
   cargadas a mano desde el escritorio usan codigos mnemotecnicos (PELLEG, CESAR, RVRDS) y son
   decisiones de una persona que este modulo no revierte.

Todo el circuito es best-effort: ni el habilitar ni el deshabilitar pueden tirar abajo una emision
o una entrega ya confirmada.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import connection

from .bejerman_sdk import (
    BejermanSDKClient,
    as_string,
    bejerman_client_record_is_enabled,
    build_customer_toggle_payload,
    find_bejerman_client_record_by_document,
    first_value,
    is_ok_response,
)

logger = logging.getLogger(__name__)


# Estados terminales de un ingreso. Todo lo demas cuenta como abierto, incluidos 'liberado' y
# 'vendido_pendiente_entrega': el equipo ya esta listo pero todavia esta en la casa, y si se
# deshabilita la ficha ahi, la salida de ese equipo se queda sin cliente habilitado.
INGRESO_TERMINAL_STATES = ("entregado", "vendido_entregado", "baja")

BEJERMAN_CUSTOMERS_CACHE_KEY_PREFIX = "bejerman_customer_records"


def _toggle_enabled() -> bool:
    return bool(getattr(settings, "BEJERMAN_PARTICULAR_CUSTOMER_TOGGLE_ENABLED", False))


def _digits_only(value: Any) -> str:
    return "".join(ch for ch in as_string(value) if ch.isdigit())


def is_nexora_created_code(code: Any) -> bool:
    """True si el codigo lo genero la recodificacion automatica de Bejerman (secuencial numerica).

    Al momento de escribir esto no habia ni un codigo numerico entre los 299 clientes de SEPID, asi
    que el criterio distingue sin ambiguedad. Si alguna vez alguien carga a mano un codigo numerico,
    esta funcion deja de ser confiable y hay que pasar a marcarlas por la tabla de auditoria.
    """
    text = as_string(code).strip()
    return bool(text) and text.isdigit()


def _record_code(record: dict[str, Any]) -> str:
    return as_string(
        first_value(record, ("Cliente_Codigo", "ClienteCodigo", "CodigoCliente", "Codigo", "CodCliente"))
    ).strip()


def _invalidate_customers_cache(company_key: Any) -> None:
    """La pagina de Clientes cachea el maestro 5 minutos; despues de escribir hay que tirar esa foto."""
    key = as_string(company_key).strip()
    if key:
        cache.delete(f"{BEJERMAN_CUSTOMERS_CACHE_KEY_PREFIX}:{key}")


def _log_action(
    *,
    documento: str,
    company_key: Any,
    customer_code: str,
    action: str,
    reason: str = "",
) -> None:
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bejerman_particular_customer_log(
                    documento, company_key, customer_code, action, reason
                ) VALUES (%s,%s,%s,%s,%s)
                """,
                [documento, as_string(company_key).strip(), customer_code, action, reason[:500]],
            )
    except Exception:
        # La auditoria no puede ser el motivo por el que falle una emision.
        logger.warning(
            "bejerman_particular_customer_log_failed",
            extra={"documento": documento, "action": action},
            exc_info=True,
        )


def _find_record(client: BejermanSDKClient, documento: str) -> dict[str, Any] | None:
    """Lee el maestro EN VIVO (no por cache): con una foto vieja se decide mal si rehabilitar."""
    return find_bejerman_client_record_by_document(client.list_clientes(), documento)


def ensure_particular_customer_enabled(
    client: BejermanSDKClient,
    company_key: Any,
    propietario_doc: Any,
) -> dict[str, Any] | None:
    """Rehabilita la ficha del particular si existe y esta deshabilitada. Devuelve la ficha o None.

    Si no existe ficha no hace nada: la va a crear el codigo ocasional al emitir. Nunca levanta: un
    fallo aca como mucho termina en una ficha duplicada, y eso no justifica bloquear un ingreso.
    """
    documento = _digits_only(propietario_doc)
    if not documento or not _toggle_enabled():
        return None
    try:
        record = _find_record(client, documento)
        if not record:
            _log_action(
                documento=documento,
                company_key=company_key,
                customer_code="",
                action="skipped",
                reason="sin ficha: la crea el codigo ocasional",
            )
            return None
        if bejerman_client_record_is_enabled(record):
            return record
        code = _record_code(record)
        response = client.modificar_cliente(build_customer_toggle_payload(record, habilitado=True))
        if not is_ok_response(response):
            raise RuntimeError(as_string(response)[:300])
        _invalidate_customers_cache(company_key)
        _log_action(documento=documento, company_key=company_key, customer_code=code, action="enable")
        logger.info(
            "bejerman_particular_customer_enabled",
            extra={"documento": documento, "customer_code": code, "company_key": as_string(company_key)},
        )
        return {**record, "Cliente_Habilitado": True}
    except Exception:
        logger.warning(
            "bejerman_particular_customer_enable_failed",
            extra={"documento": documento, "company_key": as_string(company_key)},
            exc_info=True,
        )
        _log_action(
            documento=documento,
            company_key=company_key,
            customer_code="",
            action="skipped",
            reason="fallo al rehabilitar",
        )
        return None


def has_other_open_ingresos(propietario_doc: Any, exclude_ingreso_id: Any = None) -> bool:
    """True si el mismo documento tiene otro ingreso todavia abierto.

    Es la salvaguarda del caso "un particular trae dos equipos": si se entrega uno y el otro sigue
    adentro, la ficha tiene que seguir habilitada para poder emitir la salida del segundo.
    """
    documento = _digits_only(propietario_doc)
    if not documento:
        return False
    placeholders = ",".join(["%s"] * len(INGRESO_TERMINAL_STATES))
    params: list[Any] = [documento, *INGRESO_TERMINAL_STATES]
    exclude_sql = ""
    try:
        exclude_id = int(exclude_ingreso_id)
    except (TypeError, ValueError):
        exclude_id = None
    if exclude_id is not None:
        exclude_sql = "AND t.id <> %s"
        params.append(exclude_id)
    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT 1
              FROM ingresos t
             WHERE regexp_replace(COALESCE(t.propietario_doc, ''), '[^0-9]', '', 'g') = %s
               AND COALESCE(t.estado::text, '') NOT IN ({placeholders})
               {exclude_sql}
             LIMIT 1
            """,
            params,
        )
        return cur.fetchone() is not None


def disable_particular_customer_if_idle(
    company_key: Any,
    propietario_doc: Any,
    *,
    exclude_ingreso_id: Any = None,
    actor_user_id: int | None = None,
    client: BejermanSDKClient | None = None,
) -> bool:
    """Deshabilita la ficha del particular si ya no le queda nada abierto. True si la deshabilito.

    Solo toca fichas creadas por NEXORA (codigo numerico). Nunca levanta: se llama despues de una
    entrega ya confirmada y no puede revertirla.
    """
    documento = _digits_only(propietario_doc)
    if not documento or not _toggle_enabled():
        return False
    try:
        if has_other_open_ingresos(documento, exclude_ingreso_id):
            _log_action(
                documento=documento,
                company_key=company_key,
                customer_code="",
                action="skipped",
                reason="quedan otros ingresos abiertos del mismo documento",
            )
            return False
        sdk = client or BejermanSDKClient(
            company_key=as_string(company_key).strip() or "SEPID", actor_user_id=actor_user_id
        )
        record = _find_record(sdk, documento)
        if not record or not bejerman_client_record_is_enabled(record):
            return False
        code = _record_code(record)
        if not is_nexora_created_code(code):
            _log_action(
                documento=documento,
                company_key=company_key,
                customer_code=code,
                action="skipped",
                reason="ficha cargada a mano, no la creo NEXORA",
            )
            return False
        response = sdk.modificar_cliente(build_customer_toggle_payload(record, habilitado=False))
        if not is_ok_response(response):
            raise RuntimeError(as_string(response)[:300])
        _invalidate_customers_cache(company_key)
        _log_action(documento=documento, company_key=company_key, customer_code=code, action="disable")
        logger.info(
            "bejerman_particular_customer_disabled",
            extra={"documento": documento, "customer_code": code, "company_key": as_string(company_key)},
        )
        return True
    except Exception:
        logger.warning(
            "bejerman_particular_customer_disable_failed",
            extra={"documento": documento, "company_key": as_string(company_key)},
            exc_info=True,
        )
        return False
