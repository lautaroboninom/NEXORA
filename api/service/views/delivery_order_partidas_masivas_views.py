"""Validación de la carga masiva de partidas de una orden de entrega.

Junta lo que la decisión fila por fila necesita saber —el stock del depósito en Bejerman, los MG de
la lista y, en alquiler, si cada NS se puede alquilar— y la deja en `evaluar_partidas`
(`service/delivery_order_partidas_masivas.py`). No graba nada: el formulario carga las partidas que
pasan y el guardado de la orden las vuelve a validar.
"""
from __future__ import annotations

import logging

from django.db import connection
from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .. import bejerman_catalog_cache
from ..bejerman_delivery import _interactive_request_timeout, _normalize_delivery_company_key
from ..bejerman_sdk import BejermanSDKClient, _temporary_request_timeout
from ..delivery_order_partidas_masivas import (
    PARTIDAS_MASIVAS_MAX_FILAS,
    es_numero_interno,
    evaluar_partidas,
    leer_cargadas,
    leer_filas,
)
from ..delivery_orders import (
    RENTAL_STOCK_DEPOSIT,
    DeliveryOrderError,
    normalize_delivery_type,
    remito_profile_for_type,
    rental_stock_lot_availability,
)
from ..permissions import MappedPermissionGuard
from .devices_views import MG_KEY_PATTERN, MG_KEY_SQL

logger = logging.getLogger(__name__)


def _actor_id(request):
    return getattr(getattr(request, "user", None), "id", None)


def _pedido_invalido(detail: str, code: str, status: int = 400):
    return Response({"detail": detail, "code": code}, status=status)


def _series_por_numero_interno(textos: list[str]) -> dict[str, list[str]]:
    """Las series de los equipos de cada MG de la lista, con la clave del alta de equipos.

    `MG_KEY_SQL` en los dos lados hace que `MG 1234`, `mg1234` y `MG 001234` sean el mismo número.
    """
    textos = sorted({texto for texto in textos if es_numero_interno(texto)})
    if not textos:
        return {}
    with connection.cursor() as cur:
        cur.execute(
            f"""
            SELECT e.mg, d.numero_serie
              FROM unnest(%s::text[]) AS e(mg)
              JOIN devices d
                ON d.numero_interno ~* '{MG_KEY_PATTERN}'
               AND {MG_KEY_SQL.format(col="d.numero_interno")} = {MG_KEY_SQL.format(col="e.mg")}
             WHERE COALESCE(TRIM(d.numero_serie), '') <> ''
             ORDER BY d.id
            """,
            [textos],
        )
        rows = cur.fetchall()
    series: dict[str, list[str]] = {}
    for texto, serie in rows:
        series.setdefault(texto, []).append(str(serie).strip())
    return series


class DeliveryOrderPartidasMasivasValidarView(APIView):
    """Qué pasa con cada partida de una lista pegada, sin grabar nada."""

    permission_classes = [permissions.IsAuthenticated, MappedPermissionGuard]

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        try:
            delivery_type = normalize_delivery_type(data.get("deliveryType"))
        except DeliveryOrderError as exc:
            return _pedido_invalido(str(exc), exc.code)
        if delivery_type == "service_release":
            # La partida de una reparación es la serie del equipo del cliente: no se elige del stock.
            return _pedido_invalido(
                "La carga masiva no aplica a una reparación: cada partida es la serie de su equipo.",
                "BULK_PARTIDAS_NOT_APPLICABLE",
            )
        article_code = str(data.get("articleCode") or "").strip()
        if not article_code:
            return _pedido_invalido("Elegí el artículo del renglón antes de validar las partidas.", "ARTICLE_CODE_REQUIRED")
        filas = leer_filas(data.get("filas"))
        if not any(fila["partida"] for fila in filas):
            return _pedido_invalido("La lista no tiene ninguna partida.", "BULK_PARTIDAS_EMPTY")
        if len(filas) > PARTIDAS_MASIVAS_MAX_FILAS:
            return _pedido_invalido(
                f"Cada carga admite como máximo {PARTIDAS_MASIVAS_MAX_FILAS} partidas: dividí la lista.",
                "BULK_PARTIDAS_TOO_MANY",
            )
        company_key = _normalize_delivery_company_key(data.get("companyKey"))
        # El alquiler sale siempre de STL: es contra lo que valida su guardado.
        deposit_code = (
            RENTAL_STOCK_DEPOSIT
            if delivery_type == "rental"
            else (str(data.get("depositCode") or "").strip() or remito_profile_for_type(delivery_type).deposit_code).upper()
        )
        order_id = str(data.get("orderId") or "").strip() or None

        try:
            client = BejermanSDKClient(
                company_key=company_key,
                actor_user_id=_actor_id(request),
                allow_system_credentials=True,
            )
            with _temporary_request_timeout(client, _interactive_request_timeout()):
                stock_rows = bejerman_catalog_cache.stock_rows(client, company_key, deposit_code)
        except Exception:
            logger.warning("delivery_order_bulk_partidas_stock_unavailable", exc_info=True)
            return Response(
                {
                    "detail": f"No se pudo leer el stock de {deposit_code} en Bejerman. Reintentá en unos minutos.",
                    "code": "BEJERMAN_STOCK_UNAVAILABLE",
                },
                status=503,
            )

        def descripciones(codes):
            # Nunca levanta: sin catálogo el error muestra sólo el código.
            with _temporary_request_timeout(client, _interactive_request_timeout()):
                return bejerman_catalog_cache.article_descriptions(client, codes, company_key)

        # El vencimiento es un dato de más: sólo si las partidas ya están en memoria, como el
        # recuadro de partidas disponibles.
        partidas_cacheadas = bejerman_catalog_cache.partida_index_if_cached(company_key)

        def vencimiento(deposito, partida):
            return (partidas_cacheadas.lookup(deposito, partida) or {}).get("expirationDate")

        resultado = evaluar_partidas(
            filas,
            article_code=article_code,
            deposit_code=deposit_code,
            stock_rows=stock_rows,
            cargadas=leer_cargadas(data.get("cargadas")),
            series_por_numero_interno=_series_por_numero_interno([fila["partida"] for fila in filas]),
            descripciones=descripciones,
            disponibilidad_alquiler=(
                (lambda series: rental_stock_lot_availability(series, order_id)) if delivery_type == "rental" else None
            ),
            vencimiento=vencimiento if partidas_cacheadas else None,
            checked_at=timezone.now().isoformat(),
        )
        return Response(
            {
                **resultado,
                "articleCode": article_code,
                "depositCode": deposit_code,
                "companyKey": company_key,
                "maxFilas": PARTIDAS_MASIVAS_MAX_FILAS,
            }
        )


__all__ = ["DeliveryOrderPartidasMasivasValidarView"]
