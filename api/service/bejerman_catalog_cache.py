"""Cache en memoria del proceso para los volcados completos del SDK de Bejerman.

`ObtenerArticulos`, `ObtenerPartidas` y `ObtenerStockDeposito` ignoran los filtros
que se les mandan y devuelven la tabla entera (ver el comentario en
`BejermanSDKClient.list_articulos`). Sin cache, cada tecla del buscador de
artículos y cada selección de partida dispara una descarga completa por SOAP
dentro del request; con `ATOMIC_REQUESTS` eso además deja una transacción de
Postgres abierta esperando la red, y el parseo del volcado retiene el GIL del
worker, así que degrada también a los requests que no tocan Bejerman.

Se cachea en memoria del proceso y no vía `django.core.cache` a propósito: el
backend configurado es `LocMemCache`, que picklea al guardar y despicklea en cada
lectura, y acá los valores son listas de miles de dicts — serializar en cada hit
se comería buena parte de la ganancia. El alcance efectivo es el mismo en ambos
casos (un cache por worker de gunicorn), así que no se pierde nada a cambio.

Las entradas se toman como de sólo lectura: los consumidores (`build_articles_result`,
`build_article_stock_lots`) construyen dicts nuevos y no mutan la entrada.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from django.conf import settings

from .bejerman_sdk import (
    PartidaIndex,
    article_search_blob,
    build_article_description_map,
    build_partida_rows,
    build_stock_rows,
    map_article_items,
)

logger = logging.getLogger(__name__)

# TTLs por defecto. El catálogo de artículos cambia poco (alta de artículos
# nuevos), así que tolera una ventana amplia. Stock y partidas mandan la
# validación de disponibilidad, así que van cortos: alcanzan para colapsar la
# ráfaga de llamadas de una misma sesión de armado sin ampliar de forma
# apreciable la carrera que ya existe hoy entre validar y emitir el remito.
DEFAULT_ARTICLES_TTL = 300
DEFAULT_PARTIDAS_TTL = 60
DEFAULT_STOCK_TTL = 30


def _ttl(setting_name: str, default: int) -> int:
    try:
        return max(0, int(getattr(settings, setting_name, default)))
    except (TypeError, ValueError):
        return default


class _TTLCache:
    """Cache con TTL y protección contra estampida.

    El lock por clave importa: el patrón real es una ráfaga de requests
    simultáneos sobre la misma clave (varias teclas seguidas, varios usuarios
    armando pedidos a la vez). Sin él, todos los threads fallan el cache al mismo
    tiempo y disparan cada uno su propia descarga completa.
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, Any]] = {}
        self._entries_lock = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}

    def _key_lock(self, key: str) -> threading.Lock:
        with self._entries_lock:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    def _fresh(self, key: str, ttl_seconds: int) -> tuple[bool, Any]:
        with self._entries_lock:
            entry = self._entries.get(key)
        if not entry:
            return False, None
        stored_at, value = entry
        if time.monotonic() - stored_at >= ttl_seconds:
            return False, None
        return True, value

    def get_or_set(
        self,
        key: str,
        ttl_seconds: int,
        producer: Callable[[], Any],
        *,
        force_refresh: bool = False,
    ) -> Any:
        if ttl_seconds <= 0:
            return producer()
        if not force_refresh:
            hit, value = self._fresh(key, ttl_seconds)
            if hit:
                return value
        with self._key_lock(key):
            # Otro thread pudo haber refrescado mientras esperábamos el lock.
            if not force_refresh:
                hit, value = self._fresh(key, ttl_seconds)
                if hit:
                    return value
            value = producer()
            with self._entries_lock:
                self._entries[key] = (time.monotonic(), value)
            return value

    def invalidate(self, prefix: str = "") -> None:
        with self._entries_lock:
            if not prefix:
                self._entries.clear()
                return
            for key in [key for key in self._entries if key.startswith(prefix)]:
                self._entries.pop(key, None)


