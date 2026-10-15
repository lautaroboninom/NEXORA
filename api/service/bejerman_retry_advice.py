"""Qué conviene hacer con un proceso de Bejerman que falló, y dónde está el equipo.

El panel ofrecía «Reintentar» para todo, y reintentar sólo sirve cuando la falla fue
pasajera. El 15/10/2026 un RIS se reintentó diez veces contra el mismo rechazo: dos
equipos del mismo remito tenían cargada la misma serie, cosa que ningún reintento
arregla. Y para destrabar las operaciones de stock había que consultar Bejerman por SQL:
el panel mostraba el error, pero no dónde estaba el equipo.

Este módulo lee el error (y el poco contexto que hace falta) y devuelve un consejo: qué
clase de falla es, qué hacer y si reintentar tal cual tiene sentido. También arma el
veredicto de «Ver en Bejerman» a partir del stock de la partida en todos los depósitos.

Sin imports de Django a propósito: es lógica pura y se prueba con Python pelado.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable, Mapping

# Qué hacer con «Reintentar» según la clase de falla. El panel pide confirmación antes de
# reintentar algo que no está recomendado.
RETRY_RECOMMENDED = "recommended"
RETRY_POINTLESS = "pointless"
RETRY_DANGEROUS = "dangerous"

# Traslados en los que el operador puede elegir de qué depósito sale el equipo. El pase a
# DES no entra: ese ya busca solo el depósito donde está la partida.
MOVABLE_SYNC_TYPES = frozenset(
    {
        "stock_str_to_stl",
        "stock_str_to_stc",
        "stock_str_to_val",
        "stock_str_to_stcl",
        "stock_location_transfer",
        "stock_val_to_stl",
    }
)
# Fases en las que el traslado ya pudo haber emitido la salida: ahí cambiar el origen
# dejaría una SAL en un depósito y la siguiente en otro.
EMITTED_TRANSFER_PHASES = frozenset({"sal_pending", "sal_done", "ent_pending", "done", "target_entry_done"})
AUTO_SOURCE_DEPOSITS = frozenset({"", "AUTO"})
MAX_MOVE_OPTIONS = 4

# Cortes con el pedido ya en camino: Bejerman pudo haberlo procesado igual.
_IN_FLIGHT_MARKERS = (
    "read timed out",
    "connection aborted",
    "remotedisconnected",
    "remote end closed",
    "connection reset",
    "reset by peer",
    "sin confirmacion",
    "requiere reconciliacion",
)
# Fallas en las que el pedido no llegó a ejecutarse, o en las que Bejerman pide volver a
# probar: reintentar es lo que corresponde.
_TRANSIENT_MARKERS = (
    "timed out",
    "timeout",
    "connection refused",
    "failed to establish",
    "max retries exceeded",
    "name or service not known",
    "serviceactivationexception",
    "service unavailable",
    "bad gateway",
    "gateway time",
    "no es un rechazo",
    "reintente en un minuto",
    "no respondio",
    "no responde",
    "temporalmente",
    "todavia no se confirmo",
    "expcuotasv",
    "expsincuotasv",
    "invalid object name",
    "there is already an object named",
    "deadlock",
    "error http bejerman",
)
_ALREADY_IMPORTED_MARKERS = ("ya existe en la empresa", "importacion anterior")
_ARTICLE_MARKERS = (
    "mas de un articulo",
    "articulo bejerman posible",
    "falta definir el articulo",
    "articulo bejerman para restaurar",
    "sin candidatos",
    "no se pudo resolver el articulo",
)
_STOCK_LOCATION_MARKERS = (
    "stock positivo",
    "no tiene stock",
    "sin stock",
    "varios depositos",
)
_EXHAUSTED_PREFIX = re.compile(r"^\s*m[aá]ximos reintentos agotados:\s*", re.IGNORECASE)
_PARTIDA_ARTICLE = re.compile(r"partida\s+(\S+)\s+para el art[ií]culo\s+(\S+)", re.IGNORECASE)
_ARTICLE_CODE = re.compile(r"art[ií]culo\s+(\S+?)\s*\.?\s*$", re.IGNORECASE)


def _plain(value: Any) -> str:
    """Texto comparable: sin tildes, en minúsculas y con los espacios colapsados."""

    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", text).strip().lower()


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _deposit(value: Any) -> str:
    return _clean(value).upper()


def _code(value: Any) -> str:
    return _clean(value).upper()


def _has_any(text: str, markers: Iterable[str]) -> bool:
    return any(marker in text for marker in markers)


def _without_exhausted_prefix(message: Any) -> str:
    """«Máximos reintentos agotados: X» se clasifica por X, que es la falla real."""

    return _EXHAUSTED_PREFIX.sub("", _clean(message))


def _advice(kind: str, title: str, detail: str, *, retry: str = "", action: str = "") -> dict[str, str]:
    return {"kind": kind, "title": title, "detail": detail, "retry": retry, "action": action}


def _join(values: Iterable[str]) -> str:
    items = [value for value in dict.fromkeys(values) if value]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " y " + items[-1]


def _transient_advice() -> dict[str, str]:
    return _advice(
        "transient",
        "Falla pasajera",
        "Bejerman no respondió o estaba ocupado. Reintentar debería funcionar.",
        retry=RETRY_RECOMMENDED,
        action="retry",
    )


def advice_for_stock_job(job: Mapping[str, Any]) -> dict[str, str] | None:
    """Consejo para una operación de stock fallida o bloqueada.

    En el stock reintentar nunca duplica: Bejerman deduplica cada movimiento por su
    `Comprobante_IdOrigen`. Por eso acá el único peligro es la pérdida de tiempo de
    reintentar algo que va a volver a fallar.
    """

    status = _plain(job.get("status"))
    if status not in {"failed", "blocked"}:
        return None
    # El tipo sin implementación ya lo explica `unsupported_reason`, con el mismo consejo.
    if job.get("supported_sync_type") is False:
        return None
    message = _without_exhausted_prefix(job.get("last_error"))
    text = _plain(message)
    if not text:
        return None
    if "tipo de sincronizacion no soportado" in text:
        return None
    if "credenciales" in text or "sesion de usuario" in text:
        return _advice(
            "credentials",
            "Falta la credencial de Bejerman",
            "La operación necesita las credenciales de Bejerman del usuario que la inició. "
            "Se reanuda sola cuando las cargue; también puede reintentarla usted con las suyas.",
            retry=RETRY_RECOMMENDED,
            action="retry",
        )
    if _has_any(text, _ALREADY_IMPORTED_MARKERS):
        return _advice(
            "already_done",
            "El movimiento ya había entrado",
            "Bejerman dice que el comprobante ya existe: el movimiento entró en un intento anterior. "
            "Reintentar lo confirma contra el stock y cierra la operación, sin emitir otro comprobante.",
            retry=RETRY_RECOMMENDED,
            action="retry",
        )
    if "numero de serie requerido" in text:
        return _advice(
            "needs_data",
            "Falta el número de serie",
            "Sin serie no se puede ubicar la partida en Bejerman. Cárguela con «Editar» y después reintente.",
            retry=RETRY_POINTLESS,
            action="edit",
        )
    if "figura como articulo" in text and "confirmado" in text:
        return _advice(
            "needs_data",
            "La partida tiene otro artículo",
            "En Bejerman la partida figura con un artículo distinto del que tiene confirmado el equipo. "
            "Use «Ver en Bejerman» para ver dónde está y con qué artículo, y corrija el que esté mal "
            "antes de reintentar.",
            retry=RETRY_POINTLESS,
            action="diagnose",
        )
    if _has_any(text, _ARTICLE_MARKERS):
        return _advice(
            "needs_data",
            "Falta elegir el artículo",
            "No está definido qué artículo de Bejerman corresponde a este modelo. Elíjalo en el detalle "
            "(«Resolver artículo Bejerman»): al confirmarlo, la operación se reabre sola.",
            retry=RETRY_POINTLESS,
            action="article",
        )
    if _has_any(text, _STOCK_LOCATION_MARKERS):
        return _advice(
            "check_bejerman",
            "El equipo no está donde se lo espera",
            "Bejerman no tiene el equipo en el depósito de origen, así que reintentar tal cual da el "
            "mismo error. Use «Ver en Bejerman» para saber dónde está y elegir qué hacer.",
            retry=RETRY_POINTLESS,
            action="diagnose",
        )
    if _has_any(text, _IN_FLIGHT_MARKERS) or _has_any(text, _TRANSIENT_MARKERS):
        return _transient_advice()
    return None


def _repeated_serial_advice(serial: str, repeated_serial_os: list[str]) -> dict[str, str]:
    return _advice(
        "needs_data",
        "Serie repetida en el remito",
        f"La serie {serial} está cargada en más de un equipo de este remito "
        f"({_join(repeated_serial_os)}). Bejerman lo rechaza por eso y ningún reintento lo va a "
        "arreglar: corrija la serie del equipo que esté mal y después reintente.",
        retry=RETRY_POINTLESS,
        action="open_os",
    )


def advice_for_ingreso_remito(
    row: Mapping[str, Any],
    *,
    repeated_serial_os: Iterable[Any] = (),
) -> dict[str, str] | None:
    """Consejo para un remito de ingreso (RIS/RDA/RDN) fallido o en revisión.

    `repeated_serial_os` son las otras OS del mismo remito que tienen cargada la misma
    serie que ésta: el error más común que ningún reintento arregla.
    """

    status = _plain(row.get("status"))
    if status == "failed_review":
        if _plain(row.get("reviewState")) == "superseded":
            return None
        return _advice(
            "review",
            "Sin confirmar en Bejerman",
            "No se sabe si el remito llegó a emitirse. Use «Verificar en Bejerman», que busca los "
            "remitos de ingreso de esta serie, y después «Resolver revisión»: si apareció, adóptelo; "
            "si no, márquelo como no emitido y vuelva a emitirlo.",
            retry=RETRY_DANGEROUS,
            action="verify",
        )
    if status != "failed":
        return None
    repeated = [_clean(item) for item in repeated_serial_os if _clean(item)]
    if repeated:
        return _repeated_serial_advice(_clean(row.get("numero_serie")) or "del equipo", repeated)
    message = _without_exhausted_prefix(row.get("last_error"))
    text = _plain(message)
    if not text:
        return None
    if "no hay stock suficiente en la partida" in text:
        match = _PARTIDA_ARTICLE.search(message)
        partida = match.group(1) if match else _clean(row.get("numero_serie"))
        return _advice(
            "needs_data",
            "Bejerman rechaza la partida",
            f"Bejerman no acepta el remito por el stock de la partida {partida}. Reintentar sin cambiar "
            "nada da el mismo error: revise que la serie del equipo sea la correcta y use "
            "«Ver en Bejerman» para saber dónde está esa partida.",
            retry=RETRY_POINTLESS,
            action="partida_stock",
        )
    if "no hay stock ingresado para el articulo" in text:
        match = _ARTICLE_CODE.search(message)
        article = match.group(1).rstrip(".") if match else ""
        detail_article = f"El artículo {article}" if article else "El artículo del equipo"
        return _advice(
            "needs_data",
            "El artículo no tiene stock en Bejerman",
            f"{detail_article} no tiene stock ingresado en Bejerman y por eso no se acepta el remito. "
            "Hay que revisar ese artículo en Bejerman, o el artículo asignado al equipo, antes de "
            "reintentar.",
            retry=RETRY_POINTLESS,
            action="open_os",
        )
    if "partida inexistente" in text or "no informada" in text:
        return _advice(
            "needs_data",
            "Bejerman no reconoce la partida",
            "Bejerman no encuentra la partida del equipo. Revise la serie y use «Ver en Bejerman» "
            "antes de reintentar.",
            retry=RETRY_POINTLESS,
            action="partida_stock",
        )
    if "no se pudo confirmar el alta de stock" in text:
        return _advice(
            "check_bejerman",
            "No se confirmó el alta de stock",
            "NEXORA no pudo confirmar que la partida haya entrado al depósito antes de mandar el remito. "
            "Use «Ver en Bejerman» para saber dónde está la partida antes de reintentar.",
            retry=RETRY_POINTLESS,
            action="partida_stock",
        )
    if _has_any(text, _ALREADY_IMPORTED_MARKERS):
        return _advice(
            "already_done",
            "El comprobante ya existe",
            "Bejerman dice que el comprobante ya existe. Reintentar lo busca y lo asocia, sin emitir otro.",
            retry=RETRY_RECOMMENDED,
            action="retry",
        )
    if _has_any(text, _IN_FLIGHT_MARKERS):
        return _advice(
            "uncertain",
            "Pudo haber salido igual",
            "La conexión se cortó con el pedido en camino: el remito pudo haberse emitido igual. "
            "Búsquelo en Bejerman antes de reintentar, o el reintento puede duplicarlo.",
            retry=RETRY_DANGEROUS,
            action="",
        )
    if _has_any(text, _TRANSIENT_MARKERS):
        return _transient_advice()
    return None


def advice_for_delivery_group(
    row: Mapping[str, Any],
    *,
    issued_remitos: Iterable[Any] = (),
) -> dict[str, str] | None:
    """Consejo para un remito de orden de entrega.

    `issued_remitos` son los remitos que otro intento ya emitió para las mismas órdenes:
    con eso a la vista, este intento fallido ya no es trabajo pendiente.
    """

    status = _plain(row.get("status"))
    if _plain(row.get("reviewState")) == "superseded":
        return None
    issued = [_clean(item) for item in issued_remitos if _clean(item)]
    if issued and status in {"failed", "failed_review", "sent_unknown"}:
        detail = (
            f"La orden ya salió con {_join(issued)}, emitido en otro intento. Reintentar éste no "
            "corresponde: el proceso sale solo del panel cuando la orden se factura o se entrega."
        )
        if status != "failed":
            detail += (
                " Si este intento también quedó emitido en Bejerman, anúlelo allá y cierre el "
                "proceso con «Resolver revisión» → «Es un duplicado»."
            )
        return _advice("already_done", "La orden ya tiene su remito", detail, retry=RETRY_DANGEROUS, action="")
    if status == "sent_unknown":
        return _advice(
            "uncertain",
            "Sin respuesta de Bejerman",
            "El remito se mandó y Bejerman no confirmó si lo emitió. NEXORA lo vuelve a verificar sola: "
            "no lo emita a mano mientras tanto, o puede quedar duplicado.",
            retry=RETRY_DANGEROUS,
            action="",
        )
    if status == "failed_review":
        return _advice(
            "review",
            "Sin confirmar en Bejerman",
            "No se sabe si el remito llegó a emitirse. Use «Verificar en Bejerman»: resuelve sola la "
            "mayoría de los casos.",
            retry=RETRY_DANGEROUS,
            action="verify",
        )
    if status != "failed":
        return None
    text = _plain(_without_exhausted_prefix(row.get("last_error")))
    if not text:
        return None
    if _has_any(text, _IN_FLIGHT_MARKERS):
        return _advice(
            "uncertain",
            "Pudo haber salido igual",
            "La conexión se cortó con el pedido en camino: el remito pudo haberse emitido igual. "
            "Búsquelo en Bejerman (por cliente y fecha) antes de reintentar.",
            retry=RETRY_DANGEROUS,
            action="",
        )
    if _has_any(text, _TRANSIENT_MARKERS):
        return _transient_advice()
    return _advice(
        "needs_data",
        "Bejerman rechazó el remito",
        "Bejerman rechazó el comprobante: reintentarlo sin cambios va a dar el mismo error. Corrija "
        "la orden de entrega según el mensaje y después reintente.",
        retry=RETRY_POINTLESS,
        action="",
    )


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _option(action: str, label: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"action": action, "label": label, "detail": detail, **extra}


def can_change_source(job: Mapping[str, Any]) -> bool:
    """¿Se le puede cambiar el depósito de origen a esta operación?

    Sólo a un traslado que todavía no emitió nada y que no viaja en un lote: el renglón de
    un lote va en una SAL compartida con otros equipos.
    """

    if _clean(job.get("sync_type")) not in MOVABLE_SYNC_TYPES:
        return False
    if job.get("lote_id"):
        return False
    if _plain(job.get("status")) not in {"failed", "blocked"}:
        return False
    payload = job.get("request_payload") if isinstance(job.get("request_payload"), Mapping) else {}
    if payload.get("sal_done"):
        return False
    return _plain(payload.get("transfer_phase")) not in EMITTED_TRANSFER_PHASES


def stock_diagnosis(
    *,
    serial: Any,
    source_deposit: Any,
    target_deposit: Any,
    records: Iterable[Mapping[str, Any]],
    equipment_article: Any = "",
    movable: bool = False,
) -> dict[str, Any]:
    """Veredicto de «Ver en Bejerman» para una operación de stock.

    `records` es el stock positivo de la partida en todos los depósitos, con las claves de
    `_stock_record_summary` (deposito, article_code, article_description, quantity).
    """

    serial_text = _clean(serial)
    source = _deposit(source_deposit)
    target = _deposit(target_deposit)
    auto_source = source in AUTO_SOURCE_DEPOSITS
    wanted = _code(equipment_article)

    locations: list[dict[str, Any]] = []
    for record in records or []:
        deposit = _deposit(record.get("deposito"))
        quantity = _number(record.get("quantity"))
        if not deposit or (quantity is not None and quantity <= 0):
            continue
        article = _code(record.get("article_code"))
        locations.append(
            {
                "deposit": deposit,
                "articleCode": article,
                "articleDescription": _clean(record.get("article_description")),
                "quantity": quantity,
                "matchesArticle": not wanted or not article or article == wanted,
                "role": "target" if deposit == target else "source" if deposit == source else "other",
            }
        )

    base = {"serial": serial_text, "sourceDeposit": source, "targetDeposit": target, "locations": locations}
    if not serial_text:
        return {
            **base,
            "verdict": "no_serial",
            "title": "El equipo no tiene número de serie",
            "detail": "Sin serie no se puede buscar la partida en Bejerman. Cárguela con «Editar» y vuelva a consultar.",
            "options": [_option("edit", "Editar equipo", "Cargar el número de serie.")],
        }

    matching = [item for item in locations if item["matchesArticle"]]
    in_target = [item for item in matching if item["role"] == "target"]
    in_source = [item for item in matching if item["role"] == "source"]
    elsewhere = [item for item in matching if item["role"] == "other"]
    if auto_source:
        # El pase a DES no tiene origen fijo: cualquier depósito que no sea el destino lo es.
        elsewhere = in_source + elsewhere
        in_source = []

    if in_target:
        detail = (
            f"Bejerman ya tiene el equipo en {target}. Reintentar cierra la operación como hecha, "
            "sin emitir comprobantes."
        )
        if in_source:
            detail += (
                f" También figura en {source}: el reintento emite la salida de {source} que faltaba, "
                "para que no quede duplicado."
            )
        if elsewhere:
            detail += (
                f" Además figura en {_join(item['deposit'] for item in elsewhere)}: ese stock sobra y "
                "hay que corregirlo en Bejerman."
            )
        return {
            **base,
            "verdict": "already_in_target",
            "title": "Ya está en el destino",
            "detail": detail,
            "options": [_option("retry", "Dar por hecha", "Reintenta: confirma el stock y cierra la operación.")],
        }

    if in_source:
        detail = (
            f"Bejerman tiene el equipo en {source}, que es donde esta operación lo espera. Si falló, "
            "fue por otra causa: lea el mensaje del último intento y reintente."
        )
        if elsewhere:
            detail += (
                f" Ojo: también figura en {_join(item['deposit'] for item in elsewhere)}, y ese stock "
                "sobra."
            )
        return {
            **base,
            "verdict": "in_source",
            "title": "Está en el origen",
            "detail": detail,
            "options": [_option("retry", "Reintentar", "Vuelve a ejecutar la operación.")],
        }

    if elsewhere:
        deposits = list(dict.fromkeys(item["deposit"] for item in elsewhere))
        listed = _join(deposits)
        if auto_source:
            if len(deposits) == 1:
                return {
                    **base,
                    "verdict": "in_source",
                    "title": f"Está en {deposits[0]}",
                    "detail": (
                        f"Bejerman tiene el equipo en {deposits[0]}: reintentar debería pasarlo a {target}."
                    ),
                    "options": [_option("retry", "Reintentar", f"Mueve el equipo de {deposits[0]} a {target}.")],
                }
            return {
                **base,
                "verdict": "several",
                "title": "Está en varios depósitos",
                "detail": (
                    f"La partida figura en {listed}, y una unidad no puede estar en dos lugares. "
                    "Corrija el stock sobrante en Bejerman y después reintente."
                ),
                "options": [],
            }
        detail = f"Bejerman tiene el equipo en {listed}, no en {source}."
        options: list[dict[str, Any]] = []
        if movable:
            detail += f" Puede moverlo desde donde está: sale de ese depósito y entra en {target}."
            options.extend(
                _option(
                    "move_from",
                    f"Mover desde {deposit}",
                    f"Cambia el origen a {deposit} y reintenta: sale de {deposit} y entra en {target}.",
                    deposit=deposit,
                )
                for deposit in deposits[:MAX_MOVE_OPTIONS]
            )
        else:
            detail += " Corrija el stock en Bejerman o descarte la operación si ya no corresponde."
        if len(deposits) > 1:
            detail += " Al figurar en más de un depósito, uno de esos stocks sobra y hay que corregirlo en Bejerman."
        options.append(
            _option(
                "discard",
                "Descartar",
                "Cierra la operación sin mover nada.",
                reason=f"Bejerman tiene el equipo en {listed}, no en {source}; el pase ya no corresponde.",
            )
        )
        return {
            **base,
            "verdict": "elsewhere" if len(deposits) == 1 else "several",
            "title": f"Está en {listed}" if len(deposits) == 1 else "Está en varios depósitos",
            "detail": detail,
            "options": options,
        }

    if locations:
        codes = _join(item["articleCode"] for item in locations if item["articleCode"])
        deposits = _join(item["deposit"] for item in locations)
        return {
            **base,
            "verdict": "other_article",
            "title": "Está con otro artículo",
            "detail": (
                f"Bejerman tiene la partida {serial_text} en {deposits}, pero como artículo {codes or 'distinto'}, "
                f"y el equipo tiene el {wanted}. Si el de Bejerman es el correcto, corrija el artículo del "
                "equipo con «Editar»; si no, corrija la partida en Bejerman."
            ),
            "options": [_option("edit", "Editar equipo", "Corregir el artículo del equipo.")],
        }

    return {
        **base,
        "verdict": "not_in_stock",
        "title": "No está en ningún depósito",
        "detail": (
            f"Bejerman no tiene stock de la partida {serial_text} en ningún depósito. Si el equipo ya "
            "salió (con un RSS, un RT o un remito de alquiler), esta operación ya no corresponde: "
            "descártela. Si nunca entró, primero hay que emitir o registrar su remito de ingreso."
        ),
        "options": [
            _option(
                "discard",
                "Descartar",
                "Cierra la operación sin mover nada.",
                reason=f"Bejerman no tiene stock de la partida {serial_text} en ningún depósito.",
            )
        ],
    }


def repeated_serials_by_ingreso(rows: Iterable[Mapping[str, Any]]) -> dict[int, list[str]]:
    """Para cada OS, las otras OS de su mismo remito que tienen cargada la misma serie.

    `rows` son los remitos de ingreso con `ingreso_id`, `numero_serie` y los `ingresoIds`
    del lote en `request_payload`. Devuelve {ingreso_id: ["OS 29881", ...]} sólo para las OS
    con la serie repetida.
    """

    serial_by_ingreso: dict[int, str] = {}
    lotes: list[list[int]] = []
    for row in rows or []:
        try:
            ingreso_id = int(row.get("ingreso_id"))
        except (TypeError, ValueError):
            continue
        serial = _code(row.get("numero_serie"))
        if serial:
            serial_by_ingreso[ingreso_id] = serial
        payload = row.get("request_payload") if isinstance(row.get("request_payload"), Mapping) else {}
        lote: list[int] = []
        for raw in payload.get("ingresoIds") or []:
            try:
                lote.append(int(raw))
            except (TypeError, ValueError):
                continue
        if ingreso_id not in lote:
            lote.append(ingreso_id)
        lotes.append(lote)

    repeated: dict[int, list[str]] = {}
    for lote in lotes:
        by_serial: dict[str, list[int]] = {}
        for ingreso_id in dict.fromkeys(lote):
            serial = serial_by_ingreso.get(ingreso_id)
            if serial:
                by_serial.setdefault(serial, []).append(ingreso_id)
        for ingresos in by_serial.values():
            if len(ingresos) < 2:
                continue
            for ingreso_id in ingresos:
                others = [f"OS {other}" for other in ingresos if other != ingreso_id]
                current = repeated.setdefault(ingreso_id, [])
                current.extend(item for item in others if item not in current)
    return repeated
