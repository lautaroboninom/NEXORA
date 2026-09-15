"""Alta masiva de equipos propios para el parque de alquiler.

Es el alta con "Equipo propio" del modal de Equipos, para muchas unidades de una vez: un tipo,
una marca y un modelo, y una lista de números de serie con su MG. Cada unidad tiene que estar en
el stock de Bejerman —en el depósito de origen (VAL o SAR) o ya en STL— y las que están en el origen se
transfieren a STL en UN lote: una SAL y una ENT para todas, en vez de dos comprobantes por equipo
(ver `enqueue_stock_val_to_stl_lote`).

La validación corre dos veces a propósito: una para mostrarle al operador fila por fila qué va a
pasar, sin grabar nada, y otra al confirmar, porque entre las dos pudo cambiar la base. Si una sola
fila falla no se da de alta ninguna: un lote a medias obliga a alguien a reconstruir a mano qué
entró y qué no.
"""
import logging
import re

from django.conf import settings
from django.db import IntegrityError, transaction
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from ..bejerman_catalog_cache import article_descriptions, stock_rows as cached_stock_rows
from ..bejerman_companies import list_ingress_companies
from ..bejerman_sdk import (
    BejermanSDKClient,
    BejermanSdkConfigError,
    BejermanSdkResponseError,
    BejermanSdkUnavailable,
    as_string,
)
from ..bejerman_sync import (
    STOCK_LOTE_MAX_ITEMS,
    BejermanBlockedError,
    BejermanConfigError,
    _mapping_for_context,
    enqueue_stock_val_to_stl_lote,
)
from ..permissions import MappedPermissionGuard
from .devices_views import (
    MG_KEY_PATTERN,
    MG_KEY_SQL,
    RENTAL_SHELF_LOCATION_NAME,
    _MG_CANONICAL_RE,
    _alta_propio_source_deposits,
    _bejerman_company_key_or_default,
    _has_table_column,
    _mg_owner_customer_id,
    _normalize_mg,
    _parse_int_or_none,
    _rental_shelf_location_id,
)
from .helpers import _set_audit_user, exec_returning, q, require_roles
from .tipo_equipo_utils import sentence_case_tipo_equipo

logger = logging.getLogger(__name__)

ALTA_MASIVA_MAX_FILAS = STOCK_LOTE_MAX_ITEMS
ACCION_TRANSFERIR = "transferir"
ACCION_YA_EN_DESTINO = "ya_en_destino"
ROLES_ALTA = ["jefe", "jefe_veedor", "admin"]

# Excel muestra en notación científica los números largos de una columna con formato General:
# "210000708153" termina como "2,1E+11" y la serie original ya no se puede recuperar del archivo.
_NOTACION_CIENTIFICA_RE = re.compile(r"^\d+(?:[.,]\d+)?E[+-]\d+$", re.IGNORECASE)

BEJERMAN_NO_DISPONIBLE = (
    BejermanSdkConfigError,
    BejermanSdkUnavailable,
    BejermanSdkResponseError,
    BejermanConfigError,
)


def _ingresos_views():
    # Import diferido, como en DeviceStockCandidatosView: `ingresos_views` es pesado.
    from . import ingresos_views

    return ingresos_views


def _clave_serie(value) -> str:
    """La misma normalización que `uq_devices_ns_tipo_norm` y `_norm_serial_key`."""
    return str(value or "").upper().replace(" ", "").replace("-", "").strip()


def _norm_code(value) -> str:
    return str(value or "").strip().upper()


def _renglones(numeros) -> str:
    numeros = [str(numero) for numero in numeros]
    if len(numeros) == 1:
        return f"la fila {numeros[0]}"
    return "las filas " + ", ".join(numeros[:-1]) + f" y {numeros[-1]}"


def _target_deposit() -> str:
    return str(getattr(settings, "BEJERMAN_TARGET_DEPOSIT", "STL") or "STL").strip().upper() or "STL"