_article_responses_cache = _TTLCache()
_articles_cache = _TTLCache()
_partidas_cache = _TTLCache()
_stock_cache = _TTLCache()


def _company(company_key: Any) -> str:
    return str(company_key or "").strip().upper() or "SEPID"


def _client_company(client: Any, company_key: Any = None) -> str:
    """Clave de cache de la empresa, tomada del cliente que va a hacer la consulta.

    Se prefiere `client.company_key` por sobre el valor que pasó el llamador
    porque es el que determina contra qué empresa consulta realmente el SDK.
    `BejermanSDKClient` admite construirse con `bejerman_company` y dejar
    `company_key` vacío; en ese caso se usa el código de empresa Bejerman crudo en
    su propio espacio de nombres, para no confundirlo nunca con SEPID por
    defecto — servir el catálogo de otra empresa sería un error silencioso."""
    from_client = str(getattr(client, "company_key", "") or "").strip().upper()
    if from_client:
        return from_client
    explicit = str(company_key or "").strip().upper()
    if explicit:
        return explicit
    company = str(getattr(client, "company", "") or "").strip().upper()
    return f"#{company}" if company else "SEPID"


def _deposit(deposit_code: Any) -> str:
    return str(deposit_code or "").strip().upper()


class ArticleCatalog:
    """Catálogo mapeado más el texto de búsqueda de cada ítem, precalculado."""

    __slots__ = ("items", "blobs")

    def __init__(self, items: list[dict[str, Any]]):
        self.items = items
        self.blobs = [article_search_blob(item) for item in items]


def article_response(client: Any, company_key: Any = None, *, force_refresh: bool = False) -> dict[str, Any]:
    """Respuesta cruda de ObtenerArticulos.

    La necesitan los consumidores que hacen su propio mapeo sobre los registros
    originales (por ejemplo `build_article_sales_vat_map`). Comparte cache con
    `article_catalog`, así que ambos se sirven de una sola descarga."""
    return _article_responses_cache.get_or_set(
        _client_company(client, company_key),
        _ttl("BEJERMAN_ARTICLES_CACHE_SECONDS", DEFAULT_ARTICLES_TTL),
        # Los filtros y el límite se pasan explícitos aunque el SDK los descarte:
        # es la firma que usan el resto de los llamadores.
        lambda: client.list_articulos([], 5000),
        force_refresh=force_refresh,
    )


def article_catalog(client: Any, company_key: Any = None, *, force_refresh: bool = False) -> ArticleCatalog:
    """Catálogo completo de artículos de la empresa, mapeado y deduplicado."""
    return _articles_cache.get_or_set(
        _client_company(client, company_key),
        _ttl("BEJERMAN_ARTICLES_CACHE_SECONDS", DEFAULT_ARTICLES_TTL),
        lambda: ArticleCatalog(
            map_article_items(article_response(client, company_key, force_refresh=force_refresh))
        ),
        force_refresh=force_refresh,
    )


def article_descriptions(client: Any, article_codes: Any, company_key: Any = None) -> dict[str, str]:
    """Descripciones del maestro de Bejerman para esos códigos, del catálogo cacheado.

    Es el dato con el que se imprimen los renglones del remito (ver
    `build_article_description_map`). **Nunca levanta**: si el catálogo no se puede
    bajar se devuelve vacío y cada renglón se queda con el texto que tenía, que es lo
    que se venía imprimiendo; un problema de red no puede frenar una emisión.
    """

    codes = [code for code in (article_codes or []) if str(code or "").strip()]
    if not codes:
        return {}
    try:
        return build_article_description_map(article_response(client, company_key), codes)
    except Exception:
        logger.warning(
            "bejerman_article_descriptions_unavailable",
            extra={"company_key": str(_client_company(client, company_key))},
            exc_info=True,
        )
        return {}


