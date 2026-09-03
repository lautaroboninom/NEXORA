"""Backfill de las entregas de reparaciones que quedaron a medio registrar.

La entrega de un equipo reparado se escribe en dos lados: la **orden de entrega**
(`delivery_orders`, tipo `service_release`) y la **hoja de servicio** (`ingresos`).
Hasta el 02/09/2026 la carga manual de un RSS ya emitido en Bejerman escribia solo el
primero, asi que la OS se quedaba en `liberado` sin `fecha_entrega`. Eso ya esta
arreglado en el circuito; lo que falta es corregir lo que quedo atras.

Tres desfasajes, tres fases, dry-run por defecto y en este orden a proposito:

- `os`       la orden tiene el remito y la hoja de servicio sigue abierta. Es el caso que
             dejo el bug; se cierra con el mismo helper que usa el circuito.
- `ordenes`  al reves: la hoja de servicio ya cerro y la orden sigue en la cola de
             Recepcion. Solo se cierran las que **quedaron colgadas**: las que tienen el
             remito cargado y esperan el boton "Entregar" son el circuito normal, no un
             error (esta fijado en `test_rss_emitido_completa_entrega_del_ingreso`).
- `bejerman` el RSS existe en Bejerman y no entro a NEXORA por ningun lado. Se asocia solo
             cuando hay **un unico** candidato y el codigo de cliente del comprobante
             coincide con el del equipo; lo ambiguo se informa para cargarlo a mano.

`os` corre antes que `bejerman` porque una hoja que la orden ya puede cerrar no tiene por
que ir a preguntarle nada a Bejerman.

**La fecha de entrega es la del comprobante, no la de hoy.** Los helpers del circuito
sellan `CURRENT_TIMESTAMP` porque ahi la entrega esta pasando en ese momento; en un
backfill eso haria pasar por reciente una entrega de junio, y la garantia de reparacion se
calcula justamente con `MAX(fecha_entrega)` a 90 dias.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time

from django.core.management.base import BaseCommand
from django.db import connection

from service.delivery_orders import (
    DeliveryOrderError,
    _ENTREGA_SOURCE_ESTADOS,
    _service_release_ingreso_ids_for_order_ids,
    _sync_service_release_ingresos_delivery,
    mark_delivered,
)

FASES = ("os", "ordenes", "bejerman")
# Estados en los que la hoja de servicio ya dio la salida por buena.
ESTADOS_CERRADOS = ("entregado", "alquilado", "vendido_entregado", "baja")
# Estados de orden que ya no esperan nada de Recepcion.
ESTADOS_ORDEN_CERRADA = (
    "cancelado",
    "entregado_no_facturable",
    "entregado_pendiente_facturacion",
    "facturado",
)
# Las series reales traen almohadilla, barra y espacios (D9#1807, MA26/040, "PO5614386 A92A"):
# se aceptan, pero nada que pueda cerrar el literal que se arma para SAMTRONIC.
_SERIE_SEGURA = re.compile(r"[^A-Za-z0-9#/._\- ]")
# Punto flexible (hay remitos viejos tipeados sin ceros) pero numero de 4 digitos para
# arriba: con menos, un "24-08" suelto se colaria como si fuera un comprobante.
_DOCUMENTO = re.compile(r"(\d{1,5})\s*-\s*(\d{4,10})")
# El RSS existe desde 2026; el piso solo protege del año tipeado en dos dígitos.
_PISO_FECHA = date(2020, 1, 1)
_SQL_RSS_POR_PARTIDA = """
SET NOCOUNT ON;
SELECT LTRIM(RTRIM(t.spv_Letra)) AS letra,
       LTRIM(RTRIM(t.spv_CodPvt)) AS punto,
       LTRIM(RTRIM(t.spv_Nro)) AS numero,
       CONVERT(varchar(10), c.scv_FEmision, 23) AS fecha,
       LTRIM(RTRIM(COALESCE(c.scvcli_Cod, ''))) AS cliente_codigo,
       LTRIM(RTRIM(COALESCE(c.scvcli_RazSoc, ''))) AS cliente_nombre,
       LTRIM(RTRIM(COALESCE(d.sdvdep_Cod, ''))) AS deposito,
       UPPER(LTRIM(RTRIM(COALESCE(d.sdvstp_Partida, '')))) AS partida
  FROM dbo.SegCabV c
  JOIN dbo.SegTiposV t ON t.spvscv_ID = c.scv_ID
  JOIN dbo.SegDetV d ON d.sdvscv_ID = c.scv_ID
 WHERE LTRIM(RTRIM(t.spvtco_Cod)) = 'RSS'
   AND UPPER(LTRIM(RTRIM(COALESCE(d.sdvstp_Partida, '')))) IN ({partidas})
   AND c.scv_FEmision >= '{desde}'
 ORDER BY c.scv_FEmision, t.spv_Nro;