def _source_deposits() -> list[str]:
    target = _target_deposit()
    return [deposito for deposito in _alta_propio_source_deposits() if deposito != target]


class _PedidoInvalido(Exception):
    def __init__(self, detail: str, conflict_type: str, status: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.conflict_type = conflict_type
        self.status = status

    def response(self):
        return Response({"detail": self.detail, "conflict_type": self.conflict_type}, status=self.status)


def _leer_filas(raw) -> list[dict]:
    """Las filas tal como las manda el front: `{fila, numero_serie, numero_interno}`.

    `fila` es el renglón de la lista que ve el operador; se conserva para que cada error le señale
    la línea que tiene que corregir.
    """
    filas = []
    for index, entry in enumerate(raw if isinstance(raw, list) else [], start=1):
        if isinstance(entry, dict):
            numero_serie = entry.get("numero_serie")
            numero_interno = entry.get("numero_interno")
            fila = _parse_int_or_none(entry.get("fila")) or index
        elif isinstance(entry, (list, tuple)):
            numero_serie = entry[0] if len(entry) > 0 else ""
            numero_interno = entry[1] if len(entry) > 1 else ""
            fila = index
        else:
            continue
        filas.append(
            {
                "fila": fila,
                "numero_serie": str(numero_serie or "").strip(),
                "numero_interno": str(numero_interno or "").strip(),
            }
        )
    return filas


def _leer_cabecera(data) -> dict:
    company_key = _bejerman_company_key_or_default(data.get("empresa") or data.get("company_key"))
    if not company_key:
        raise _PedidoInvalido("Empresa Bejerman no válida.", "EMPRESA_INVALIDA")
    target = _target_deposit()
    source = str(data.get("stock_deposito") or "VAL").strip().upper()
    if source not in _source_deposits():
        raise _PedidoInvalido(
            f"El depósito {source} no es un origen válido para un alta de equipos propios.",
            "DEPOSITO_INVALIDO",
        )
    tipo_equipo = sentence_case_tipo_equipo(data.get("tipo_equipo"))
    if not tipo_equipo:
        raise _PedidoInvalido("Elegí el tipo de equipo.", "TIPO_REQUERIDO")
    marca_id = _parse_int_or_none(data.get("marca_id"))
    model_id = _parse_int_or_none(data.get("model_id"))
    if not marca_id or not model_id:
        raise _PedidoInvalido("Elegí la marca y el modelo.", "MODELO_REQUERIDO")
    model_variante_sql = "COALESCE(m.variante,'')" if _has_table_column("models", "variante") else "''"
    model = q(
        f"""
        SELECT m.id,
               m.marca_id,
               COALESCE(m.nombre,'') AS modelo,
               COALESCE(m.tipo_equipo,'') AS tipo_equipo,
               {model_variante_sql} AS variante,
               COALESCE(b.nombre,'') AS marca
          FROM models m
          LEFT JOIN marcas b ON b.id = m.marca_id
         WHERE m.id = %s
        """,
        [model_id],
        one=True,
    )
    if not model:
        raise _PedidoInvalido("El modelo elegido no existe.", "MODELO_INEXISTENTE")
    if model.get("marca_id") is not None and int(model["marca_id"]) != marca_id:
        raise _PedidoInvalido("El modelo elegido no pertenece a esa marca.", "MODELO_DE_OTRA_MARCA")
    tipo_modelo = str(model.get("tipo_equipo") or "").strip()
    if tipo_modelo and tipo_modelo.lower() != tipo_equipo.lower():
        raise _PedidoInvalido(
            f"El modelo {model['modelo']} es de tipo {tipo_modelo}, no {tipo_equipo}.",
            "MODELO_DE_OTRO_TIPO",
        )
    customer_id = _mg_owner_customer_id()
    if not customer_id:
        raise _PedidoInvalido(
            "No hay un cliente MG BIO cargado en el sistema: sin él no se puede registrar un equipo propio.",
            "MG_OWNER_MISSING",
        )
    ubicacion_id = _rental_shelf_location_id()
    if not ubicacion_id:
        raise _PedidoInvalido(
            f"No existe la ubicación «{RENTAL_SHELF_LOCATION_NAME}»: no se puede dejar el equipo "
            "en la estantería de alquiler.",
            "RENTAL_SHELF_MISSING",
        )
    return {
        "company_key": company_key,
        "source": source,
        "target": target,
        "tipo_equipo": tipo_equipo,
        "marca_id": marca_id,
        "model_id": model_id,
        "variante": str(data.get("variante") or "").strip(),
        "model_variante": str(model.get("variante") or "").strip(),
        "modelo_label": " ".join(p for p in (model.get("marca"), model.get("modelo")) if p) or f"el modelo {model_id}",
        "customer_id": customer_id,
        "ubicacion_id": ubicacion_id,
    }


def _articulos_del_modelo(cabecera) -> tuple[str, str]:
    """(confirmado a mano, mapeado) del modelo del lote, con la misma consulta que el worker.

    Es lo que mira `_mapping_article_codes_for_job` al ejecutar la transferencia. Si la validación
    mirara otra cosa, el alta pasaría y el movimiento de stock se bloquearía después, en el panel,
    con los equipos ya creados.
    """
    context = {
        "model_id": cabecera["model_id"],
        "variante": cabecera["variante"] or cabecera["model_variante"],
        "empresa_bejerman": cabecera["company_key"],
        "empresa_facturar": cabecera["company_key"],
    }
    mapping = _mapping_for_context(context, cabecera["company_key"]) or {}
    code = str(mapping.get("article_code") or "").strip()
    manual = str(mapping.get("match_source") or "").strip().lower() == "manual"
    return (code if manual else ""), code


def _contexto_stock(cabecera, filas, actor_user_id, *, refrescar: bool) -> dict:
    """Lo que Bejerman sabe de las series pedidas: renglones con stock en origen y destino."""
    ingresos = _ingresos_views()
    prefix = ingresos._swap_equipment_article_prefix().upper()
    claves = {_clave_serie(fila["numero_serie"]) for fila in filas if fila["numero_serie"]}
    company_key = cabecera["company_key"]
    client = BejermanSDKClient(company_key=company_key, actor_user_id=actor_user_id)
    por_serie: dict[str, list[dict]] = {}
    codigos = set()
    for deposito in (cabecera["source"], cabecera["target"]):
        for row in cached_stock_rows(client, company_key, deposito, force_refresh=refrescar) or []:
            partida = as_string(row.get("partida")).strip()
            article_code = as_string(row.get("articleCode")).strip()
            if not partida or not article_code.upper().startswith(prefix):
                continue
            clave = _clave_serie(partida)
            if clave not in claves:
                continue
            cantidad = row.get("availableQuantity")
            if cantidad is None:
                cantidad = row.get("realQuantity")
            if cantidad is None or cantidad <= 0:
                continue
            por_serie.setdefault(clave, []).append(
                {
                    "article_code": article_code,
                    "deposito": as_string(row.get("depositCode")).strip().upper() or deposito,
                    "partida": partida,
                    "cantidad": cantidad,
                }
            )
            codigos.add(article_code)
    descripciones = {}
    if codigos:
        try:
            descripciones = article_descriptions(client, sorted(codigos), company_key) or {}
        except Exception:
            # El catálogo es opcional: sin él la fila se muestra igual con el código.
            logger.exception("No se pudieron leer las descripciones de artículos")
    modelos = ingresos._swap_article_model_map(sorted(codigos)) if codigos else {}
    confirmado, mapeado = _articulos_del_modelo(cabecera)
    return {
        "stock_por_serie": por_serie,
        "descripciones": descripciones,
        "modelos_por_articulo": {_norm_code(code): row for code, row in (modelos or {}).items()},
        "articulo_confirmado": confirmado,
        "articulo_mapeado": mapeado,
    }


def _series_existentes(filas, tipo_equipo) -> dict:
    claves = sorted({_clave_serie(fila["numero_serie"]) for fila in filas if fila["numero_serie"]})
    if not claves:
        return {}
    rows = q(
        """
        SELECT id, REPLACE(REPLACE(UPPER(numero_serie),' ',''),'-','') AS clave
          FROM devices
         WHERE REPLACE(REPLACE(UPPER(numero_serie),' ',''),'-','') = ANY(%s)
           AND LOWER(TRIM(COALESCE(tipo_equipo,''))) = %s
        """,
        [claves, tipo_equipo.strip().lower()],
    ) or []
    return {row["clave"]: row["id"] for row in rows}


def _mg_existentes(filas) -> dict:
    valores = sorted({_normalize_mg(fila["numero_interno"]) for fila in filas if fila["numero_interno"]})
    if not valores:
        return {}
    # La comparación vive en SQL, con la misma clave que el alta de a uno (`MG_KEY_SQL`), para que
    # `MG 1234` y `MG 001234` choquen igual contra lo ya guardado.
    rows = q(
        f"""
        SELECT e.mg, MIN(d.id) AS id
          FROM unnest(%s::text[]) AS e(mg)
          JOIN devices d
            ON d.numero_interno ~* '{MG_KEY_PATTERN}'
           AND {MG_KEY_SQL.format(col="d.numero_interno")} = {MG_KEY_SQL.format(col="e.mg")}
         GROUP BY e.mg
        """,
        [valores],
    ) or []
    return {row["mg"]: row["id"] for row in rows}


def _elegir_articulo(
    candidatos,
    *,
    deposito,
    cabecera,
    articulo_confirmado,
    articulo_mapeado,
    modelos_por_articulo,
    descripciones,
):
    """El artículo que se va a mover, con las mismas reglas que el worker. (código, error, aviso)

    El artículo confirmado a mano para el modelo es el único que veta
    (`_assert_source_article_matches_confirmed_mapping`). Con varios artículos para la misma
    partida —el CPAP y su humidificador comparten serie— desempata el mapeo del modelo.
    """
    codigos = list(dict.fromkeys(r["article_code"] for r in candidatos if r.get("article_code")))
    modelo_label = cabecera["modelo_label"]

    def etiqueta(code):
        descripcion = str(descripciones.get(code) or "").strip()
        return f"{code} ({descripcion})" if descripcion else code

    if articulo_confirmado:
        for code in codigos:
            if _norm_code(code) == _norm_code(articulo_confirmado):
                return code, "", ""
        return (
            "",
            f"En {deposito} la partida es el artículo {', '.join(etiqueta(c) for c in codigos)}, pero "
            f"{modelo_label} tiene confirmado el artículo {articulo_confirmado}: revisá que el modelo "
            "sea el correcto.",
            "",
        )
    if len(codigos) > 1:
        for code in codigos:
            if articulo_mapeado and _norm_code(code) == _norm_code(articulo_mapeado):
                return code, "", ""
        for code in codigos:
            mapeo = modelos_por_articulo.get(_norm_code(code)) or {}
            if mapeo.get("model_id") and int(mapeo["model_id"]) == int(cabecera["model_id"]):
                return code, "", ""
        return (
            "",
            f"En {deposito} la partida tiene stock en varios artículos "
            f"({', '.join(etiqueta(c) for c in codigos)}) y ninguno está asociado a {modelo_label}.",
            "",
        )
    code = codigos[0]
    mapeo = modelos_por_articulo.get(_norm_code(code)) or {}
    if not mapeo:
        return code, "", f"El artículo {etiqueta(code)} no está asociado a ningún modelo."
    if mapeo.get("model_id") and int(mapeo["model_id"]) != int(cabecera["model_id"]):
        otro = " ".join(p for p in (mapeo.get("marca"), mapeo.get("modelo")) if p) or "otro modelo"
        return code, "", f"El artículo {etiqueta(code)} está asociado a {otro}: revisá que {modelo_label} sea el correcto."
    return code, "", ""


def evaluar_filas(
    filas,
    *,
    cabecera,
    stock_por_serie,
    descripciones,
    modelos_por_articulo,
    articulo_confirmado,
    articulo_mapeado,
    series_existentes,
    mg_existentes,
):
    """Qué pasa con cada fila. Función pura: todo lo que sabe de la base y de Bejerman le llega."""
    source = cabecera["source"]
    target = cabecera["target"]
    filas_por_serie: dict[str, list] = {}
    filas_por_mg: dict[str, list] = {}
    for fila in filas:
        clave = _clave_serie(fila["numero_serie"])
        mg = _normalize_mg(fila["numero_interno"])
        if clave:
            filas_por_serie.setdefault(clave, []).append(fila["fila"])
        if mg:
            filas_por_mg.setdefault(mg, []).append(fila["fila"])

    resultado = []
    for fila in filas:
        errores: list[str] = []
        avisos: list[str] = []
        numero_serie = fila["numero_serie"]
        mg_original = fila["numero_interno"]
        clave = _clave_serie(numero_serie)
        mg = _normalize_mg(mg_original)
        cientifica = bool(numero_serie) and bool(_NOTACION_CIENTIFICA_RE.match(numero_serie.replace(" ", "")))

        if not numero_serie:
            errores.append("Falta el número de serie.")
        elif cientifica:
            errores.append(
                "La serie quedó en notación científica (Excel la convirtió en número): cargala con la "
                "columna en formato Texto."
            )
        if not mg_original:
            errores.append("Falta el número interno (MG).")
        elif not _MG_CANONICAL_RE.match(mg):
            errores.append(f"«{mg_original}» no es un número interno: se espera algo como MG 1234.")
        otras_series = [n for n in filas_por_serie.get(clave, []) if n != fila["fila"]]
        if clave and otras_series:
            errores.append(f"La serie está repetida en la lista (también en {_renglones(otras_series)}).")
        otros_mg = [n for n in filas_por_mg.get(mg, []) if n != fila["fila"]]
        if mg and otros_mg:
            errores.append(f"El {mg} está repetido en la lista (también en {_renglones(otros_mg)}).")
        if clave and clave in series_existentes:
            errores.append(
                f"Ya hay un equipo {cabecera['tipo_equipo']} con esta serie (ficha #{series_existentes[clave]})."
            )
        if mg and mg in mg_existentes:
            errores.append(f"El {mg} ya está asignado a otro equipo (ficha #{mg_existentes[mg]}).")

        accion = ""
        article_code = ""
        deposito = ""
        if clave and not cientifica:
            renglones = stock_por_serie.get(clave) or []
            en_origen = [r for r in renglones if _norm_code(r["deposito"]) == source]
            en_destino = [r for r in renglones if _norm_code(r["deposito"]) == target]
            candidatos = en_origen or en_destino
            deposito_candidato = source if en_origen else target
            if not candidatos:
                errores.append(f"No tiene stock en {source} ni en {target} en Bejerman.")
            else:
                article_code, error_articulo, aviso_articulo = _elegir_articulo(
                    candidatos,
                    deposito=deposito_candidato,
                    cabecera=cabecera,
                    articulo_confirmado=articulo_confirmado,
                    articulo_mapeado=articulo_mapeado,
                    modelos_por_articulo=modelos_por_articulo,
                    descripciones=descripciones,
                )
                if error_articulo:
                    errores.append(error_articulo)
                elif en_origen and any(_norm_code(r["article_code"]) == _norm_code(article_code) for r in en_destino):
                    errores.append(
                        f"La partida figura con stock en {source} y en {target} a la vez: corregí el stock en "
                        "Bejerman antes de darla de alta."
                    )
                else:
                    if aviso_articulo:
                        avisos.append(aviso_articulo)
                    deposito = deposito_candidato
                    accion = ACCION_TRANSFERIR if en_origen else ACCION_YA_EN_DESTINO
                    if accion == ACCION_YA_EN_DESTINO:
                        avisos.append(f"Ya está en {target}: no se mueve stock.")

        resultado.append(
            {
                "fila": fila["fila"],
                "numero_serie": numero_serie,
                "numero_interno": mg or mg_original,
                "estado": "error" if errores else "ok",
                "errores": errores,
                "avisos": avisos,
                "accion": accion,
                "article_code": article_code if accion else "",
                "descripcion": str(descripciones.get(article_code) or "") if accion else "",
                "deposito": deposito,
            }
        )

    ok = [r for r in resultado if r["estado"] == "ok"]
    return {
        "filas": resultado,
        "resumen": {
            "total": len(resultado),
            "ok": len(ok),
            "con_error": len(resultado) - len(ok),
            "transferir": sum(1 for r in ok if r["accion"] == ACCION_TRANSFERIR),
            "ya_en_destino": sum(1 for r in ok if r["accion"] == ACCION_YA_EN_DESTINO),
            "con_aviso": sum(1 for r in resultado if r["avisos"]),
        },
    }


def _evaluar_con_base(cabecera, filas, contexto) -> dict:
    return evaluar_filas(
        filas,
        cabecera=cabecera,
        series_existentes=_series_existentes(filas, cabecera["tipo_equipo"]),
        mg_existentes=_mg_existentes(filas),
        **contexto,
    )


def _actor_id(request):
    return getattr(getattr(request, "user", None), "id", None) or getattr(request, "user_id", None)


def _bejerman_no_disponible(exc):
    # Sin Bejerman no hay validación posible: se dice por qué, en vez de mostrar todo en error.
    return Response({"detail": str(exc), "conflict_type": "BEJERMAN_UNAVAILABLE"}, status=502)


class _AltaMasivaBase(APIView):
    permission_classes = [permissions.IsAuthenticated, MappedPermissionGuard]

    def _preparar(self, request):
        require_roles(request, ROLES_ALTA)
        data = request.data or {}
        cabecera = _leer_cabecera(data)
        filas = _leer_filas(data.get("filas"))
        if not filas:
            raise _PedidoInvalido("La lista no tiene ninguna fila con número de serie y MG.", "SIN_FILAS")
        if len(filas) > ALTA_MASIVA_MAX_FILAS:
            raise _PedidoInvalido(
                f"Cada alta admite como máximo {ALTA_MASIVA_MAX_FILAS} equipos: dividí la lista.",
                "DEMASIADAS_FILAS",
            )
        return cabecera, filas


class DeviceAltaMasivaValidarView(_AltaMasivaBase):
    """Valida una alta masiva sin grabar nada: qué pasa con cada fila."""

    def post(self, request):
        try:
            cabecera, filas = self._preparar(request)
        except _PedidoInvalido as exc:
            return exc.response()
        try:
            contexto = _contexto_stock(cabecera, filas, _actor_id(request), refrescar=False)
        except BEJERMAN_NO_DISPONIBLE as exc:
            return _bejerman_no_disponible(exc)
        resultado = _evaluar_con_base(cabecera, filas, contexto)
        return Response({**resultado, "origen": cabecera["source"], "destino": cabecera["target"]})


class DeviceAltaMasivaView(_AltaMasivaBase):
    """Alta masiva de equipos propios: crea los equipos y encola la transferencia en un lote."""

    def get(self, request):
        require_roles(request, ROLES_ALTA)
        return Response(
            {
                "empresas": [
                    {"key": company.key, "label": company.label}
                    for company in list_ingress_companies()
                    if not company.is_test
                ],
                "depositos": _source_deposits(),
                "deposito_origen": "VAL",
                "deposito_destino": _target_deposit(),
                "max_filas": ALTA_MASIVA_MAX_FILAS,
            }
        )

    def post(self, request):
        try:
            cabecera, filas = self._preparar(request)
        except _PedidoInvalido as exc:
            return exc.response()
        actor_user_id = _actor_id(request)
        # El stock se lee FUERA de la transacción: no se llama a Bejerman con la base tomada. Se
        # fuerza una lectura fresca porque desde la validación pudo moverse; igual el worker vuelve
        # a mirar cada partida antes de emitir.
        try:
            contexto = _contexto_stock(cabecera, filas, actor_user_id, refrescar=True)
        except BEJERMAN_NO_DISPONIBLE as exc:
            return _bejerman_no_disponible(exc)

        try:
            with transaction.atomic():
                _set_audit_user(request)
                resultado = _evaluar_con_base(cabecera, filas, contexto)
                con_error = resultado["resumen"]["con_error"]
                if con_error:
                    transaction.set_rollback(True)
                    return Response(
                        {
                            "detail": (
                                f"Hay {con_error} {'fila' if con_error == 1 else 'filas'} con error: "
                                "no se dio de alta ningún equipo."
                            ),
                            "conflict_type": "ALTA_MASIVA_CON_ERRORES",
                            **resultado,
                        },
                        status=400,
                    )
                creados = []
                for fila in resultado["filas"]:
                    device_id = exec_returning(
                        """
                        INSERT INTO devices(
                          customer_id, marca_id, model_id, numero_serie, numero_interno,
                          tipo_equipo, variante, ubicacion_id, alquilado, alquiler_a
                        ) VALUES (%s, %s, %s, %s, %s, %s, NULLIF(%s,''), %s, FALSE, NULL)
                        RETURNING id
                        """,
                        [
                            cabecera["customer_id"],
                            cabecera["marca_id"],
                            cabecera["model_id"],
                            fila["numero_serie"],
                            fila["numero_interno"],
                            cabecera["tipo_equipo"],
                            cabecera["variante"],
                            cabecera["ubicacion_id"],
                        ],
                    )
                    creados.append(
                        {
                            "id": device_id,
                            "fila": fila["fila"],
                            "numero_serie": fila["numero_serie"],
                            "numero_interno": fila["numero_interno"],
                            "accion": fila["accion"],
                            "article_code": fila["article_code"],
                        }
                    )
                lote = enqueue_stock_val_to_stl_lote(
                    [creado["id"] for creado in creados],
                    company_key=cabecera["company_key"],
                    source_deposit=cabecera["source"],
                    article_codes={creado["id"]: creado["article_code"] for creado in creados},
                    actor_user_id=actor_user_id,
                )
        except BejermanBlockedError as exc:
            return Response(
                {"detail": f"No se dio de alta ningún equipo: {exc}", "conflict_type": "LOTE_NO_ENCOLADO"},
                status=400,
            )
        except IntegrityError:
            return Response(
                {
                    "detail": (
                        "Otro usuario cargó una de estas series o MG mientras tanto: no se dio de alta "
                        "ningún equipo. Volvé a validar la lista."
                    ),
                    "conflict_type": "ALTA_MASIVA_CONFLICTO",
                },
                status=409,
            )

        resumen = resultado["resumen"]
        return Response(
            {
                "ok": True,
                "creados": len(creados),
                "transferir": resumen["transferir"],
                "ya_en_destino": resumen["ya_en_destino"],
                "devices": creados,
                "stock_lote": {
                    "id": lote["lote_id"],
                    "reference": lote["reference"],
                    "source_deposit": lote["source_deposit"],
                    "target_deposit": lote["target_deposit"],
                    "jobs": len(lote["jobs"]),
                },
            },
            status=201,
        )


__all__ = [
    "DeviceAltaMasivaView",
    "DeviceAltaMasivaValidarView",
    "evaluar_filas",
]