def article_catalog_if_cached(company_key: Any) -> ArticleCatalog | None:
    """El catálogo sólo si ya está en memoria y vigente; nunca sale a la red.

    Lo usan los caminos donde resolver el dato es deseable pero no vale bloquear
    al usuario (por ejemplo, abrir el detalle de una orden): si el catálogo está
    frío se devuelve None y el llamador sigue con el dato sin resolver, igual que
    haría si Bejerman estuviera caído."""
    hit, value = _articles_cache._fresh(  # noqa: SLF001 - mismo módulo
        _company(company_key),
        _ttl("BEJERMAN_ARTICLES_CACHE_SECONDS", DEFAULT_ARTICLES_TTL),
    )
    return value if hit else None


class PartidaCatalog:
    """Volcado de partidas más su índice de búsqueda, precalculado.

    El índice se arma junto con la descarga y no bajo demanda para que ambos
    salgan siempre de la misma entrada de cache: cachearlos por separado los
    dejaría vencer en momentos distintos y duplicaría las descargas."""

    __slots__ = ("rows", "index")

    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows
        self.index = PartidaIndex(rows)


def _partida_catalog(client: Any, company_key: Any = None, *, force_refresh: bool = False) -> PartidaCatalog:
    return _partidas_cache.get_or_set(
        _client_company(client, company_key),
        _ttl("BEJERMAN_PARTIDAS_CACHE_SECONDS", DEFAULT_PARTIDAS_TTL),
        lambda: PartidaCatalog(build_partida_rows(client.obtener_partidas())),
        force_refresh=force_refresh,
    )


def partida_rows(client: Any, company_key: Any = None, *, force_refresh: bool = False) -> list[dict[str, Any]]:
    """Todas las partidas de la empresa, ya mapeadas."""
    return _partida_catalog(client, company_key, force_refresh=force_refresh).rows


def partida_index(client: Any, company_key: Any = None, *, force_refresh: bool = False) -> PartidaIndex:
    """Las mismas partidas, indexadas por depósito y número de partida."""
    return _partida_catalog(client, company_key, force_refresh=force_refresh).index


def partida_index_if_cached(company_key: Any) -> PartidaIndex | None:
    """Índice de partidas vigente, sin iniciar una consulta remota.

    La selección interactiva de partidas necesita primero saber qué stock hay.
    La fecha de vencimiento es enriquecimiento opcional: si la caché de partidas
    está fría, descargar toda la tabla para completarla no debe demorar ni
    bloquear esa consulta.
    """
    hit, catalog = _partidas_cache._fresh(  # noqa: SLF001 - mismo módulo
        _company(company_key),
        _ttl("BEJERMAN_PARTIDAS_CACHE_SECONDS", DEFAULT_PARTIDAS_TTL),
    )
    return catalog.index if hit else None


def stock_rows(client: Any, company_key: Any, deposit_code: Any, *, force_refresh: bool = False) -> list[dict[str, Any]]:
    """Stock completo de un depósito, ya mapeado."""
    company = _client_company(client, company_key)
    deposit = _deposit(deposit_code)
    return _stock_cache.get_or_set(
        f"{company}|{deposit}",
        _ttl("BEJERMAN_STOCK_CACHE_SECONDS", DEFAULT_STOCK_TTL),
        lambda: build_stock_rows(client.obtener_stock_deposito(deposit)),
        force_refresh=force_refresh,
    )


def invalidate_stock(company_key: Any = None) -> None:
    """Descarta el stock cacheado después de un movimiento que lo modifica.

    Se llama al emitir remitos: el stock que quedó en cache ya no refleja lo que
    hay en Bejerman y la próxima orden se validaría contra números viejos."""
    if company_key is None:
        _stock_cache.invalidate()
        _partidas_cache.invalidate()
        return
    company = _company(company_key)
    _stock_cache.invalidate(f"{company}|")
    _partidas_cache.invalidate(company)


def invalidate_all() -> None:
    _article_responses_cache.invalidate()
    _articles_cache.invalidate()
    _partidas_cache.invalidate()
    _stock_cache.invalidate()