""".strip()


def _rows(cur) -> list[dict]:
    columns = [col[0] for col in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _serie_key(valor) -> str:
    return _SERIE_SEGURA.sub("", str(valor or "").strip()).strip().upper()


def _mediodia_local(dia: date) -> datetime:
    """La fecha del comprobante como instante, al mediodia de la zona de la app.

    Django habla con Postgres en UTC (`USE_TZ = True`), asi que una fecha suelta se guarda
    a medianoche UTC y en hora argentina se lee como el dia anterior: el remito del 09/06
    quedaba entregado el 08/06. Al mediodia ningun huso mueve el dia.
    """
    from django.utils import timezone

    return timezone.make_aware(datetime.combine(dia, time(12, 0)), timezone.get_current_timezone())


def _numero_remito(fila: dict) -> str:
    punto = str(fila.get("punto") or "").zfill(5)
    numero = str(fila.get("numero") or "").lstrip("0").zfill(8)
    return f"RSS {fila.get('letra') or 'R'} {punto}-{numero}"


class Command(BaseCommand):
    help = (
        "Informa (y con --apply corrige) los desfasajes entre las ordenes de entrega de "
        "reparaciones y las hojas de servicio. Dry-run por defecto."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Aplica los cambios de las fases pedidas.")
        parser.add_argument(
            "--fase",
            default="os,ordenes",
            help="Fases a correr, separadas por coma: " + ", ".join(FASES) + ". Default: os,ordenes.",
        )
        parser.add_argument(
            "--os",
            default="",
            help="Limita la fase `bejerman` a estos ingresos (ids separados por coma).",
        )
        parser.add_argument(
            "--usuario",
            type=int,
            default=None,
            help="Id de usuario a registrar como autor de los cambios (opcional).",
        )

    def handle(self, *args, **options):
        fases = [f.strip() for f in (options["fase"] or "").split(",") if f.strip()]
        desconocidas = [f for f in fases if f not in FASES]
        if desconocidas:
            self.stderr.write(
                "Fase desconocida: " + ", ".join(desconocidas) + ". Validas: " + ", ".join(FASES)
            )
            return
        apply_changes = options["apply"]
        actor = options["usuario"]
        if "os" in fases:
            self._fase_os(apply_changes)
        if "ordenes" in fases:
            self._fase_ordenes(apply_changes, actor)
        if "bejerman" in fases:
            self._fase_bejerman(options["os"], apply_changes, actor)

    # ------------------------------------------------------------------- Bejerman
    def _consultar_rss(self, series: set[str], desde: date) -> dict[str, list[dict]]:
        """Todas las series en una sola consulta por empresa.

        `search_remitos_by_serial` abre su propia conexion SSH por serie: con 90 hojas de
        servicio eso son 90 conexiones anidadas en fila, que es el patron que tumbo
        produccion el 27/07/2026.
        """
        from service.bejerman_delivery import REMITO_QUERY_COMPANY_KEYS, _remote_bejerman_sql_csv

        limpias = {serie for serie in series if serie}
        if not limpias:
            return {}
        # Hay fechas de ingreso tipeadas con el año en dos digitos (0026-05-18). Pasadas tal
        # cual al filtro, SQL Server no las convierte a `datetime` (su minimo es 1753) y el
        # canal devuelve rc=0 con el error solo en stderr: parece "0 remitos".
        piso = max(_PISO_FECHA, desde).isoformat()
        lista = ", ".join("'" + serie.replace("'", "''") + "'" for serie in sorted(limpias))
        sql = _SQL_RSS_POR_PARTIDA.format(partidas=lista, desde=piso)

        emitidos: dict[str, list[dict]] = {}
        for company_key in REMITO_QUERY_COMPANY_KEYS:
            outcome: dict = {}
            filas = _remote_bejerman_sql_csv(
                sql, company_key=company_key, outcome=outcome, timeout_seconds=180
            )
            if outcome.get("status") != "success":
                self.stderr.write(
                    f"  {company_key}: no se pudo consultar Bejerman ({outcome.get('status')})."
                )
                continue
            self.stdout.write(f"  {company_key}: {len(filas)} renglones RSS.")
            for fila in filas:
                emitidos.setdefault(_serie_key(fila.get("partida")), []).append(fila)
        return emitidos

    # ------------------------------------------------------------------ fase os
    def _fase_os(self, apply_changes: bool) -> None:
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                "\n[os] La orden tiene el remito y la hoja de servicio sigue abierta"
            )
        )
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT o.id AS order_id, o.order_number, o.remito_number, o.delivered_at, o.status
                  FROM delivery_orders o
                 WHERE o.delivery_type = 'service_release'
                   AND o.status <> 'cancelado'
                   AND NULLIF(BTRIM(COALESCE(o.remito_number, '')), '') IS NOT NULL
                 ORDER BY o.delivered_at NULLS LAST, o.id
                """
            )
            orders = _rows(cur)

        total = 0
        for order in orders:
            order_id = str(order["order_id"])
            ingreso_ids = _service_release_ingreso_ids_for_order_ids([order_id])
            if not ingreso_ids:
                continue
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT id, estado FROM ingresos WHERE id = ANY(%s) ORDER BY id",
                    [ingreso_ids],
                )
                ingresos = _rows(cur)
            drifted = [
                i for i in ingresos if (i["estado"] or "").strip().lower() in _ENTREGA_SOURCE_ESTADOS
            ]
            if not drifted:
                continue

            total += len(drifted)
            cuando = f"entregada {order['delivered_at']}" if order["delivered_at"] else order["status"]
            self.stdout.write(
                f"  {order['order_number']} (remito {order['remito_number']}, {cuando}): "
                + ", ".join(f"OS-{i['id']} [{i['estado']}]" for i in drifted)
            )
            if apply_changes:
                # La orden ya sabe cuando se entrego; la hoja hereda esa fecha y no la de hoy.
                synced = _sync_service_release_ingresos_delivery(
                    [i["id"] for i in drifted],
                    order["remito_number"],
                    None,
                    fecha_entrega=order["delivered_at"],
                )
                self.stdout.write(self.style.SUCCESS(f"    -> sincronizadas: {synced}"))

        if total == 0:
            self.stdout.write(self.style.SUCCESS("  Nada para sincronizar."))
        elif not apply_changes:
            self.stdout.write(self.style.WARNING(f"  DRY-RUN: {total} OS detectadas (usar --apply)."))

    # -------------------------------------------------------------- fase ordenes
    def _ordenes_con_hoja_cerrada(self) -> list[dict]:
        with connection.cursor() as cur:
            cur.execute(
                """
                WITH enlaces AS (
                  SELECT o.id AS order_id,
                         o.order_number,
                         o.status,
                         COALESCE(NULLIF(BTRIM(o.remito_number), ''), '') AS remito,
                         o.created_at::date AS creada,
                         enlazadas.ingreso_id
                    FROM delivery_orders o
                    JOIN LATERAL (
                          SELECT o2.ingreso_id FROM delivery_orders o2
                           WHERE o2.id = o.id AND o2.ingreso_id IS NOT NULL
                          UNION
                          SELECT doi.ingreso_id FROM delivery_order_items doi
                           WHERE doi.order_id = o.id AND doi.ingreso_id IS NOT NULL
                          UNION
                          SELECT doip.ingreso_id
                            FROM delivery_order_item_partidas doip
                            JOIN delivery_order_items doi2 ON doi2.id = doip.order_item_id
                           WHERE doi2.order_id = o.id AND doip.ingreso_id IS NOT NULL
                         ) enlazadas ON TRUE
                   WHERE o.delivery_type = 'service_release'
                     AND o.status <> ALL(%s)
                )
                SELECT e.order_id, e.order_number, e.status, e.remito, e.creada,
                       i.id AS ingreso_id, i.estado::text AS estado,
                       i.fecha_ingreso::date AS fecha_ingreso,
                       i.fecha_entrega,
                       COALESCE(NULLIF(BTRIM(i.remito_salida), ''), '') AS remito_salida,
                       COALESCE(BTRIM(d.numero_serie), '') AS numero_serie
                  FROM enlaces e
                  JOIN ingresos i ON i.id = e.ingreso_id
                  LEFT JOIN devices d ON d.id = i.device_id
                 WHERE e.order_id IN (
                        SELECT order_id FROM enlaces e2
                         GROUP BY order_id
                        HAVING BOOL_AND(EXISTS (
                                 SELECT 1 FROM ingresos i2
                                  WHERE i2.id = e2.ingreso_id AND i2.estado::text = ANY(%s)
                               ))
                       )
                 ORDER BY e.creada, e.order_number, i.id
                """,
                [list(ESTADOS_ORDEN_CERRADA), list(ESTADOS_CERRADOS)],
            )
            return _rows(cur)

    def _remito_para_cerrar(self, hojas: list[dict], rss: dict[str, list[dict]]) -> tuple[str, str] | None:
        """Con que numero se puede cerrar la orden, y de donde salio.

        Primero lo que ya escribio una persona en la hoja de servicio, si es un
        comprobante de verdad; si no, el RSS que Bejerman tiene para esas series.
        """
        for hoja in hojas:
            if _DOCUMENTO.search(hoja["remito_salida"]):
                return hoja["remito_salida"], "hoja de servicio"

        numeros: set[str] = set()
        for hoja in hojas:
            candidatos = [
                fila
                for fila in rss.get(_serie_key(hoja["numero_serie"]), [])
                if (fila.get("fecha") or "") >= hoja["fecha_ingreso"].isoformat()
            ]
            if len(candidatos) != 1:
                return None
            fila = candidatos[0]
            # Un `remito_salida` de digitos sueltos suele ser el numero sin el punto de
            # venta ("4673" por 00004-00004673); si no coincide con el que dice Bejerman,
            # son dos salidas distintas y no hay que adivinar cual vale.
            fragmento = hoja["remito_salida"]
            if (
                fragmento.isdigit()
                and fragmento != str(hoja["ingreso_id"])
                and fragmento.lstrip("0") not in str(fila.get("numero") or "").lstrip("0")
            ):
                return None
            numeros.add(_numero_remito(fila))
        if len(numeros) != 1:
            return None
        return numeros.pop(), "Bejerman"

    def _fase_ordenes(self, apply_changes: bool, actor: int | None) -> None:
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                "\n[ordenes] La hoja de servicio ya cerro y la orden sigue abierta"
            )
        )
        filas = self._ordenes_con_hoja_cerrada()
        por_orden: dict[str, list[dict]] = {}
        for fila in filas:
            por_orden.setdefault(fila["order_id"], []).append(fila)

        con_remito = {oid: hojas for oid, hojas in por_orden.items() if hojas[0]["remito"]}
        sin_remito = {oid: hojas for oid, hojas in por_orden.items() if not hojas[0]["remito"]}

        def _resumen(hojas):
            return " | ".join(
                f"OS-{h['ingreso_id']} {h['estado']} "
                f"fe={h['fecha_entrega'].date() if h['fecha_entrega'] else '-'} "
                f"rem={h['remito_salida'] or '(vacio)'}"
                for h in hojas
            )

        self.stdout.write(
            f"  Con remito en la orden ({len(con_remito)}): esperan el boton Entregar de Recepcion, "
            "es el circuito normal, no se tocan."
        )
        for hojas in con_remito.values():
            cab = hojas[0]
            self.stdout.write(
                f"    {cab['order_number']} {cab['creada']} remito {cab['remito']} -> {_resumen(hojas)}"
            )

        self.stdout.write(f"  Sin remito en la orden ({len(sin_remito)}): quedaron colgadas en la cola.")
        if not sin_remito:
            return

        # Una sola consulta a Bejerman para todas las series de esta fase.
        series = {_serie_key(h["numero_serie"]) for hojas in sin_remito.values() for h in hojas}
        desde = min(h["fecha_ingreso"] for hojas in sin_remito.values() for h in hojas)
        rss = self._consultar_rss(series, desde)

        cerrables, sin_numero = [], []
        for order_id, hojas in sin_remito.items():
            resuelto = self._remito_para_cerrar(hojas, rss)
            (cerrables if resuelto else sin_numero).append((order_id, hojas, resuelto))

        for order_id, hojas, resuelto in cerrables:
            cab = hojas[0]
            remito, origen = resuelto
            self.stdout.write(
                f"    {cab['order_number']} {cab['creada']} -> cerrar con {remito} (segun {origen}); "
                f"{_resumen(hojas)}"
            )
            if not apply_changes:
                continue
            try:
                mark_delivered(order_id, actor, remito)
            except DeliveryOrderError as exc:
                self.stderr.write(f"      no se pudo cerrar: {exc}")
                continue
            # `mark_delivered` sella la entrega con la hora de ahora porque en el circuito la
            # entrega esta pasando; aca ya paso, y la fecha buena es la de la hoja.
            entregada = min(
                (h["fecha_entrega"] for h in hojas if h["fecha_entrega"]), default=None
            )
            if entregada:
                with connection.cursor() as cur:
                    cur.execute(
                        "UPDATE delivery_orders SET delivered_at = %s, prepared_at = %s WHERE id = %s",
                        [entregada, entregada, order_id],
                    )
            self.stdout.write(self.style.SUCCESS("      -> cerrada"))

        for _order_id, hojas, _ in sin_numero:
            cab = hojas[0]
            self.stdout.write(
                self.style.WARNING(
                    f"    {cab['order_number']} {cab['creada']} -> sin numero de remito recuperable; "
                    f"{_resumen(hojas)}"
                )
            )
        if sin_numero:
            self.stdout.write(
                self.style.WARNING(
                    f"  {len(sin_numero)} ordenes quedan abiertas: ni la hoja ni Bejerman dan un "
                    "comprobante con el que cerrarlas (salieron por alquiler o el campo tiene el "
                    "numero de OS). Cancelarlas es decision de negocio."
                )
            )
        if cerrables and not apply_changes:
            self.stdout.write(self.style.WARNING(f"  DRY-RUN: {len(cerrables)} ordenes (usar --apply)."))

    # ------------------------------------------------------------- fase bejerman
    def _fase_bejerman(self, os_filter: str, apply_changes: bool, actor: int | None) -> None:
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                "\n[bejerman] Hojas de servicio abiertas: busca el RSS ya emitido"
            )
        )
        ids = [int(v) for v in (os_filter or "").replace(" ", "").split(",") if v.isdigit()]
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT i.id, i.estado::text AS estado, i.fecha_ingreso::date AS fecha_ingreso,
                       BTRIM(COALESCE(d.numero_serie, '')) AS numero_serie,
                       UPPER(BTRIM(COALESCE(c.cod_empresa, ''))) AS cod_empresa,
                       COALESCE(c.razon_social, '') AS cliente
                  FROM ingresos i
                  LEFT JOIN devices d ON d.id = i.device_id
                  LEFT JOIN customers c ON c.id = d.customer_id
                 WHERE i.estado::text <> ALL(%s)
                   AND NULLIF(BTRIM(COALESCE(i.remito_salida, '')), '') IS NULL
                   AND NULLIF(BTRIM(COALESCE(d.numero_serie, '')), '') IS NOT NULL
                   AND (CARDINALITY(%s::int[]) = 0 OR i.id = ANY(%s))
                 ORDER BY i.id
                """,
                [list(ESTADOS_CERRADOS), ids, ids],
            )
            pendientes = _rows(cur)

        if not pendientes:
            self.stdout.write(self.style.SUCCESS("  No hay hojas de servicio abiertas sin remito."))
            return

        series = {_serie_key(hoja["numero_serie"]) for hoja in pendientes}
        desde = min(hoja["fecha_ingreso"] for hoja in pendientes)
        self.stdout.write(
            f"  {len(pendientes)} hojas abiertas sin remito ({len(series)} series); "
            "una consulta por empresa..."
        )
        rss = self._consultar_rss(series, desde)

        aplicables, dudosos = [], []
        for hoja in pendientes:
            candidatos = [
                fila
                for fila in rss.get(_serie_key(hoja["numero_serie"]), [])
                if (fila.get("fecha") or "") >= hoja["fecha_ingreso"].isoformat()
            ]
            if not candidatos:
                continue
            self.stdout.write(
                f"  OS-{hoja['id']} [{hoja['estado']}] ing {hoja['fecha_ingreso']} "
                f"serie {hoja['numero_serie']} ({hoja['cod_empresa'] or 'sin codigo'}):"
            )
            for fila in candidatos:
                self.stdout.write(
                    f"    {_numero_remito(fila)}  {fila.get('fecha', '')}  "
                    f"{fila.get('cliente_codigo', '')}  {fila.get('cliente_nombre', '')}"
                )
            unico = candidatos[0] if len(candidatos) == 1 else None
            cliente_ok = bool(
                unico
                and hoja["cod_empresa"]
                and hoja["cod_empresa"] == str(unico.get("cliente_codigo") or "").strip().upper()
            )
            if cliente_ok:
                aplicables.append((hoja, unico))
            else:
                motivo = "varios candidatos" if not unico else "el cliente del remito no coincide"
                dudosos.append((hoja, motivo))
                self.stdout.write(self.style.WARNING(f"    no se asocia solo: {motivo}."))

        if not aplicables and not dudosos:
            self.stdout.write(self.style.SUCCESS("  Ninguna tiene un RSS emitido en Bejerman."))
            return

        self.stdout.write(
            f"  {len(aplicables)} se pueden asociar solas, {len(dudosos)} necesitan el buscador "
            "de la ServiceSheet (campo Remito salida)."
        )
        if not apply_changes:
            if aplicables:
                self.stdout.write(self.style.WARNING("  DRY-RUN: no se aplico nada (usar --apply)."))
            return

        for hoja, fila in aplicables:
            remito = _numero_remito(fila)
            fecha = date.fromisoformat(fila["fecha"])
            self._cerrar_hoja(hoja["id"], remito, _mediodia_local(fecha), actor)
            self.stdout.write(self.style.SUCCESS(f"    OS-{hoja['id']} -> {remito} ({fecha})"))

    def _cerrar_hoja(self, ingreso_id: int, remito: str, fecha: datetime, actor: int | None) -> None:
        """Cierra la hoja de servicio como lo hace la pantalla de entrega.

        El estado sale de `resolve_ingreso_entrega_estado`, que es el mismo helper que usa
        `EntregarIngresoView`: un MG vuelve a `alquilado` y una venta pendiente cierra como
        `vendido_entregado`, no todo cae en `entregado`.
        """
        from service.views.ingresos_views import resolve_ingreso_entrega_estado

        deteccion = resolve_ingreso_entrega_estado(ingreso_id, remito_salida=remito)
        estado_to = deteccion["estado_to"]
        sets = [
            "estado = %s",
            "remito_salida = COALESCE(NULLIF(remito_salida, ''), %s)",
            "fecha_entrega = COALESCE(fecha_entrega, %s)",
        ]
        params: list = [estado_to, remito, fecha]
        if deteccion["set_alquilado"]:
            sets.append("alquilado = true")
        if deteccion["clear_alquilado"]:
            sets.extend(
                [
                    "alquilado = false",
                    "alquiler_a = NULL",
                    "alquiler_remito = NULL",
                    "alquiler_fecha = NULL",
                ]
            )
        params.append(ingreso_id)
        with connection.cursor() as cur:
            cur.execute("UPDATE ingresos SET " + ", ".join(sets) + " WHERE id = %s", params)
            cur.execute(
                """
                INSERT INTO ingreso_events (ticket_id, a_estado, usuario_id, comentario)
                SELECT %s, %s, %s, %s
                 WHERE NOT EXISTS (
                       SELECT 1 FROM ingreso_events WHERE ticket_id = %s AND a_estado = %s
                 )
                """,
                [
                    ingreso_id,
                    estado_to,
                    actor,
                    f"Entrega registrada desde el remito {remito} ya emitido en Bejerman",
                    ingreso_id,
                    estado_to,
                ],
            )
